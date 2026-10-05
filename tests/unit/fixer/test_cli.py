"""手動実行の入口（design.md §13。``src/fixer/__main__.py`` の ``main(argv)``）。

対応: AC-27 の入口（pytest の既定実行では、実際の修正役は走らない）、design.md §13 の引数と終了コード。

確かめるのは、引数の処理・出力・終了コードだけ。**実際の修正役は走らせない**: 入口のモジュールを読み込むだけで
（スクリプトとしては実行しない）、``run_fix`` は必ず偽物に差し替える（``_never_run_real_fixer`` が自動で適用される）。

読み方（設計に名前が無い点）: 入口の関数は ``main(argv: list[str] | None = None) -> int``（第 1 段階と同じ）。
入口のモジュールは ``run_fix`` を自分の名前空間に持つ（``from src.fixer import run_fix`` など）。
"""

import importlib
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from .fakes import GATE_ORDER, gate_detail, make_fix_run
from .repo_factory import BRANCH

ENTRY_MODULE = "src.fixer.__main__"


class Cli:
    def __init__(self, module, monkeypatch) -> None:
        self._module = module
        self.run_fix = MagicMock(side_effect=AssertionError("the test did not provide a result"))
        monkeypatch.setattr(module, "run_fix", self.run_fix)

    def run(self, argv: list[str], result) -> object:
        self.run_fix.side_effect = None
        self.run_fix.return_value = result
        return exit_code(lambda: self._module.main(argv))

    def main(self, argv: list[str]) -> object:
        return exit_code(lambda: self._module.main(argv))


@pytest.fixture(autouse=True)
def _never_run_real_fixer(monkeypatch):
    """入口が呼ぶ ``run_fix`` を、必ず偽物にする。ログの設定も触らせない。"""
    module = importlib.import_module(ENTRY_MODULE)
    monkeypatch.setattr(logging, "basicConfig", MagicMock())
    return Cli(module, monkeypatch)


@pytest.fixture
def cli(_never_run_real_fixer) -> Cli:
    return _never_run_real_fixer


def exit_code(action) -> object:
    """終了コード。``main`` が値を返しても、``SystemExit`` で終わっても、同じに扱う。"""
    try:
        return action()
    except SystemExit as exit_request:
        return exit_request.code


def base_argv(tmp_path) -> list[str]:
    return ["--handoff", str(tmp_path / "handoff.json"), "--out-dir", str(tmp_path / "fix-out")]


def called_with(cli: Cli) -> dict:
    """``run_fix`` に渡った引数（位置でもキーワードでもよい）。"""
    args, kwargs = cli.run_fix.call_args
    values = dict(kwargs)
    if args:
        values["handoff_path"] = args[0]
    return values


class TestExitCodes:
    def test_main_proposed_result_exits_zero(self, cli, tmp_path):
        assert cli.run(base_argv(tmp_path), make_fix_run()) == 0

    @pytest.mark.parametrize(
        ("status", "stop_detail"),
        [
            ("rejected", "G5"),
            ("declined", "no_changes"),
            ("incomplete", "timeout"),
            ("not_eligible", "conditions_not_met"),
            ("failed", "invalid_handoff"),
            ("failed", "branch_exists"),
        ],
    )
    def test_main_any_other_result_exits_one(self, cli, tmp_path, status, stop_detail):
        run = make_fix_run(status=status, stop_detail=stop_detail, branch=None, commit=None)

        assert cli.run(base_argv(tmp_path), run) == 1

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--handoff", "h.json"],
            ["--out-dir", "out"],
            ["--handoff", "h.json", "--out-dir", "out", "--unknown-option"],
            ["--handoff", "h.json", "--out-dir", "out", "--push"],
            ["--handoff", "h.json", "--out-dir", "out", "extra-positional"],
            ["--handoff"],
        ],
    )
    def test_main_wrong_arguments_exit_two_without_running(self, cli, argv, capsys):
        """design.md §13: 引数の誤りは 2。修正役は呼ばれない。"""
        assert cli.main(argv) == 2
        cli.run_fix.assert_not_called()


class TestArguments:
    def test_main_passes_handoff_and_out_dir_with_defaults_for_base_and_repo(self, cli, tmp_path):
        cli.run(base_argv(tmp_path), make_fix_run())

        cli.run_fix.assert_called_once()
        values = called_with(cli)
        assert Path(values["handoff_path"]) == tmp_path / "handoff.json"
        assert Path(values["out_dir"]) == tmp_path / "fix-out"
        assert values.get("base", "HEAD") == "HEAD"
        assert Path(values["repo"]).resolve() == Path(".").resolve()

    def test_main_passes_base_and_repo_when_given(self, cli, tmp_path):
        repo = tmp_path / "some-repo"

        cli.run([*base_argv(tmp_path), "--base", "abc1234", "--repo", str(repo)], make_fix_run())

        values = called_with(cli)
        assert values["base"] == "abc1234"
        assert Path(values["repo"]) == repo

    def test_main_does_not_pass_fake_runners_or_a_model_override(self, cli, tmp_path):
        """入口は、実物の pytest・ruff を使う（差し替え口は、テストのためのもの）。"""
        cli.run(base_argv(tmp_path), make_fix_run())

        values = called_with(cli)
        assert values.get("test_runner") is None
        assert values.get("lint_runner") is None


class TestOutput:
    def test_main_prints_status_branch_gates_and_output_directory(self, cli, tmp_path, capsys):
        """design.md §13: 標準出力には、終了理由、ブランチ名、検査の結果、出力先を出す。"""
        cli.run(base_argv(tmp_path), make_fix_run())

        out = capsys.readouterr().out
        assert "proposed" in out
        assert "committed" in out
        assert BRANCH in out
        assert str(tmp_path / "fix-out") in out
        for gate in GATE_ORDER:
            assert gate in out

    def test_main_prints_which_gate_failed(self, cli, tmp_path, capsys):
        from src.fixer.models import GateResult

        gates = [GateResult(gate=gate, passed=gate != "G5", detail=gate_detail(gate)) for gate in GATE_ORDER]
        run = make_fix_run(status="rejected", stop_detail="G5", branch=None, commit=None, gates=gates)

        cli.run(base_argv(tmp_path), run)

        out = capsys.readouterr().out
        assert "rejected" in out
        assert "G5" in out
        assert BRANCH not in out

    def test_main_strips_control_characters_from_the_output(self, cli, tmp_path, capsys):
        """design.md §13: 制御文字を除く（端末のエスケープシーケンスを、そのまま出さない）。"""
        from src.fixer.models import GateResult

        gates = [GateResult(gate="G1", passed=False, detail="bad\x1b[31mdetail\x07")]
        run = make_fix_run(
            status="rejected", stop_detail="G1", branch=None, commit=None, gates=gates, explanation="x\x1b[2Jy\x00"
        )
        argv = ["--handoff", str(tmp_path / "h.json"), "--out-dir", str(tmp_path / "out\x1b[31m")]

        cli.run(argv, run)

        captured = capsys.readouterr()
        for stream in (captured.out, captured.err):
            for char in ("\x1b", "\x07", "\x00"):
                assert char not in stream, repr(char)


class TestUnisolatedOption:
    """レビュー対応 F11: 隔離されていない環境での起動は、明示したときだけ許す。"""

    def test_main_does_not_allow_unisolated_runs_by_default(self, cli, tmp_path):
        cli.run(base_argv(tmp_path), make_fix_run())

        assert called_with(cli).get("allow_unisolated", False) is False

    def test_main_passes_allow_unisolated_when_asked(self, cli, tmp_path):
        code = cli.run([*base_argv(tmp_path), "--allow-unisolated"], make_fix_run())

        assert code == 0
        assert called_with(cli)["allow_unisolated"] is True

    def test_main_prints_the_reason_when_the_environment_is_refused(self, cli, tmp_path, capsys):
        run = make_fix_run(status="failed", stop_detail="unisolated_environment", branch=None, commit=None, gates=[])

        code = cli.run(base_argv(tmp_path), run)

        assert code == 1
        assert "unisolated_environment" in capsys.readouterr().out
