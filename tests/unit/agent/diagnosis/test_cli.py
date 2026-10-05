"""手動実行の入口（``diagnosis/__main__.py`` の ``main(argv)``。design.md §12、§17 の 19）。

確かめるのは、引数の処理・出力・``--handoff-out``・終了コードだけ。**実際の診断は走らせない**:
入口のモジュールを読み込むだけで（スクリプトとしては実行しない）、``diagnose_execution`` は必ず
偽物に差し替える（``_never_run_real_diagnosis`` が自動で適用される。差し替えを忘れたテストは失敗する）。
"""

import importlib
import json
import logging
from unittest.mock import MagicMock

import pytest

from .factories import VALID_EXECUTION_ID, make_run

ENTRY_MODULE = "diagnosis.__main__"
UNKNOWN_SUMMARY_MARKER = "CLI_SUMMARY_MARKER_6120"


class Cli:
    def __init__(self, module, monkeypatch) -> None:
        self._module = module
        self._monkeypatch = monkeypatch
        self.diagnose = MagicMock(side_effect=AssertionError("the test did not provide a diagnosis result"))
        monkeypatch.setattr(module, "diagnose_execution", self.diagnose)

    def run(self, argv: list[str], result) -> int:
        self.diagnose.side_effect = None
        self.diagnose.return_value = result
        return self._module.main(argv)

    def main(self, argv: list[str]) -> int:
        return self._module.main(argv)


@pytest.fixture(autouse=True)
def _never_run_real_diagnosis(monkeypatch):
    """入口が呼ぶ ``diagnose_execution`` を、必ず偽物にする。ログの設定も触らせない。"""
    module = importlib.import_module(ENTRY_MODULE)
    monkeypatch.setattr(logging, "basicConfig", MagicMock())
    return Cli(module, monkeypatch)


@pytest.fixture
def cli(_never_run_real_diagnosis) -> Cli:
    return _never_run_real_diagnosis


def exit_code(action) -> object:
    """終了コード。``main`` が値を返しても、``SystemExit`` で終わっても、同じに扱う。"""
    try:
        return action()
    except SystemExit as exit_request:
        return exit_request.code


def unknown_run(status: str, stop_detail: str):
    from diagnosis.handoff import build_handoff
    from diagnosis.models import Diagnosis

    run = make_run(status=status, stop_detail=stop_detail)
    run.diagnosis = Diagnosis.unknown()
    run.handoff = build_handoff(run.execution_id, run.diagnosis)
    return run


# ---------------------------------------------------------------------------
# §17 の 19: --handoff-out と終了コード
# ---------------------------------------------------------------------------


class TestHandoffOut:
    def test_completed_diagnosis_writes_handoff_file_and_exits_zero(self, cli, tmp_path, capsys):
        run = make_run(summary=UNKNOWN_SUMMARY_MARKER)
        target = tmp_path / "handoff.json"

        code = cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], run)

        assert code == 0
        text = target.read_text(encoding="utf-8")
        assert json.loads(text) == run.handoff.to_dict()
        assert text.endswith("\n")
        assert UNKNOWN_SUMMARY_MARKER not in text  # 受け渡しに自由記述は入らない
        assert "handoff written" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("status", "stop_detail"),
        [
            ("incomplete", "max_turns"),
            ("incomplete", "timeout"),
            ("incomplete", "no_submission"),
            ("incomplete", "max_budget"),
            ("failed", "sdk_error"),
            ("failed", "invalid_execution_id"),
        ],
    )
    def test_diagnosis_that_did_not_complete_writes_no_handoff_and_exits_one(
        self, cli, tmp_path, capsys, status, stop_detail
    ):
        target = tmp_path / "handoff.json"

        code = cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], unknown_run(status, stop_detail))

        assert code == 1
        assert not target.exists()  # 「不明」の既定値を、診断した結果のように渡さない
        assert list(tmp_path.iterdir()) == []
        assert "handoff not written" in capsys.readouterr().err

    @pytest.mark.parametrize("result_status", [("completed", "submitted"), ("failed", "sdk_error")])
    def test_existing_handoff_file_ends_with_exit_two_before_diagnosing(self, cli, tmp_path, result_status):
        """修正仕様: 書き出し先に既にファイルがあれば、診断を始める前に終了コード 2 で終わる。

        古い受け渡しを、修正役が拾わないようにするため。終了の仕方は、``return 2`` でも ``SystemExit(2)`` でもよい。
        """
        target = tmp_path / "handoff.json"
        target.write_text("PREVIOUS CONTENT\n", encoding="utf-8")

        run = make_run(status=result_status[0], stop_detail=result_status[1])

        code = exit_code(lambda: cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], run))

        assert code == 2
        cli.diagnose.assert_not_called()
        assert target.read_text(encoding="utf-8") == "PREVIOUS CONTENT\n"

    def test_existing_handoff_file_is_reported_on_stderr_not_stdout(self, cli, tmp_path, capsys):
        target = tmp_path / "handoff.json"
        target.write_text("{}", encoding="utf-8")

        exit_code(lambda: cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], make_run()))
        captured = capsys.readouterr()

        assert captured.out == ""
        assert captured.err.strip()

    def test_existing_directory_as_handoff_target_ends_with_exit_two_before_diagnosing(self, cli, tmp_path):
        code = exit_code(lambda: cli.run([VALID_EXECUTION_ID, "--handoff-out", str(tmp_path)], make_run()))

        assert code == 2
        cli.diagnose.assert_not_called()

    def test_dangling_symlink_as_handoff_target_ends_with_exit_two_before_diagnosing(self, cli, tmp_path):
        """リンクの先が無くても、書き出し先に「何かがある」。リンクをたどって別の場所に書かない。"""
        target = tmp_path / "handoff.json"
        target.symlink_to(tmp_path / "elsewhere.json")

        code = exit_code(lambda: cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], make_run()))

        assert code == 2
        cli.diagnose.assert_not_called()
        assert not (tmp_path / "elsewhere.json").exists()

    def test_without_handoff_out_no_file_is_written(self, cli, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)

        code = cli.run([VALID_EXECUTION_ID], make_run())

        assert code == 0
        assert list(tmp_path.iterdir()) == []
        assert "handoff" not in capsys.readouterr().err

    def test_handoff_file_carries_fixer_eligibility_decided_by_code(self, cli, tmp_path):
        from diagnosis.models import CodeLocation, EvidenceRef

        run = make_run(
            cause_nature="code_defect",
            confidence="high",
            action_type="fix_code",
            evidence=[EvidenceRef(id="EV-7", note="x")],
            exception_type="NonDictGeneratorResponse",
            exception_evidence_id="EV-7",
            code_locations=[CodeLocation(path="/app/orchestrator.py", function="run", line=10)],
            code_location_evidence_ids=["EV-7"],
        )
        target = tmp_path / "handoff.json"

        cli.run([VALID_EXECUTION_ID, "--handoff-out", str(target)], run)
        data = json.loads(target.read_text(encoding="utf-8"))

        assert data["schema_version"] == 1
        assert data["execution_id"] == VALID_EXECUTION_ID
        assert data["fixer_eligible"] is True
        assert data["exception_type"] == "NonDictGeneratorResponse"
        assert data["code_locations"] == [{"path": "/app/orchestrator.py", "function": "run", "line": 10}]
        assert "EV-7" not in json.dumps(data)


class TestExitCode:
    def test_completed_diagnosis_exits_zero(self, cli):
        assert cli.run([VALID_EXECUTION_ID], make_run()) == 0

    @pytest.mark.parametrize(
        ("status", "stop_detail"),
        [("incomplete", "max_turns"), ("incomplete", "timeout"), ("failed", "sdk_error")],
    )
    def test_diagnosis_that_did_not_complete_exits_one(self, cli, status, stop_detail):
        assert cli.run([VALID_EXECUTION_ID], unknown_run(status, stop_detail)) == 1

    def test_completed_diagnosis_with_unknown_cause_still_exits_zero(self, cli):
        """「分からない」は正当な結果（R10）。提出が受理されていれば completed。"""
        assert cli.run([VALID_EXECUTION_ID], unknown_run("completed", "submitted")) == 0


# ---------------------------------------------------------------------------
# 引数の処理
# ---------------------------------------------------------------------------


class TestArguments:
    def test_execution_id_is_passed_to_diagnosis_once_as_manual_trigger(self, cli):
        cli.run([VALID_EXECUTION_ID], make_run())

        cli.diagnose.assert_called_once_with(VALID_EXECUTION_ID, trigger="manual")

    def test_execution_id_is_passed_through_without_being_rewritten(self, cli):
        """形式の検証は ``diagnose_execution`` の側で行う。入口は値を作り替えない。"""
        cli.run(["not-an-execution-id"], unknown_run("failed", "invalid_execution_id"))

        cli.diagnose.assert_called_once_with("not-an-execution-id", trigger="manual")

    def test_missing_execution_id_exits_with_usage_error_without_diagnosing(self, cli, capsys):
        with pytest.raises(SystemExit) as raised:
            cli.main([])

        assert raised.value.code == 2
        cli.diagnose.assert_not_called()
        assert "execution_id" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            [VALID_EXECUTION_ID, "--unknown-option"],
            [VALID_EXECUTION_ID, "--handoff-out"],
            [VALID_EXECUTION_ID, "exec-20250101000000-ffffffff"],
            [VALID_EXECUTION_ID, "--model", "opus"],
        ],
    )
    def test_invalid_arguments_exit_with_usage_error_without_diagnosing(self, cli, argv):
        with pytest.raises(SystemExit) as raised:
            cli.main(argv)

        assert raised.value.code == 2
        cli.diagnose.assert_not_called()

    def test_help_does_not_diagnose(self, cli, capsys):
        with pytest.raises(SystemExit) as raised:
            cli.main(["--help"])

        assert raised.value.code == 0
        cli.diagnose.assert_not_called()
        out = capsys.readouterr().out
        assert "--handoff-out" in out
        assert "--json" in out

    def test_options_can_come_before_the_execution_id(self, cli, tmp_path):
        target = tmp_path / "handoff.json"

        code = cli.run(["--handoff-out", str(target), "--json", VALID_EXECUTION_ID], make_run())

        assert code == 0
        assert target.exists()
        cli.diagnose.assert_called_once_with(VALID_EXECUTION_ID, trigger="manual")


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


class TestOutput:
    def test_text_output_shows_diagnosis_and_trace(self, cli, capsys):
        run = make_run()

        cli.run([VALID_EXECUTION_ID], run)
        out = capsys.readouterr().out

        assert f"execution_id: {VALID_EXECUTION_ID}" in out
        assert "status: completed (submitted)" in out
        assert "failed_stage: researching" in out
        assert "cause_category: rate_limit" in out
        assert "cause_nature: transient" in out
        assert "confidence: high" in out
        assert "summary: レート制限に 3 回当たって失敗しました" in out
        assert "evidence: 3 valid / 0 invalid" in out
        assert "fixer_eligible: False" in out
        positions = [out.index(name) for name in ("1. get_execution_overview", "2. list_events", "3. submit_diagnosis")]
        assert positions == sorted(positions)
        assert "num_turns: 4" in out

    def test_text_output_lists_code_locations(self, cli, capsys):
        from diagnosis.models import CodeLocation

        run = make_run(code_locations=[CodeLocation(path="/app/orchestrator.py", function="run", line=10)])

        cli.run([VALID_EXECUTION_ID], run)

        assert "code_location: /app/orchestrator.py:10 in run" in capsys.readouterr().out

    def test_json_output_is_parseable_and_contains_result_trace_and_handoff(self, cli, capsys):
        run = make_run()

        code = cli.run([VALID_EXECUTION_ID, "--json"], run)
        data = json.loads(capsys.readouterr().out)

        assert code == 0
        assert data["execution_id"] == VALID_EXECUTION_ID
        assert data["status"] == "completed"
        assert data["stop_detail"] == "submitted"
        assert data["diagnosis"]["cause_category"] == "rate_limit"
        assert data["diagnosis"]["summary"] == "レート制限に 3 回当たって失敗しました"
        assert [call["tool"] for call in data["trace"]] == ["get_execution_overview", "list_events", "submit_diagnosis"]
        assert data["handoff"] == run.handoff.to_dict()
        assert data["usage"] == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 165}

    def test_json_output_keeps_japanese_text_readable(self, cli, capsys):
        cli.run([VALID_EXECUTION_ID, "--json"], make_run())

        assert "レート制限に 3 回当たって失敗しました" in capsys.readouterr().out

    def test_json_output_for_failed_diagnosis_is_still_printed_with_exit_one(self, cli, capsys):
        code = cli.run([VALID_EXECUTION_ID, "--json"], unknown_run("failed", "sdk_error"))
        data = json.loads(capsys.readouterr().out)

        assert code == 1
        assert data["status"] == "failed"
        assert data["diagnosis"]["cause_category"] == "unknown"
        assert data["handoff"]["fixer_eligible"] is False

    def test_logs_go_to_stderr_not_stdout(self, cli):
        import sys

        cli.run([VALID_EXECUTION_ID, "--json"], make_run())

        logging.basicConfig.assert_called_once()
        assert logging.basicConfig.call_args.kwargs["stream"] is sys.stderr


class TestControlCharacters:
    """修正仕様 L7: 端末への出力から、制御文字（改行とタブを除く）を取り除く。"""

    CONTROL = "\x1b[31mRED\x1b[0m\x07\x08\x00\x7f\x9b2J\r"

    def _has_control(self, text: str) -> list[str]:
        import unicodedata

        return [repr(ch) for ch in text if unicodedata.category(ch) == "Cc" and ch not in "\n\t"]

    def test_text_output_strips_control_characters_from_free_text(self, cli, capsys):
        from diagnosis.models import EvidenceRef

        run = make_run(
            summary=f"要約{self.CONTROL}の続き",
            action_note=f"対処{self.CONTROL}の続き",
            evidence=[EvidenceRef(id="EV-2", note=f"根拠{self.CONTROL}の続き")],
        )

        cli.run([VALID_EXECUTION_ID], run)
        out = capsys.readouterr().out

        assert self._has_control(out) == []
        assert "\x1b" not in out
        for word in ("要約", "対処", "根拠", "の続き", "RED"):
            assert word in out  # 取り除くのは制御文字だけ

    def test_text_output_strips_control_characters_from_trace_arguments(self, cli, capsys):
        from diagnosis.models import ToolCall

        run = make_run()
        run.trace = [
            ToolCall(seq=1, tool="list_events", args={"event_type": "x"}, outcome="ok", evidence_count=1, duration_ms=1)
        ]
        run.diagnosis.exception_type = "Bad\x1b[2JType"

        cli.run([VALID_EXECUTION_ID], run)

        assert self._has_control(capsys.readouterr().out) == []

    def test_text_output_keeps_line_structure(self, cli, capsys):
        cli.run([VALID_EXECUTION_ID], make_run(summary=f"要約{self.CONTROL}"))
        out = capsys.readouterr().out

        assert out.count("\n") >= 10  # 改行は残る（1 項目 1 行）
        assert [line for line in out.splitlines() if line.startswith("summary:")] == ["summary: 要約[31mRED[0m2J"]

    def test_json_output_has_no_raw_control_characters(self, cli, capsys):
        cli.run([VALID_EXECUTION_ID, "--json"], make_run(summary=f"要約{self.CONTROL}"))
        out = capsys.readouterr().out

        assert self._has_control(out) == []
        json.loads(out)


class TestGuard:
    def test_entry_point_without_replaced_result_cannot_run_a_real_diagnosis(self, cli):
        """このファイルの土台そのものの確認: 差し替えを忘れると、診断に入る前に失敗する。"""
        with pytest.raises(AssertionError, match="did not provide a diagnosis result"):
            cli.main([VALID_EXECUTION_ID])
