"""連鎖した例外と、切られたスタックトレースは、受け渡しに使わない（main に入れる前のレビュー、5 巡目の修正仕様 ②）。

背景: A3 の確かめは「最初のブロックを終える例外の行の型が、記録された ``error_type`` と同じか」だけを見ていた。
``ValueError`` を捕まえて ``ValueError`` を投げ直すと、最初のブロックは内側の例外で終わり、記録された型も
``ValueError`` なので、一致してしまう。内側の場所が、外側の例外の場所として修正役に渡った。

仕様:

- スタックトレースの最初のブロックの後ろに、次のどれかがあれば、そのイベントの例外の型と場所を、受け渡しに使わない
  （``exception_type`` と ``exception_evidence_id`` は ``None``、``code_locations`` は空、``fixer_eligible`` は偽）。
  - ``During handling of the above exception, another exception occurred:`` という行
  - ``The above exception was the direct cause of the following exception:`` という行
  - 2 つ目の ``Traceback (most recent call last):`` という行
- スタックトレースが 5,000 字以上のときも、使わない（記録する側が先頭 5,000 字で切るので、後ろに連鎖があっても
  分からない）。
- モデルに返す結果（``payload.error_type``、``frames``）は、今までどおり返す。
- 連鎖の無い、5,000 字未満のスタックトレースは、今までどおり使える。

再現は、実際に例外を連鎖させて ``traceback.format_exc()`` で作る。コードは、一時ディレクトリの「コードの根」に
置いたものを、``/app/worker.py`` という名前でコンパイルして動かす（トレースのパスが、本番と同じ形になる）。
"""

import linecache
import traceback
from unittest.mock import patch

import pytest

from . import fakes, scenarios
from .factories import frame_ids, parse, valid_submission

APP_PATH = "/app/worker.py"
HEADER = "Traceback (most recent call last):"
DURING = "During handling of the above exception, another exception occurred:"
DIRECT_CAUSE = "The above exception was the direct cause of the following exception:"
TRACE_LIMIT = 5_000  # 記録する側が切る長さ

WORKER_SOURCE = '''\
import traceback


class LoadError(Exception):
    """コードの根で定義された例外。"""


def parse_value(text):
    raise ValueError("inner: " + text)


def lookup(data, key):
    return data[key]


def plain(text):
    return parse_value(text)


def same_type_from(text):
    try:
        return parse_value(text)
    except ValueError as error:
        raise ValueError("outer: could not parse") from error


def same_type_in_handler(text):
    try:
        return parse_value(text)
    except ValueError:
        raise ValueError("outer: could not parse")


def other_type_from(text):
    try:
        return lookup({}, text)
    except KeyError as error:
        raise RuntimeError("outer: lookup failed") from error


def other_type_in_handler(text):
    try:
        return lookup({}, text)
    except KeyError:
        raise RuntimeError("outer: lookup failed")


def own_type_from(text):
    try:
        raise LoadError("inner: " + text)
    except LoadError as error:
        raise LoadError("outer: load failed") from error


def in_finally(text):
    try:
        return parse_value(text)
    finally:
        raise ValueError("outer: cleanup failed")


def three_levels(text):
    try:
        return same_type_from(text)
    except ValueError as error:
        raise ValueError("outermost") from error


def context_suppressed(text):
    try:
        return parse_value(text)
    except ValueError:
        raise ValueError("outer: context suppressed") from None


def handled_then_new(text):
    try:
        parse_value(text)
    except ValueError:
        pass
    return parse_value("second " + text)


def entry(function, text):
    """本番の入口と同じ形: 例外を捕まえて、型の名前と ``traceback.format_exc()`` を記録する。"""
    try:
        function(text)
    except Exception as error:
        return type(error).__name__, traceback.format_exc()
    raise AssertionError("the function did not raise")
'''

# 連鎖する関数: 名前 → (記録される型, 区切りの文, 最初のブロックの最後のフレームの関数)
CHAINED = {
    "same_type_from": ("ValueError", DIRECT_CAUSE, "parse_value"),
    "same_type_in_handler": ("ValueError", DURING, "parse_value"),
    "own_type_from": ("LoadError", DIRECT_CAUSE, "own_type_from"),
    "in_finally": ("ValueError", DURING, "parse_value"),
    "three_levels": ("ValueError", DIRECT_CAUSE, "parse_value"),
    "other_type_from": ("RuntimeError", DIRECT_CAUSE, "lookup"),
    "other_type_in_handler": ("RuntimeError", DURING, "lookup"),
}
# 内側と外側が同じ型になるもの（レビューで見つかった抜け。今までの確かめを通る）
SAME_TYPE = ("same_type_from", "same_type_in_handler", "own_type_from", "in_finally", "three_levels")
# 連鎖しない関数: 名前 → 最後のフレームの関数
UNCHAINED = {"plain": "parse_value", "context_suppressed": "context_suppressed", "handled_then_new": "parse_value"}


@pytest.fixture
def worker(tmp_path):
    """コードの根に ``worker.py`` を置き、``/app/worker.py`` として動かせる名前空間を返す。"""
    root = tmp_path / "code"
    root.mkdir()
    (root / "worker.py").write_text(WORKER_SOURCE, encoding="utf-8")
    namespace: dict = {"__name__": "worker"}
    exec(compile(WORKER_SOURCE, APP_PATH, "exec"), namespace)  # noqa: S102
    # トレースに、実物と同じようにソースの行が出るようにする
    linecache.cache[APP_PATH] = (len(WORKER_SOURCE), None, WORKER_SOURCE.splitlines(keepends=True), APP_PATH)
    namespace["__code_root__"] = root
    yield namespace
    linecache.cache.pop(APP_PATH, None)


def record(worker, name: str, text: str = "x") -> tuple[str, str]:
    """``name`` の関数で実際に例外を起こし、記録される（型の名前, スタックトレース）を返す。"""
    error_type, stack_trace = worker["entry"](worker[name], text)
    assert stack_trace.startswith(HEADER + "\n")
    return error_type, stack_trace


def first_block(stack_trace: str) -> str:
    """最初のブロック（見出しから、最初の例外の行まで）。"""
    lines = stack_trace.splitlines(keepends=True)
    for index, line in enumerate(lines[1:], start=1):
        if not line.startswith(" "):
            return "".join(lines[: index + 1])
    raise AssertionError("no exception line")


def make_session(worker, error_type, stack_trace):
    from diagnosis.models import DiagnosisLimits
    from diagnosis.tools import ToolSession

    scenario = scenarios.rate_limited()
    payload = {"error_type": error_type, "error_message": "boom", "stack_trace": stack_trace}
    scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
    session = ToolSession(fakes.build_reader(scenario).reader, DiagnosisLimits(), code_root=worker["__code_root__"])
    return session, scenario


def code_defect_submission(code_location_ids) -> dict:
    return valid_submission(
        failed_stage="generating",
        cause_category="output_validation_failed",
        cause_nature="code_defect",
        confidence="high",
        action_type="fix_code",
        evidence=[{"id": "EV-1", "note": "x"}],
        exception_evidence_id="EV-1",
        code_location_ids=code_location_ids,
    )


def hand_off(worker, error_type, stack_trace):
    """記録を読み、読めたフレームをすべて選んで提出する。返すのは（イベント, 診断, 受け渡し）。"""
    from diagnosis.handoff import build_handoff

    session, scenario = make_session(worker, error_type, stack_trace)
    listed = session.call("list_events", {})
    assert listed.outcome == "ok"
    event = parse(listed)["events"][0]
    submitted = session.call("submit_diagnosis", code_defect_submission(frame_ids(event)[:5]))
    assert submitted.outcome == "ok"
    diagnosis = session.finalize()
    return event, diagnosis, build_handoff(scenario.execution_id, diagnosis)


def expect_dropped(diagnosis, handoff) -> None:
    assert handoff.exception_type is None
    assert tuple(handoff.code_locations) == ()
    assert handoff.fixer_eligible is False
    assert diagnosis.exception_type is None
    assert diagnosis.exception_evidence_id is None
    assert list(diagnosis.code_locations) == []
    assert list(diagnosis.code_location_evidence_ids) == []
    assert handoff.to_dict()["code_locations"] == []
    assert handoff.to_dict()["exception_type"] is None


def assert_usable(diagnosis, handoff, error_type: str, last_function: str) -> None:
    assert handoff.exception_type == error_type
    assert handoff.fixer_eligible is True
    assert diagnosis.exception_evidence_id == "EV-1"
    assert [location.function for location in handoff.code_locations][-1] == last_function
    assert {location.path for location in handoff.code_locations} == {APP_PATH}


def padded_plain_trace(worker, length: int) -> str:
    """連鎖の無い、ちょうど ``length`` 字のスタックトレース（例外メッセージの長さで合わせる）。"""
    _, shortest = record(worker, "plain", "")
    _, stack_trace = record(worker, "plain", "m" * (length - len(shortest)))
    assert len(stack_trace) == length
    return stack_trace


# ---------------------------------------------------------------------------
# 実際に連鎖させた例外
# ---------------------------------------------------------------------------


class TestRealChainedExceptions:
    @pytest.mark.parametrize("name", SAME_TYPE)
    def test_reviewer_reproduction_same_type_chain_is_not_handed_off(self, worker, name):
        """レビューの再現: 内側と外側が同じ型。最初のブロックの型が、記録された型と一致してしまう。"""
        expected_type, separator, inner_function = CHAINED[name]
        error_type, stack_trace = record(worker, name)
        # 筋書きの確認: 記録の型と、最初のブロックを終える例外の行の型が、同じである（今までの確かめは通る）
        assert error_type == expected_type
        assert first_block(stack_trace).splitlines()[-1].split(":")[0].split(".")[-1] == error_type
        assert separator in stack_trace.splitlines()
        assert stack_trace.count(HEADER) >= 2
        assert len(stack_trace) < TRACE_LIMIT

        event, diagnosis, handoff = hand_off(worker, error_type, stack_trace)

        expect_dropped(diagnosis, handoff)
        # モデルに返す結果は、今までどおり（診断の材料としては使える）
        assert event["payload"]["error_type"] == error_type
        assert [frame["function"] for frame in event["frames"]][-1] == inner_function
        assert {frame["path"] for frame in event["frames"]} == {APP_PATH}

    @pytest.mark.parametrize("name", ["other_type_from", "other_type_in_handler"])
    def test_chain_of_different_types_is_still_not_handed_off(self, worker, name):
        """今までも渡らなかった形（記録の型は外側、最初のブロックは内側）。"""
        expected_type, separator, inner_function = CHAINED[name]
        error_type, stack_trace = record(worker, name)
        assert error_type == expected_type
        assert separator in stack_trace.splitlines()

        event, diagnosis, handoff = hand_off(worker, error_type, stack_trace)

        expect_dropped(diagnosis, handoff)
        assert event["payload"]["error_type"] == "RuntimeError"
        assert [frame["function"] for frame in event["frames"]][-1] == inner_function

    @pytest.mark.parametrize("name", ["other_type_from", "other_type_in_handler"])
    def test_chain_recorded_with_the_inner_type_is_not_handed_off_either(self, worker, name):
        """記録の型が内側の型と同じでも、連鎖があれば使わない（どちらの例外の場所か、決められない）。"""
        _, stack_trace = record(worker, name)

        event, diagnosis, handoff = hand_off(worker, "KeyError", stack_trace)

        expect_dropped(diagnosis, handoff)
        assert event["payload"]["error_type"] == "KeyError"
        assert len(event["frames"]) >= 1

    @pytest.mark.parametrize("name", sorted(UNCHAINED))
    def test_trace_without_a_chain_is_handed_off_as_before(self, worker, name):
        error_type, stack_trace = record(worker, name)
        assert stack_trace.count(HEADER) == 1
        assert DURING not in stack_trace
        assert DIRECT_CAUSE not in stack_trace

        event, diagnosis, handoff = hand_off(worker, error_type, stack_trace)

        assert error_type == "ValueError"
        assert_usable(diagnosis, handoff, "ValueError", UNCHAINED[name])
        assert [frame["function"] for frame in event["frames"]][0] == "entry"

    def test_frames_returned_to_the_model_are_the_same_with_and_without_the_rule(self, worker):
        """``frames`` は、最初のブロックのものを、今までどおり返す（2 つ目のブロックは読まない）。"""
        error_type, stack_trace = record(worker, "same_type_from")

        event, _, _ = hand_off(worker, error_type, stack_trace)

        assert [frame["function"] for frame in event["frames"]] == ["same_type_from", "parse_value"]
        assert all(set(frame) == {"frame_id", "path", "function", "line"} for frame in event["frames"])

    def test_chained_trace_does_not_become_eligible_through_diagnose_execution(self, worker):
        """入口から通しても、同じ型の連鎖は「修正役に渡せる」にならない。"""
        from diagnosis import diagnose_execution

        from .sdk_fake import FakeSdk

        runs = {}
        for name in ("same_type_from", "plain"):
            error_type, stack_trace = record(worker, name)
            scenario = scenarios.rate_limited()
            payload = {"error_type": error_type, "error_message": "boom", "stack_trace": stack_trace}
            scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", payload)]
            bundle = fakes.build_reader(scenario)

            async def script(call) -> None:
                await call("list_events")
                await call("submit_diagnosis", code_defect_submission(["FR-1", "FR-2"]))

            with patch("diagnosis.runner._invoke_sdk", FakeSdk(script)):
                runs[name] = diagnose_execution(
                    scenario.execution_id,
                    reader=bundle.reader,
                    emitter=fakes.FakeEmitter(),
                    code_root=str(worker["__code_root__"]),
                )

        assert runs["same_type_from"].status == "completed"
        assert runs["same_type_from"].handoff.fixer_eligible is False
        assert runs["same_type_from"].handoff.exception_type is None
        assert tuple(runs["same_type_from"].handoff.code_locations) == ()
        assert runs["plain"].handoff.fixer_eligible is True
        assert runs["plain"].handoff.exception_type == "ValueError"


# ---------------------------------------------------------------------------
# 連鎖の印になる行（最初のブロックの後ろ）
# ---------------------------------------------------------------------------


def second_block() -> str:
    return f'{HEADER}\n  File "{APP_PATH}", line 16, in plain\n    return parse_value(text)\nValueError: outer\n'


class TestChainMarkersAfterTheFirstBlock:
    @pytest.mark.parametrize(
        "tail",
        [
            f"\n{DURING}\n\n" + second_block(),
            f"\n{DIRECT_CAUSE}\n\n" + second_block(),
            f"\n{DURING}\n",  # 区切りの文だけ（2 つ目のブロックは、切れて無い）
            f"\n{DIRECT_CAUSE}\n",
            f"\n{DURING}",  # 末尾の改行も無い
            f"\n{DIRECT_CAUSE}",
            f"{DURING}\n",  # 空行を挟まない
            f"{DIRECT_CAUSE}\n",
            "\n" + second_block(),  # 区切りの文が無く、2 つ目の見出しだけ
            second_block(),
            f"\n{HEADER}\n",
            f"\n{HEADER}",
            f"more detail of the message\n\n{DURING}\n\n" + second_block(),  # 複数行のメッセージの後ろ
            "more detail of the message\n" + second_block(),
            f"\n\n\n\n{DIRECT_CAUSE}\n\n\n" + second_block(),
        ],
        ids=[
            "during_and_block",
            "direct_cause_and_block",
            "during_only",
            "direct_cause_only",
            "during_without_newline",
            "direct_cause_without_newline",
            "during_without_blank_line",
            "direct_cause_without_blank_line",
            "second_header_after_blank_line",
            "second_header_directly",
            "second_header_line_only",
            "second_header_without_newline",
            "during_after_message_lines",
            "second_header_after_message_lines",
            "direct_cause_after_many_blank_lines",
        ],
    )
    def test_marker_line_after_the_first_block_makes_the_event_unusable(self, worker, tail):
        error_type, plain = record(worker, "plain")
        stack_trace = plain + tail
        assert len(stack_trace) < TRACE_LIMIT

        event, diagnosis, handoff = hand_off(worker, error_type, stack_trace)

        expect_dropped(diagnosis, handoff)
        assert event["payload"]["error_type"] == "ValueError"
        assert [frame["function"] for frame in event["frames"]] == ["entry", "plain", "parse_value"]

    @pytest.mark.parametrize(
        "tail",
        ["", "second line of the message\n", "  indented detail\nlast line\n", "\n", "\n\nnote: retried 3 times\n"],
        ids=["nothing", "message_line", "indented_message_lines", "blank_line", "note_after_blank_lines"],
    )
    def test_trace_followed_by_lines_that_are_not_markers_is_still_usable(self, worker, tail):
        error_type, plain = record(worker, "plain")

        _, diagnosis, handoff = hand_off(worker, error_type, plain + tail)

        assert_usable(diagnosis, handoff, "ValueError", "parse_value")

    def test_only_the_marked_event_is_dropped_when_another_event_is_clean(self, worker):
        """連鎖のあるイベントを例外の根拠にすると使えず、連鎖の無いイベントなら使える（イベントごとに決まる）。"""
        from diagnosis.handoff import build_handoff
        from diagnosis.models import DiagnosisLimits
        from diagnosis.tools import ToolSession

        chained_type, chained = record(worker, "same_type_from")
        plain_type, plain = record(worker, "plain")
        results = {}
        for chosen in ("EV-1", "EV-2"):
            scenario = scenarios.rate_limited()
            scenario.events = [
                scenarios.make_event(
                    scenario.execution_id, 1, "error", {"error_type": chained_type, "stack_trace": chained}
                ),
                scenarios.make_event(
                    scenario.execution_id, 2, "error", {"error_type": plain_type, "stack_trace": plain}
                ),
            ]
            session = ToolSession(
                fakes.build_reader(scenario).reader, DiagnosisLimits(), code_root=worker["__code_root__"]
            )
            events = parse(session.call("list_events", {}))["events"]
            submission = code_defect_submission([*frame_ids(events[0]), *frame_ids(events[1])])
            submission["evidence"] = [{"id": "EV-1", "note": "x"}, {"id": "EV-2", "note": "y"}]
            submission["exception_evidence_id"] = chosen
            assert session.call("submit_diagnosis", submission).outcome == "ok"
            diagnosis = session.finalize()
            results[chosen] = (diagnosis, build_handoff(scenario.execution_id, diagnosis))

        expect_dropped(*results["EV-1"])
        diagnosis, handoff = results["EV-2"]
        assert handoff.fixer_eligible is True
        assert diagnosis.exception_evidence_id == "EV-2"
        assert set(diagnosis.code_location_evidence_ids) == {"EV-2"}
        assert [location.function for location in handoff.code_locations] == ["entry", "plain", "parse_value"]


# ---------------------------------------------------------------------------
# 5,000 字以上のスタックトレース
# ---------------------------------------------------------------------------


class TestTraceAtTheRecordingLimit:
    @pytest.mark.parametrize("length", [TRACE_LIMIT, TRACE_LIMIT + 1, 12_000])
    def test_trace_of_five_thousand_characters_or_more_is_not_handed_off(self, worker, length):
        """切られていなくても、5,000 字以上なら使わない（切られたものと、見分けられない）。"""
        stack_trace = padded_plain_trace(worker, length)

        event, diagnosis, handoff = hand_off(worker, "ValueError", stack_trace)

        expect_dropped(diagnosis, handoff)
        assert event["payload"]["error_type"] == "ValueError"
        assert [frame["function"] for frame in event["frames"]] == ["entry", "plain", "parse_value"]

    @pytest.mark.parametrize("length", [TRACE_LIMIT - 1, TRACE_LIMIT - 100, 1_000])
    def test_trace_shorter_than_five_thousand_characters_is_handed_off_as_before(self, worker, length):
        """境界: 4,999 字までは、今までどおり使える。"""
        stack_trace = padded_plain_trace(worker, length)

        _, diagnosis, handoff = hand_off(worker, "ValueError", stack_trace)

        assert_usable(diagnosis, handoff, "ValueError", "parse_value")

    @pytest.mark.parametrize("name", ["same_type_from", "same_type_in_handler"])
    @pytest.mark.parametrize("kept_after_first_block", [0, 1, 2, 12])
    def test_real_chain_cut_at_the_recording_limit_is_not_handed_off(self, worker, name, kept_after_first_block):
        """実際の経路: 内側の例外のメッセージが長く、記録の 5,000 字が、最初のブロックの直後で切れる。

        切られた後には、区切りの文も 2 つ目の見出しも残らない（``kept_after_first_block`` は、最初のブロックの
        後ろに残る字数。0 なら、連鎖の無い 5,000 字のトレースと、まったく同じ形になる）。
        """
        _, shortest = record(worker, name, "")
        padding = TRACE_LIMIT - kept_after_first_block - len(first_block(shortest))
        error_type, whole = record(worker, name, "m" * padding)
        assert len(first_block(whole)) == TRACE_LIMIT - kept_after_first_block
        recorded = whole[:TRACE_LIMIT]  # 記録する側が、先頭 5,000 字で切る
        assert len(recorded) == TRACE_LIMIT
        assert DURING not in recorded
        assert DIRECT_CAUSE not in recorded
        assert recorded.count(HEADER) == 1

        event, diagnosis, handoff = hand_off(worker, error_type, recorded)

        expect_dropped(diagnosis, handoff)
        assert event["payload"]["error_type"] == "ValueError"
        assert [frame["function"] for frame in event["frames"]] == [name, "parse_value"]

    def test_long_trace_made_of_many_frames_is_not_handed_off(self, worker):
        """フレームが多くて 5,000 字を超える形（深い再帰）。例外の行まで読めても、使わない。"""
        frame = f'  File "{APP_PATH}", line 16, in plain\n    return parse_value(text)\n'
        stack_trace = HEADER + "\n" + frame * 100 + "ValueError: inner: x\n"
        assert len(stack_trace) >= TRACE_LIMIT

        event, diagnosis, handoff = hand_off(worker, "ValueError", stack_trace)

        expect_dropped(diagnosis, handoff)
        assert len(event["frames"]) >= 1

    def test_traceback_module_output_is_what_the_tests_assume(self, worker):
        """このファイルの前提の確認: ``format_exc()`` の連鎖の書き方（Python の版が変わったら、ここで気づく）。"""
        _, from_trace = record(worker, "same_type_from")
        _, context_trace = record(worker, "same_type_in_handler")

        assert f"\n\n{DIRECT_CAUSE}\n\n{HEADER}\n" in from_trace
        assert f"\n\n{DURING}\n\n{HEADER}\n" in context_trace
        assert traceback.format_exception_only(ValueError("x")) == ["ValueError: x\n"]
