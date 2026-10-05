"""通しの確認（design.md §14）。**実物の git・pytest・ruff** を使う（``test_runner``・``lint_runner`` を渡さない）。

差し替えるのは SDK だけ（偽の修正役が、実物の MCP サーバーを通してツールを呼ぶ）。実際の LLM は呼ばない。

対応: AC-8（テストから見える環境変数。``run_fix`` を通した経路）、AC-14（G-5）、AC-15（G-6）、AC-16（G-7）、
AC-17（G-8）、AC-21（コミット・フック・元のリポジトリ）、AC-24（足取りの件数）、AC-31（きれいな複製）。

T0-1 で確かめた時間: 見本のリポジトリでは、pytest が 1 回 0.1〜0.2 秒、ruff が 0.1 秒未満。
1 件あたり、テスト実行 4〜6 回とリント 2 回で、1〜2 秒。
"""

import json
from pathlib import Path

from .fakes import (
    FINISH_FIXED,
    PARENT_SECRETS,
    RUNTIME_ADDED_KEYS,
    SAFE_ENV_KEYS,
    SECRET_MARK,
    assert_discarded,
    good_fix,
    run_fixer,
)
from .repo_factory import EXECUTION_ID, FILES, FIX_NEW, FIX_OLD, NEW_TEST, NEW_TEST_SOURCE, SRC
from .sdk_fake import FakeSdk, result_message

FIXED_SOURCE = FILES[SRC].replace(FIX_OLD, FIX_NEW)
REAL = {"test_runner": None, "lint_runner": None, "with_status": True}


def failed_gate_details(run) -> str:
    """落ちた検査と、その詳細（失敗したときの表示用）。"""
    return f"{run.status}/{run.stop_detail}: " + "; ".join(
        f"{gate.gate} {gate.detail}" for gate in run.gates if not gate.passed
    )


def script_with(
    *, test_source: str = NEW_TEST_SOURCE, extra_replacements: tuple[tuple[str, str], ...] = (), run_tests: bool = False
):
    """再現テストと修正を書く台本。``extra_replacements`` は、修正に加えて行う置き換え。"""

    async def script(agent) -> None:
        await agent.call("write_file", {"path": NEW_TEST, "content": test_source})
        await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
        for old, new in extra_replacements:
            await agent.call("replace_in_file", {"path": SRC, "old": old, "new": new})
        if run_tests:
            await agent.call("run_tests", {"paths": [NEW_TEST]})
        await agent.call("finish", dict(FINISH_FIXED))

    return script


class TestProposedWithRealTools:
    def test_run_fix_good_fix_passes_every_real_gate_and_is_committed(self, sample_repo, tmp_path):
        """AC-21・AC-24・AC-16: すべての検査に通る差分 → ``proposed``。

        見本のリポジトリには、基準の時点で落ちているテストが 1 件あり、ruff の指摘も 1 件ある
        （どちらも、通過を妨げない）。
        """
        sample_repo.install_hooks()

        outcome = run_fixer(sample_repo, tmp_path, script=good_fix, **REAL)

        run = outcome.run
        assert (run.status, run.stop_detail) == ("proposed", "committed"), [
            (gate.gate, gate.passed, gate.detail) for gate in run.gates
        ]
        assert outcome.sdk.outcomes() == ["ok"] * 6
        assert [(call.tests_passed, call.tests_failed) for call in run.trace if call.tool == "run_tests"] == [
            (0, 1),
            (1, 0),
        ]
        assert {gate.gate for gate in run.gates} == {f"G{number}" for number in range(1, 11)}
        assert all(gate.passed for gate in run.gates)
        assert run.branch == f"fix/diagnosed-{EXECUTION_ID}"
        assert sample_repo.rev(run.branch) == run.commit
        assert sample_repo.parents(run.commit) == [sample_repo.head]
        assert sample_repo.changed_paths(sample_repo.head, run.commit) == sorted([SRC, NEW_TEST])
        assert sample_repo.file_at(run.commit, SRC) == FIXED_SOURCE
        assert sample_repo.file_at(run.commit, NEW_TEST) == NEW_TEST_SOURCE
        assert outcome.before.differences(outcome.after) == []
        assert outcome.after.hook_marks == ()
        assert outcome.after.status == ""
        assert outcome.leftover_temp_dirs == []
        assert not outcome.sdk.root.exists()
        assert sorted(path.name for path in outcome.out_dir.iterdir()) == ["pr-draft.md", "result.json", "trace.json"]

    def test_run_fix_tests_never_see_parent_secrets_in_any_run(self, sample_repo, tmp_path, monkeypatch):
        """AC-8: 修正役が走らせるテストでも、検査のテストでも、親の環境変数の目印の値は見えない。"""
        for name in PARENT_SECRETS:
            monkeypatch.setenv(name, f"{SECRET_MARK}-{name}")
        dump = tmp_path / "env-dump.jsonl"
        test_source = (
            "import json\nimport os\n\nfrom orchestrator import average\n\n\n"
            "def test_average_of_empty_list_is_zero():\n"
            "    home = os.environ.get('HOME', '')\n"
            "    record = {'env': dict(os.environ), 'cwd': os.getcwd(),\n"
            "              'home_listing': sorted(os.listdir(home)) if os.path.isdir(home) else None}\n"
            f"    with open({str(dump)!r}, 'a', encoding='utf-8') as file:  # noqa: E501\n"
            "        file.write(json.dumps(record) + '\\n')\n"
            "    assert average([]) == 0.0\n"
        )

        outcome = run_fixer(sample_repo, tmp_path, script=script_with(test_source=test_source, run_tests=True), **REAL)

        assert outcome.run.status == "proposed", failed_gate_details(outcome.run)
        records = [json.loads(line) for line in dump.read_text(encoding="utf-8").splitlines()]
        assert len(records) >= 3  # 修正役のテスト実行 1 回と、検査の B・D
        for record in records:
            env = record["env"]
            assert {name: value for name, value in env.items() if SECRET_MARK in value} == {}
            assert set(env) <= SAFE_ENV_KEYS | RUNTIME_ADDED_KEYS, sorted(set(env) - SAFE_ENV_KEYS - RUNTIME_ADDED_KEYS)
            assert env["AWS_SECRET_ACCESS_KEY"] == "dummy"
            assert record["home_listing"] == []
            assert not Path(env["HOME"]).exists()
            assert not Path(record["cwd"]).resolve().is_relative_to(sample_repo.path.resolve())
        working_directories = {record["cwd"] for record in records}
        assert len(working_directories) >= 3  # 作業した複製、T_pre、T_fix
        assert all(not Path(directory).exists() for directory in working_directories)


class TestRejectedWithRealTools:
    def test_run_fix_new_test_that_passes_before_the_fix_is_rejected_by_g5(self, sample_repo, tmp_path):
        """AC-14: 新しいテストが修正前でも通る（不具合を再現していない）。"""
        test_source = (
            "from orchestrator import average\n\n\ndef test_average_of_two_values():\n    assert average([2, 4]) == 3\n"
        )

        outcome = run_fixer(sample_repo, tmp_path, script=script_with(test_source=test_source), **REAL)

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G5")
        assert_discarded(outcome)

    def test_run_fix_new_test_that_fails_after_the_fix_is_rejected_by_g6_whatever_the_model_says(
        self, sample_repo, tmp_path
    ):
        """AC-15: 新しいテストが修正後も落ちる。モデルが「すべて通った」と述べていても結果は変わらない。"""
        test_source = (
            "from orchestrator import average\n\n\ndef test_average_of_empty_list():\n    assert average([]) == 1.5\n"
        )
        sdk = FakeSdk(
            script=script_with(test_source=test_source),
            final_text="すべてのテストが通りました。",
            result=result_message(result="All tests passed."),
        )

        outcome = run_fixer(sample_repo, tmp_path, sdk=sdk, **REAL)

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G6")
        assert_discarded(outcome)

    def test_run_fix_fix_that_breaks_an_existing_test_is_rejected_by_g7(self, sample_repo, tmp_path):
        """AC-16: 修正で既存のテストが 1 件落ちるようになる。"""
        breaking = (('    return "item:" + name\n', '    return "thing:" + name\n'),)

        outcome = run_fixer(sample_repo, tmp_path, script=script_with(extra_replacements=breaking), **REAL)

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G7")
        assert_discarded(outcome)

    def test_run_fix_fix_that_adds_a_lint_finding_is_rejected_by_g8(self, sample_repo, tmp_path):
        """AC-17: 修正で ``ruff`` の指摘が 1 件増える（使っていない import）。"""
        unused_import = (('"""\n\n\nclass WorkflowError', '"""\n\nimport sys\n\n\nclass WorkflowError'),)

        outcome = run_fixer(sample_repo, tmp_path, script=script_with(extra_replacements=unused_import), **REAL)

        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G8")
        assert_discarded(outcome)


class TestCleanTreesWithRealTools:
    def test_run_fix_file_left_in_the_workspace_cannot_make_the_gates_pass(self, sample_repo, tmp_path):
        """AC-31: 検査は、作業した複製ではなく、組み立て直した複製で走る。

        新しいテストは、キャッシュのディレクトリに残したファイルがあるときだけ通る。作業した複製では通るが、
        検査の複製にはそのファイルが無いので、G-6 に落ちる。
        """
        test_source = (
            "from pathlib import Path\n\nfrom orchestrator import average\n\n\n"
            "def test_average_of_empty_list_is_zero():\n"
            "    assert average([]) == 0.0\n"
            "    assert (Path(__file__).parent / '__pycache__' / 'leftover.pyc').exists()\n"
        )

        async def script(agent) -> None:
            await agent.call("write_file", {"path": NEW_TEST, "content": test_source})
            await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
            leftover = agent.root / "tests" / "unit" / "__pycache__" / "leftover.pyc"
            leftover.parent.mkdir(exist_ok=True)
            leftover.write_bytes(b"left by the test code")
            await agent.call("run_tests", {"paths": [NEW_TEST]})
            await agent.call("finish", dict(FINISH_FIXED))

        outcome = run_fixer(sample_repo, tmp_path, script=script, **REAL)

        in_workspace = [
            (call.tests_passed, call.tests_failed) for call in outcome.run.trace if call.tool == "run_tests"
        ]
        assert in_workspace == [(1, 0)]  # 作業した複製では通る
        assert (outcome.run.status, outcome.run.stop_detail) == ("rejected", "G6")
        assert_discarded(outcome)
