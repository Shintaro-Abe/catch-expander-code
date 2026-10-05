"""スタックトレースのフレームと、実際のコードとの突き合わせ（レビュー後の修正仕様 S1）。

背景: ``error`` イベントの ``stack_trace`` の末尾（例外メッセージ。連鎖した例外のメッセージを含む）に、
``File "/app/….py", line N, in 名前`` の形の文字列があると、フレームとして拾われ、受け渡し用の診断結果
（修正役が受け取るもの）に入っていた。記録の中身は信頼できない入力なので、取り出したフレームは、
**実際のコードにその場所があるもの**だけを登録する。

呼び方（このテストが決めたもの）:
``ToolSession(reader, limits, code_root=<コードの根>)``。``/app/x/y.py`` は ``<コードの根>/x/y.py`` と突き合わせる。
``code_root`` は ``str`` でも ``pathlib.Path`` でもよい。渡さなければ ``diagnosis`` パッケージの親ディレクトリ。
"""

import inspect
import json
import os
from pathlib import Path

import pytest

from . import fakes, scenarios
from .factories import FRAME_ID, frame_ids, parse, valid_submission

APP_SOURCE = """\
import os

def top():
    x = 1
    return x

class Worker:
    def run(self):
        def inner():
            return 1
        return inner()

    async def fetch(self):
        return 2

class Other:
    def run(self):
        return 3

VALUE = top()
"""
APP_LINES = 20
BIG_BODY_LINES = 30  # big() は 1 行目が def、2〜31 行目が本体


def frame_line(path: str, line: object, function: str, *, indent: str = "  ") -> str:
    return f'{indent}File "{path}", line {line}, in {function}'


def trace(*frame_lines: str, error: str = "ValueError: boom") -> str:
    lines = ["Traceback (most recent call last):"]
    for frame in frame_lines:
        lines.extend([frame, "    do_something()"])
    lines.append(error)
    return "\n".join(lines) + "\n"


@pytest.fixture
def code_root(tmp_path) -> Path:
    root = tmp_path / "code"
    (root / "pkg").mkdir(parents=True)
    (root / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    (root / "pkg" / "mod.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (root / "big.py").write_text(
        "def big():\n" + "".join("    pass\n" for _ in range(BIG_BODY_LINES)), encoding="utf-8"
    )
    (root / "broken.py").write_text("def evil(:\n    pass\n", encoding="utf-8")
    (root / "binary.py").write_bytes(b"\xff\xfe\x00def evil():\n    pass\n")
    (root / "dir.py").mkdir()
    return root


def make_session(stack_trace, root, *, payload=None, **session_kwargs):
    from diagnosis.models import DiagnosisLimits
    from diagnosis.tools import ToolSession

    scenario = scenarios.rate_limited()
    body = {"error_type": "ValueError", "error_message": "boom", "stack_trace": stack_trace, **(payload or {})}
    scenario.events = [scenarios.make_event(scenario.execution_id, 1, "error", body)]
    bundle = fakes.build_reader(scenario)
    if root is not None:
        session_kwargs["code_root"] = root
    return ToolSession(bundle.reader, DiagnosisLimits(), **session_kwargs), scenario


def frames_of(stack_trace, root, **kwargs) -> list[dict]:
    session, _ = make_session(stack_trace, root, **kwargs)
    result = session.call("list_events", {})
    assert result.outcome == "ok"  # 捨てるフレームがあっても、例外は出さず、一覧は返る
    return parse(result)["events"][0]["frames"]


def locations(frames: list[dict]) -> list[tuple[str, str, int]]:
    assert all(set(frame) == {"frame_id", "path", "function", "line"} for frame in frames)
    assert all(FRAME_ID.match(frame["frame_id"]) for frame in frames)
    assert len({frame["frame_id"] for frame in frames}) == len(frames)
    return [(frame["path"], frame["function"], frame["line"]) for frame in frames]


# ---------------------------------------------------------------------------
# 本物のフレームは通る
# ---------------------------------------------------------------------------


class TestRealFrames:
    @pytest.mark.parametrize("line", [3, 4, 5])
    def test_top_level_function_within_its_range_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "top")), code_root)

        assert locations(frames) == [("/app/app.py", "top", line)]

    @pytest.mark.parametrize("line", [8, 9, 10, 11])
    def test_method_inside_class_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "run")), code_root)

        assert locations(frames) == [("/app/app.py", "run", line)]

    @pytest.mark.parametrize("line", [9, 10])
    def test_nested_function_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "inner")), code_root)

        assert locations(frames) == [("/app/app.py", "inner", line)]

    @pytest.mark.parametrize("line", [13, 14])
    def test_async_function_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "fetch")), code_root)

        assert locations(frames) == [("/app/app.py", "fetch", line)]

    @pytest.mark.parametrize("line", [17, 18])
    def test_second_definition_with_the_same_name_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "run")), code_root)

        assert locations(frames) == [("/app/app.py", "run", line)]

    @pytest.mark.parametrize("line", [1, 6, 20])
    def test_module_level_frame_within_the_file_is_kept(self, code_root, line):
        frames = frames_of(trace(frame_line("/app/app.py", line, "<module>")), code_root)

        assert locations(frames) == [("/app/app.py", "<module>", line)]

    def test_function_in_subdirectory_is_kept(self, code_root):
        frames = frames_of(trace(frame_line("/app/pkg/mod.py", 2, "helper")), code_root)

        assert locations(frames) == [("/app/pkg/mod.py", "helper", 2)]

    def test_whole_real_traceback_is_kept_in_order(self, code_root):
        stack_trace = trace(
            frame_line("/app/app.py", 20, "<module>"),
            frame_line("/app/app.py", 11, "run"),
            frame_line("/usr/local/lib/python3.13/site-packages/anyio/_backends/_asyncio.py", 2461, "run"),
            frame_line("/app/app.py", 10, "inner"),
            frame_line("/app/pkg/mod.py", 2, "helper"),
        )

        frames = frames_of(stack_trace, code_root)

        assert locations(frames) == [
            ("/app/app.py", "<module>", 20),
            ("/app/app.py", "run", 11),
            ("/app/app.py", "inner", 10),
            ("/app/pkg/mod.py", "helper", 2),
        ]

    def test_code_root_can_be_given_as_string(self, code_root):
        frames = frames_of(trace(frame_line("/app/app.py", 4, "top")), str(code_root))

        assert locations(frames) == [("/app/app.py", "top", 4)]

    def test_code_root_reached_through_a_symlink_still_works(self, code_root, tmp_path):
        """根そのものがシンボリックリンクの先にあってもよい（macOS の /tmp など）。"""
        alias = tmp_path / "alias"
        alias.symlink_to(code_root, target_is_directory=True)

        frames = frames_of(trace(frame_line("/app/app.py", 4, "top")), alias)

        assert locations(frames) == [("/app/app.py", "top", 4)]

    def test_real_frames_can_be_selected_as_code_locations(self, code_root):
        from diagnosis.models import CodeLocation

        session, _ = make_session(trace(frame_line("/app/app.py", 11, "run")), code_root)
        event = parse(session.call("list_events", {}))["events"][0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id="EV-1",
                code_location_ids=frame_ids(event),
            ),
        )

        assert session.finalize().code_locations == [CodeLocation(path="/app/app.py", function="run", line=11)]


class TestDefaultCodeRoot:
    """``code_root`` を渡さなければ、``diagnosis`` パッケージの親ディレクトリ（ECS では /app、手元では src/agent）。"""

    def _real_location(self) -> tuple[str, int]:
        from diagnosis import handoff

        lines, start = inspect.getsourcelines(handoff.is_fixer_eligible)
        assert Path(handoff.__file__).name == "handoff.py"
        return "/app/diagnosis/handoff.py", start + len(lines) - 1

    def test_function_of_the_running_code_is_kept(self):
        path, line = self._real_location()

        frames = frames_of(trace(frame_line(path, line, "is_fixer_eligible")), None)

        assert locations(frames) == [(path, "is_fixer_eligible", line)]

    def test_function_that_does_not_exist_in_the_running_code_is_dropped(self):
        path, line = self._real_location()

        assert frames_of(trace(frame_line(path, line, "no_such_function_here")), None) == []

    def test_file_that_does_not_exist_in_the_running_code_is_dropped(self):
        assert frames_of(trace(frame_line("/app/diagnosis/no_such_module.py", 1, "<module>")), None) == []


# ---------------------------------------------------------------------------
# 実際のコードに無い場所は捨てる
# ---------------------------------------------------------------------------


class TestFramesNotInTheCode:
    def test_file_that_does_not_exist_is_dropped(self, code_root):
        assert frames_of(trace(frame_line("/app/missing.py", 1, "top")), code_root) == []

    def test_module_level_frame_of_missing_file_is_dropped(self, code_root):
        assert frames_of(trace(frame_line("/app/missing.py", 1, "<module>")), code_root) == []

    @pytest.mark.parametrize("function", ["pwn", "Top", "top_", "helper", "Worker", "VALUE", "os"])
    def test_function_name_that_is_not_defined_in_the_file_is_dropped(self, code_root, function):
        assert frames_of(trace(frame_line("/app/app.py", 4, function)), code_root) == []

    @pytest.mark.parametrize("line", [1, 2, 6, 7, 20, 999])
    def test_line_outside_the_function_range_is_dropped(self, code_root, line):
        assert frames_of(trace(frame_line("/app/app.py", line, "top")), code_root) == []

    @pytest.mark.parametrize("line", [7, 12, 13, 15, 16, 19])
    def test_line_between_two_definitions_with_the_same_name_is_dropped(self, code_root, line):
        assert frames_of(trace(frame_line("/app/app.py", line, "run")), code_root) == []

    @pytest.mark.parametrize("line", [21, 22, 5000])
    def test_module_level_line_past_the_end_of_file_is_dropped(self, code_root, line):
        assert frames_of(trace(frame_line("/app/app.py", line, "<module>")), code_root) == []

    @pytest.mark.parametrize("function", ["<lambda>", "<listcomp>", "<genexpr>"])
    def test_synthetic_names_other_than_module_are_dropped(self, code_root, function):
        """仕様の読み方: ``<module>`` 以外は、その名前の関数定義（def・async def）が要る。"""
        assert frames_of(trace(frame_line("/app/app.py", 4, function)), code_root) == []

    def test_file_with_syntax_error_is_dropped_without_raising(self, code_root):
        assert frames_of(trace(frame_line("/app/broken.py", 1, "evil")), code_root) == []

    def test_file_that_cannot_be_decoded_is_dropped_without_raising(self, code_root):
        assert frames_of(trace(frame_line("/app/binary.py", 1, "evil")), code_root) == []

    def test_directory_named_like_a_python_file_is_dropped_without_raising(self, code_root):
        assert frames_of(trace(frame_line("/app/dir.py", 1, "<module>")), code_root) == []

    def test_code_root_that_does_not_exist_drops_every_frame_without_raising(self, tmp_path):
        assert frames_of(trace(frame_line("/app/app.py", 4, "top")), tmp_path / "no-such-root") == []

    def test_dropped_frames_do_not_affect_real_ones(self, code_root):
        stack_trace = trace(
            frame_line("/app/missing.py", 1, "top"),
            frame_line("/app/app.py", 4, "top"),
            frame_line("/app/app.py", 4, "pwn"),
            frame_line("/app/broken.py", 1, "evil"),
            frame_line("/app/pkg/mod.py", 1, "helper"),
        )

        frames = frames_of(stack_trace, code_root)

        assert locations(frames) == [("/app/app.py", "top", 4), ("/app/pkg/mod.py", "helper", 1)]

    def test_dropped_frame_ids_cannot_be_selected(self, code_root):
        session, _ = make_session(
            trace(frame_line("/app/app.py", 4, "pwn"), frame_line("/app/app.py", 4, "top")), code_root
        )
        event = parse(session.call("list_events", {}))["events"][0]
        issued = frame_ids(event)
        assert len(issued) == 1
        others = [f"FR-{n}" for n in range(1, 5) if f"FR-{n}" not in issued]

        result = session.call(
            "submit_diagnosis",
            valid_submission(
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id="EV-1",
                code_location_ids=[*issued, *others],
            ),
        )
        diagnosis = session.finalize()

        assert sorted(parse(result)["dropped_code_location_ids"]) == others
        assert [(c.path, c.function, c.line) for c in diagnosis.code_locations] == [("/app/app.py", "top", 4)]


# ---------------------------------------------------------------------------
# 根の外
# ---------------------------------------------------------------------------


class TestFramesOutsideTheCodeRoot:
    @pytest.fixture
    def outside(self, tmp_path) -> Path:
        directory = tmp_path / "outside"
        directory.mkdir()
        (directory / "evil.py").write_text("def evil():\n    return 1\n", encoding="utf-8")
        return directory

    @pytest.mark.parametrize(
        "path",
        ["/app/../outside/evil.py", "/app/pkg/../../outside/evil.py", "/app/./app.py", "/app/pkg/../app.py"],
    )
    def test_path_with_dot_segments_is_dropped(self, code_root, outside, path):
        assert outside.is_dir()

        assert frames_of(trace(frame_line(path, 1, "evil"), frame_line(path, 4, "top")), code_root) == []

    def test_absolute_path_after_the_app_prefix_is_dropped(self, code_root, outside):
        """``/app/`` の後ろが絶対パスになる形（``/app//…``）は通さない。"""
        absolute = f"/app/{outside}/evil.py"
        assert "//" in absolute

        assert frames_of(trace(frame_line(absolute, 1, "evil")), code_root) == []

    def test_double_slash_path_is_dropped(self, code_root):
        """仕様の読み方: ``/app//app.py`` は、``/app/`` の後ろが絶対パス（``/app.py``）なので捨てる。"""
        assert frames_of(trace(frame_line("/app//app.py", 4, "top")), code_root) == []

    def test_path_outside_app_prefix_is_dropped(self, code_root, outside):
        assert frames_of(trace(frame_line(f"{outside}/evil.py", 1, "evil")), code_root) == []

    def test_symlinked_file_pointing_outside_the_root_is_dropped(self, code_root, outside):
        (code_root / "link.py").symlink_to(outside / "evil.py")
        assert (code_root / "link.py").read_text(encoding="utf-8").startswith("def evil")

        assert frames_of(trace(frame_line("/app/link.py", 1, "evil")), code_root) == []
        assert frames_of(trace(frame_line("/app/link.py", 1, "<module>")), code_root) == []

    def test_file_under_symlinked_directory_pointing_outside_the_root_is_dropped(self, code_root, outside):
        (code_root / "linked").symlink_to(outside, target_is_directory=True)
        assert (code_root / "linked" / "evil.py").is_file()

        assert frames_of(trace(frame_line("/app/linked/evil.py", 1, "evil")), code_root) == []

    def test_real_frame_next_to_symlink_escape_is_still_kept(self, code_root, outside):
        (code_root / "link.py").symlink_to(outside / "evil.py")

        frames = frames_of(trace(frame_line("/app/link.py", 1, "evil"), frame_line("/app/app.py", 4, "top")), code_root)

        assert locations(frames) == [("/app/app.py", "top", 4)]


# ---------------------------------------------------------------------------
# 長さの上限（パス 120 字、関数名 80 字）
# ---------------------------------------------------------------------------


class TestLengthLimits:
    def _long_path(self, code_root: Path, total_chars: int) -> str:
        directory = "d" * (total_chars - len("/app/") - len("/m.py"))
        (code_root / directory).mkdir()
        (code_root / directory / "m.py").write_text("def real():\n    return 1\n", encoding="utf-8")
        path = f"/app/{directory}/m.py"
        assert len(path) == total_chars
        return path

    def _long_function(self, code_root: Path, chars: int) -> str:
        name = "f" * chars
        (code_root / "long_names.py").write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
        return name

    def test_path_of_120_characters_is_kept(self, code_root):
        path = self._long_path(code_root, 120)

        assert locations(frames_of(trace(frame_line(path, 2, "real")), code_root)) == [(path, "real", 2)]

    @pytest.mark.parametrize("total_chars", [121, 200])
    def test_path_over_120_characters_is_dropped_even_if_it_exists(self, code_root, total_chars):
        path = self._long_path(code_root, total_chars)

        assert frames_of(trace(frame_line(path, 2, "real")), code_root) == []

    def test_function_name_of_80_characters_is_kept(self, code_root):
        name = self._long_function(code_root, 80)

        frames = frames_of(trace(frame_line("/app/long_names.py", 2, name)), code_root)

        assert locations(frames) == [("/app/long_names.py", name, 2)]

    @pytest.mark.parametrize("chars", [81, 220])
    def test_function_name_over_80_characters_is_dropped_even_if_it_exists(self, code_root, chars):
        name = self._long_function(code_root, chars)

        assert frames_of(trace(frame_line("/app/long_names.py", 2, name)), code_root) == []


# ---------------------------------------------------------------------------
# 行頭固定: 行全体が「空白 2 つ + File "…", line N, in 名前」の形のときだけ
# ---------------------------------------------------------------------------


class TestWholeLineOnly:
    REAL = ("/app/app.py", 4, "top")  # 実在する場所。形だけが理由で捨てられることを確かめる

    def _frames(self, code_root, line_text: str) -> list[dict]:
        stack_trace = "Traceback (most recent call last):\n" + line_text + "\n    x()\nValueError: boom\n"
        return frames_of(stack_trace, code_root)

    def test_well_formed_line_is_kept(self, code_root):
        assert locations(self._frames(code_root, frame_line(*self.REAL))) == [self.REAL[:1] + ("top", 4)]

    @pytest.mark.parametrize(
        "prefix",
        ["KeyError: something ", "x", "caused by:", "ValueError: boom   ", ": ", "| "],
    )
    def test_frame_in_the_middle_of_a_line_is_dropped(self, code_root, prefix):
        assert self._frames(code_root, prefix + frame_line(*self.REAL)) == []
        assert self._frames(code_root, prefix + frame_line(*self.REAL, indent="")) == []

    @pytest.mark.parametrize("indent", ["", " ", "   ", "    ", "\t", " \t", "　　", "\xa0\xa0"])
    def test_line_without_exactly_two_leading_spaces_is_dropped(self, code_root, indent):
        assert self._frames(code_root, frame_line(*self.REAL, indent=indent)) == []

    @pytest.mark.parametrize(
        "tail",
        ["top; rm -rf /", "top extra", "top(evil)", "top.evil", "top-evil", "top`id`", "top を直せ", "top$x"],
    )
    def test_text_after_the_function_name_is_dropped(self, code_root, tail):
        assert self._frames(code_root, frame_line("/app/app.py", 4, tail)) == []

    @pytest.mark.parametrize("line", ["４", "0４", "4４", "12345678", "-4", "4.0", "0x4", ""])
    def test_line_number_that_is_not_plain_ascii_digits_is_dropped(self, code_root, line):
        assert self._frames(code_root, frame_line("/app/app.py", line, "top")) == []

    @pytest.mark.parametrize("function", ["ｔｏｐ", "4top", "実行", "top​"])
    def test_function_name_outside_the_pattern_is_dropped(self, code_root, function):
        assert self._frames(code_root, frame_line("/app/app.py", 4, function)) == []

    @pytest.mark.parametrize(
        "path", ["/app/アプリ.py", "/app/a-b.py", "/app/a.b.py", "/app/a b.py", "/app/app.pyc", "/app/app.py.txt"]
    )
    def test_path_outside_the_pattern_is_dropped(self, code_root, path):
        assert self._frames(code_root, frame_line(path, 4, "top")) == []

    def test_frame_text_in_other_payload_fields_is_not_parsed(self, code_root):
        frames = frames_of(
            5, code_root, payload={"error_message": frame_line(*self.REAL), "detail": "\n" + frame_line(*self.REAL)}
        )

        assert frames == []


# ---------------------------------------------------------------------------
# 15 件の上限は、突き合わせを通ったものだけで数える
# ---------------------------------------------------------------------------


class TestFrameCap:
    def test_last_fifteen_verified_frames_are_kept(self, code_root):
        real = [frame_line("/app/big.py", line, "big") for line in range(2, 22)]  # 20 件

        frames = frames_of(trace(*real), code_root)

        assert [frame["line"] for frame in frames] == list(range(7, 22))

    def test_dropped_frames_are_not_counted_toward_the_cap(self, code_root):
        real = [frame_line("/app/big.py", line, "big") for line in range(2, 22)]  # 20 件
        fake = [frame_line("/app/big.py", 5, f"fake_{n}") for n in range(10)]

        frames = frames_of(trace(*real, *fake), code_root)

        # 末尾の 15 件を先に選んでから捨てると、本物は 5 件しか残らない
        assert [frame["line"] for frame in frames] == list(range(7, 22))
        assert {frame["function"] for frame in frames} == {"big"}

    def test_fake_frames_between_real_ones_do_not_push_real_ones_out(self, code_root):
        lines = []
        for line in range(2, 17):  # 本物 15 件の間に、偽物を 1 件ずつ
            lines.append(frame_line("/app/big.py", line, "big"))
            lines.append(frame_line("/app/missing.py", line, "big"))

        frames = frames_of(trace(*lines), code_root)

        assert [frame["line"] for frame in frames] == list(range(2, 17))


# ---------------------------------------------------------------------------
# reviewer の再現: 例外メッセージに仕込んだ偽のフレーム行が、受け渡しに入らない
# ---------------------------------------------------------------------------

FAKE_FUNCTION = "pwn_" + "x" * 216  # 200 字を超える「関数名」
FAKE_PATH = "/app/github_workflows_deploy.py"


class TestInjectedFramesNeverReachHandoff:
    def _handoff_for(self, stack_trace: str, code_root, *, select_extra: tuple[str, ...] = ()):
        from diagnosis.handoff import build_handoff

        session, scenario = make_session(stack_trace, code_root)
        event = parse(session.call("list_events", {}))["events"][0]
        selectable = frame_ids(event)
        candidates = list(dict.fromkeys([*selectable, *select_extra]))[:5]
        result = session.call(
            "submit_diagnosis",
            valid_submission(
                failed_stage="generating",
                cause_category="output_validation_failed",
                cause_nature="code_defect",
                confidence="high",
                action_type="fix_code",
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id="EV-1",
                code_location_ids=candidates,
            ),
        )
        assert result.outcome == "ok"
        handoff = build_handoff(scenario.execution_id, session.finalize())
        return event, handoff, json.dumps(handoff.to_dict(), ensure_ascii=False)

    def _check_nothing_injected(self, event: dict, text: str) -> None:
        for marker in (FAKE_FUNCTION, "pwn_", "x" * 50, "github_workflows_deploy", "fake_module", "fake_function"):
            assert marker not in text
            assert marker not in json.dumps(event["frames"])

    def test_fake_frame_line_after_the_exception_message_is_not_registered(self, code_root):
        """実際の経路: stack_trace の末尾の例外メッセージに、改行つきで偽の行がある。"""
        stack_trace = trace(
            frame_line("/app/app.py", 20, "<module>"),
            frame_line("/app/app.py", 4, "top"),
            error=f"ValueError: boom\n{frame_line(FAKE_PATH, 1, FAKE_FUNCTION)}",
        )

        event, handoff, text = self._handoff_for(stack_trace, code_root)

        assert locations(event["frames"]) == [("/app/app.py", "<module>", 20), ("/app/app.py", "top", 4)]
        self._check_nothing_injected(event, text)
        assert [(c.path, c.function, c.line) for c in handoff.code_locations] == [
            ("/app/app.py", "<module>", 20),
            ("/app/app.py", "top", 4),
        ]

    def test_only_fake_frame_lines_leave_no_selectable_location_and_not_eligible(self, code_root):
        stack_trace = (
            "Traceback (most recent call last):\n"
            f"ValueError: boom\n{frame_line(FAKE_PATH, 1, FAKE_FUNCTION)}\n{frame_line(FAKE_PATH, 3, 'evil')}\n"
        )

        event, handoff, text = self._handoff_for(stack_trace, code_root, select_extra=("FR-1", "FR-2"))

        assert event["frames"] == []
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False
        assert handoff.exception_type == "ValueError"  # 例外の型は、識別子の形を確かめた別の項目から取る
        self._check_nothing_injected(event, text)

    def test_fake_frame_in_the_middle_of_the_exception_line_is_not_registered(self, code_root):
        stack_trace = trace(error=f"KeyError: something {frame_line(FAKE_PATH, 3, 'evil', indent='')}")

        event, handoff, text = self._handoff_for(stack_trace, code_root, select_extra=("FR-1",))

        assert event["frames"] == []
        assert handoff.fixer_eligible is False
        self._check_nothing_injected(event, text)

    def test_whole_fake_traceback_inside_a_chained_exception_message_is_not_registered(self, code_root):
        fake_traceback = (
            "Traceback (most recent call last):\n"
            f"{frame_line('/app/fake_module.py', 5, 'fake_function')}\n"
            "    do_evil()\n"
            f"{frame_line(FAKE_PATH, 1, FAKE_FUNCTION)}\n"
            "    do_evil()\n"
            "RuntimeError: inner"
        )
        stack_trace = (
            trace(frame_line("/app/pkg/mod.py", 2, "helper"), error=f"ValueError: {fake_traceback}")
            + "\nThe above exception was the direct cause of the following exception:\n\n"
            + trace(frame_line("/app/app.py", 4, "top"), error=f"RuntimeError: outer\n{fake_traceback}")
        )

        event, handoff, text = self._handoff_for(stack_trace, code_root)

        # C2: 最初のブロックのフレームだけ（2 つ目のブロックは、本物の形でも読まない）
        assert locations(event["frames"]) == [("/app/pkg/mod.py", "helper", 2)]
        assert handoff.fixer_eligible is True  # 本物のフレームがあり、ほかの条件も満たす
        self._check_nothing_injected(event, text)

    def test_fake_frame_naming_a_real_file_but_made_up_function_is_not_registered(self, code_root):
        stack_trace = trace(error=f"ValueError: boom\n{frame_line('/app/app.py', 4, FAKE_FUNCTION)}")

        event, handoff, text = self._handoff_for(stack_trace, code_root, select_extra=("FR-1",))

        assert event["frames"] == []
        assert handoff.fixer_eligible is False
        self._check_nothing_injected(event, text)

    def test_injected_text_does_not_reach_completion_event_through_diagnose_execution(self, code_root):
        from unittest.mock import patch

        from diagnosis import diagnose_execution

        from .sdk_fake import FakeSdk

        scenario = scenarios.rate_limited()
        stack_trace = trace(error=f"ValueError: boom\n{frame_line(FAKE_PATH, 1, FAKE_FUNCTION)}")
        scenario.events = [
            scenarios.make_event(
                scenario.execution_id, 1, "error", {"error_type": "ValueError", "stack_trace": stack_trace}
            )
        ]
        bundle = fakes.build_reader(scenario)
        emitter = fakes.FakeEmitter()

        async def script(call) -> None:
            await call("list_events")
            await call(
                "submit_diagnosis",
                valid_submission(
                    cause_category="output_validation_failed",
                    cause_nature="code_defect",
                    confidence="high",
                    action_type="fix_code",
                    evidence=[{"id": "EV-1", "note": "x"}],
                    exception_evidence_id="EV-1",
                    code_location_ids=["FR-1"],
                ),
            )

        with patch("diagnosis.runner._invoke_sdk", FakeSdk(script)):
            run = diagnose_execution(
                scenario.execution_id, reader=bundle.reader, emitter=emitter, code_root=os.fspath(code_root)
            )

        payload_text = json.dumps(emitter.calls[0][1], ensure_ascii=False, default=str)
        assert run.status == "completed"
        assert run.handoff.fixer_eligible is False
        assert tuple(run.handoff.code_locations) == ()
        assert "pwn_" not in payload_text
        assert "github_workflows_deploy" not in payload_text


# ===========================================================================
# クロスレビュー後の修正仕様 C2: フレームは、スタックトレースの最初のブロックからだけ取る
# ===========================================================================
#
# 突き合わせ（上のテスト）だけでは、例外メッセージに「実在する関数の実在する行」を指す行を仕込まれると
# 防げない。フレームを読むのは、1 行目の ``Traceback (most recent call last):`` に続く最初のブロックだけにする。
# ブロックは、フレームの行（空白 2 つ + File "…", line N, in 名前）と、空白 4 つ以上で始まる行が続く間。
# それ以外の行に当たったら、そこで読むのをやめ、後ろは一切拾わない。
#
# 上のテスト（突き合わせ）は、偽の行を最初のブロックの中に置いている。ここでは、実在する場所を指す行を
# ブロックの外に置く。二重の守りを、別々に確かめる。

HEADER = "Traceback (most recent call last):"
REAL_RUN = frame_line("/app/app.py", 11, "run")
REAL_TOP = frame_line("/app/app.py", 4, "top")  # 実在する関数の、実在する行
REAL_HELPER = frame_line("/app/pkg/mod.py", 2, "helper")
REAL_MODULE = frame_line("/app/app.py", 20, "<module>")


def block(*lines: str) -> str:
    return "\n".join([HEADER, *lines])


class TestFirstBlockOnly:
    def test_real_location_injected_into_the_exception_message_is_ignored(self, code_root):
        """窓口の再現: 例外メッセージに、実在する関数の実在する行を指す行を、行頭の形どおりに仕込む。"""
        stack_trace = block(REAL_RUN, "    return inner()", f"ValueError: boom\n{REAL_TOP}\n    x = 1") + "\n"

        assert locations(frames_of(stack_trace, code_root)) == [("/app/app.py", "run", 11)]

    def test_fifteen_injected_real_locations_cannot_push_out_the_real_frames(self, code_root):
        """窓口の再現: 偽のフレームを 15 件仕込んで、本物のフレームを一覧から押し出そうとする。"""
        injected = "\n".join(frame_line("/app/big.py", line, "big") + "\n    pass" for line in range(2, 17))
        stack_trace = block(
            REAL_MODULE, "    VALUE = top()", REAL_RUN, "    return inner()", f"ValueError: boom\n{injected}"
        )

        frames = frames_of(stack_trace + "\n", code_root)

        assert locations(frames) == [("/app/app.py", "<module>", 20), ("/app/app.py", "run", 11)]

    def test_only_injected_real_locations_leave_no_frame_and_not_eligible(self, code_root):
        from diagnosis.handoff import build_handoff

        stack_trace = block(f"ValueError: boom\n{REAL_TOP}\n    x = 1\n{REAL_MODULE}") + "\n"
        session, scenario = make_session(stack_trace, code_root)
        event = parse(session.call("list_events", {}))["events"][0]
        session.call(
            "submit_diagnosis",
            valid_submission(
                cause_category="output_validation_failed",
                cause_nature="code_defect",
                confidence="high",
                action_type="fix_code",
                evidence=[{"id": "EV-1", "note": "x"}],
                exception_evidence_id="EV-1",
                code_location_ids=["FR-1", "FR-2"],
            ),
        )
        handoff = build_handoff(scenario.execution_id, session.finalize())

        assert event["frames"] == []
        assert tuple(handoff.code_locations) == ()
        assert handoff.fixer_eligible is False

    def test_whole_fake_traceback_with_real_locations_inside_the_message_is_ignored(self, code_root):
        fake = block(REAL_TOP, "    x = 1", REAL_HELPER, "    return 1", "RuntimeError: inner")
        stack_trace = block(REAL_RUN, "    return inner()", f"ValueError: {fake}") + "\n"

        assert locations(frames_of(stack_trace, code_root)) == [("/app/app.py", "run", 11)]

    @pytest.mark.parametrize(
        "separator",
        [
            "During handling of the above exception, another exception occurred:",
            "The above exception was the direct cause of the following exception:",
        ],
    )
    def test_chained_exceptions_yield_only_the_first_block(self, code_root, separator):
        first = block(REAL_HELPER, "    return 1", "KeyError: 'x'")
        second = block(REAL_RUN, "    return inner()", REAL_TOP, "    x = 1", "RuntimeError: outer")
        stack_trace = f"{first}\n\n{separator}\n\n{second}\n"

        assert locations(frames_of(stack_trace, code_root)) == [("/app/pkg/mod.py", "helper", 2)]

    @pytest.mark.parametrize(
        "first_line",
        [
            "",
            " " + HEADER,
            HEADER + " ",
            HEADER + "\r",
            HEADER.lower(),
            "Error: " + HEADER,
            HEADER.replace(":", ""),
            "Traceback (most recent call first):",
            "  + Exception Group Traceback (most recent call last):",
            "ValueError: boom",
            REAL_TOP,
        ],
    )
    def test_first_line_that_is_not_exactly_the_traceback_header_yields_no_frames(self, code_root, first_line):
        stack_trace = "\n".join([first_line, REAL_TOP, "    x = 1", "ValueError: boom"]) + "\n"

        assert frames_of(stack_trace, code_root) == []

    def test_header_that_is_not_on_the_first_line_yields_no_frames(self, code_root):
        stack_trace = "\n" + block(REAL_TOP, "    x = 1", "ValueError: boom")

        assert frames_of(stack_trace, code_root) == []
        assert frames_of("some log line\n" + stack_trace, code_root) == []

    def test_header_alone_yields_no_frames(self, code_root):
        assert frames_of(HEADER, code_root) == []
        assert frames_of(HEADER + "\n", code_root) == []

    def test_marker_lines_of_python_313_do_not_end_the_block(self, code_root):
        stack_trace = block(
            REAL_MODULE,
            "    VALUE = top()",
            "            ~~~^^",
            REAL_RUN,
            "    return inner()",
            "           ~~~~~^^",
            "    ^^^^^^^^^^^^^^",
            frame_line("/app/app.py", 10, "inner"),
            "    return 1",
            "ValueError: boom",
        )

        assert locations(frames_of(stack_trace, code_root)) == [
            ("/app/app.py", "<module>", 20),
            ("/app/app.py", "run", 11),
            ("/app/app.py", "inner", 10),
        ]

    def test_frames_without_code_lines_are_read_consecutively(self, code_root):
        stack_trace = block(REAL_MODULE, REAL_RUN, REAL_HELPER, "ValueError: boom")

        assert len(frames_of(stack_trace, code_root)) == 3

    def test_frames_of_other_files_inside_the_block_do_not_end_it(self, code_root):
        """``/app/`` 以外のフレームの行（ライブラリ、``<frozen …>``、``<string>``）も、フレームの行。

        捨てるが、ブロックは終わらせず、読み続ける。
        """
        stack_trace = block(
            '  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load',
            REAL_MODULE,
            "    VALUE = top()",
            '  File "/usr/local/lib/python3.13/site-packages/anyio/_backends/_asyncio.py", line 2461, in run',
            "    return await future",
            '  File "<string>", line 1, in <module>',
            REAL_TOP,
            "    x = 1",
            "ValueError: boom",
        )

        assert locations(frames_of(stack_trace, code_root)) == [
            ("/app/app.py", "<module>", 20),
            ("/app/app.py", "top", 4),
        ]

    @pytest.mark.parametrize(
        "stopper",
        [
            "ValueError: boom",
            "",
            "KeyError",
            " one leading space",
            "   three leading spaces",
            "  [Previous line repeated 996 more times]",
            "  two leading spaces but not a frame",
            "\tx = 1",
            "\u3000\u3000\u3000\u3000全角の空白",
        ],
    )
    def test_any_other_line_ends_the_block(self, code_root, stopper):
        stack_trace = block(REAL_RUN, "    return inner()", stopper, REAL_TOP, "    x = 1", "ValueError: boom")

        assert locations(frames_of(stack_trace, code_root)) == [("/app/app.py", "run", 11)]

    def test_record_cut_in_the_middle_of_the_block_keeps_the_frames_read_so_far(self, code_root):
        """5,000 字で切られた記録: 例外の行まで届いていなくても、読めたところまでのフレームは通る。"""
        whole = block(
            REAL_MODULE, "    VALUE = top()", REAL_RUN, "    return inner()", REAL_TOP, "    x = 1", "ValueError: boom"
        )

        after_frame_line = whole[: whole.index("    return inner()")]  # run のフレームの行の直後で切れる
        in_code_line = whole[: whole.index("    return inner()") + 10]
        in_frame_line = whole[: whole.index(REAL_TOP) + 20]  # top のフレームの行の途中で切れる

        expected = [("/app/app.py", "<module>", 20), ("/app/app.py", "run", 11)]
        assert locations(frames_of(after_frame_line, code_root)) == expected
        assert locations(frames_of(in_code_line, code_root)) == expected
        assert locations(frames_of(in_frame_line, code_root)) == expected

    def test_last_fifteen_frames_of_the_first_block_are_kept(self, code_root):
        real = [frame_line("/app/big.py", line, "big") for line in range(2, 22)]  # 最初のブロックに 20 件
        injected = [frame_line("/app/big.py", line, "big") for line in range(22, 30)]
        stack_trace = block(*real, "ValueError: boom", *injected)

        frames = frames_of(stack_trace, code_root)

        assert [frame["line"] for frame in frames] == list(range(7, 22))

    def test_verification_still_applies_inside_the_first_block(self, code_root):
        """最初のブロックの中でも、実際のコードに無い場所は捨てる（二重の守りの、もう一方）。"""
        stack_trace = block(
            frame_line("/app/missing.py", 1, "top"),
            "    x()",
            REAL_TOP,
            "    x = 1",
            frame_line("/app/app.py", 4, "pwn"),
            "    x()",
            frame_line("/app/app.py", 999, "top"),
            "ValueError: boom",
        )

        assert locations(frames_of(stack_trace, code_root)) == [("/app/app.py", "top", 4)]

    def test_module_level_frame_is_only_read_from_the_first_block(self, code_root):
        """``<module>`` は、ファイル内のどの行でも突き合わせを通る。ブロックの外では、それも拾わない。"""
        injected = "\n".join(frame_line("/app/app.py", line, "<module>") for line in range(1, 16))
        stack_trace = block(REAL_RUN, "    return inner()", f"ValueError: boom\n{injected}")

        assert locations(frames_of(stack_trace, code_root)) == [("/app/app.py", "run", 11)]


# ===========================================================================
# 再レビュー後の修正仕様 A3: 最初のブロックを終える「例外の行」の型
# ===========================================================================
#
# 連鎖した例外（``raise RuntimeError(...) from e``）の ``format_exc()`` では、最初のブロックは内側の
# 例外（KeyError）で終わり、記録される ``error_type`` は外側（RuntimeError）になる。フレームは最初の
# ブロックからしか取らないので、そのままでは「RuntimeError が、KeyError の場所で起きた」と渡ってしまう。
#
# 呼び方（このテストが決めたもの）: ``diagnosis.frames.exception_line_type(stack_trace) -> str | None``
# 最初のブロックを読み終えたところにある例外の行（行頭が空白でない行）から、型の名前
# （点で区切られた最後の部分、``:`` の前）を返す。例外の行まで読めなければ ``None``。例外は出さない。
# ``FrameVerifier.extract(stack_trace, limit)`` の戻り値の形は変えない。


def exception_line_type(stack_trace):
    from diagnosis.frames import exception_line_type as function

    return function(stack_trace)


class TestExceptionLineType:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("KeyError: 'x'", "KeyError"),
            ("ValueError: boom", "ValueError"),
            ("KeyError", "KeyError"),  # メッセージの無い例外
            ("botocore.exceptions.ClientError: An error occurred (AccessDenied)", "ClientError"),
            ("orchestrator.NonDictGeneratorResponse: missing_keys", "NonDictGeneratorResponse"),
            ("orchestrator.NonDictGeneratorResponse", "NonDictGeneratorResponse"),
            ("__main__.MyError: x", "MyError"),
            ("a.b.c.d.DeepError: x", "DeepError"),
            ("ExceptionGroup: several failures (2 sub-exceptions)", "ExceptionGroup"),
            ("KeyError: RuntimeError: nested text", "KeyError"),
            ("ValueError: File \"/app/app.py\", line 4, in top", "ValueError"),
            ("_PrivateError: x", "_PrivateError"),
            ("RuntimeError:", "RuntimeError"),
        ],
    )
    def test_type_name_is_taken_from_the_exception_line(self, line, expected):
        assert exception_line_type(block(REAL_RUN, "    return inner()", line) + "\n") == expected
        assert exception_line_type(block(REAL_RUN, "    return inner()", line)) == expected  # 末尾の改行なし

    def test_type_name_is_taken_when_there_is_no_frame(self):
        assert exception_line_type(block("ValueError: boom")) == "ValueError"

    @pytest.mark.parametrize(
        "separator",
        [
            "During handling of the above exception, another exception occurred:",
            "The above exception was the direct cause of the following exception:",
        ],
    )
    def test_chained_exceptions_yield_the_type_of_the_first_block(self, separator):
        """窓口の再現: 最初のブロックは内側の KeyError で終わる（記録の error_type は、外側の RuntimeError）。"""
        first = block(REAL_HELPER, "    return 1", "KeyError: 'x'")
        second = block(REAL_RUN, "    return inner()", "RuntimeError: outer")

        assert exception_line_type(f"{first}\n\n{separator}\n\n{second}\n") == "KeyError"

    def test_fake_traceback_inside_the_message_does_not_change_the_type(self):
        fake = block(REAL_TOP, "    x = 1", "RuntimeError: inner")

        assert exception_line_type(block(REAL_RUN, "    return inner()", f"ValueError: {fake}")) == "ValueError"

    @pytest.mark.parametrize(
        "stack_trace",
        [
            block(REAL_RUN, "    return inner()"),  # 例外の行の手前で切れている
            block(REAL_RUN, "    return in"),
            block(REAL_RUN)[:-10],  # フレームの行の途中で切れている
            HEADER,
            HEADER + "\n",
            "",
            "ValueError: boom",  # 見出しが無い
            " " + block("ValueError: boom"),
            "\n" + block("ValueError: boom"),
            block(REAL_RUN, "", "ValueError: boom"),  # 空行でブロックが終わる（例外の行ではない）
            block(REAL_RUN, "  two spaces but not a frame", "ValueError: boom"),
            block(REAL_RUN, "\tValueError: boom"),
        ],
    )
    def test_trace_without_a_readable_exception_line_yields_none(self, stack_trace):
        assert exception_line_type(stack_trace) is None

    @pytest.mark.parametrize(
        "line",
        [
            "some free text: x",
            "1Bad: x",
            "pkg.: x",
            ".Leading: x",
            "pkg..Double: x",
            "エラー: x",
            "ＫｅｙＥｒｒｏｒ: x",
            ": x",
            "Key Error",
            "E" * 81 + ": x",
            "KeyError : x",
            "KeyError\r",
        ],
    )
    def test_exception_line_that_does_not_start_with_a_dotted_identifier_yields_none(self, line):
        assert exception_line_type(block(REAL_RUN, "    return inner()", line)) is None

    @pytest.mark.parametrize("value", [None, 5, ["Traceback (most recent call last):", "KeyError"], {"a": 1}, b"x"])
    def test_value_that_is_not_a_string_yields_none(self, value):
        assert exception_line_type(value) is None

    def test_extract_still_returns_a_list_of_code_locations(self, code_root):
        """``FrameVerifier.extract`` の戻り値の形は変えない（修正役のテストが使っている）。"""
        from diagnosis.frames import FrameVerifier
        from diagnosis.models import CodeLocation

        found = FrameVerifier(code_root).extract(block(REAL_RUN, "    return inner()", "KeyError: 'x'"), 15)

        assert found == [CodeLocation(path="/app/app.py", function="run", line=11)]
        assert isinstance(found, list)
