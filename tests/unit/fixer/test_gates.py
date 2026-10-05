"""合否の検査（design.md §10）。``check_static`` と ``check_dynamic`` を、実物のテスト実行なしで確かめる。

対応: AC-11（G-1）、AC-12（G-2）、AC-13（G-3）、AC-14（G-4・G-5）、AC-15（G-6）、AC-16（G-7）、AC-17（G-8）、
AC-18（G-9）、AC-19（G-10）。``run_fix`` を通した確認（``rejected``、破棄）は ``test_runner.py``、
実物の pytest・ruff を使う確認は ``test_end_to_end.py``。

静的な検査の ``ChangeSet`` は、**実物の複製を直接書き換えて ``Workspace.changes()`` で取り出す**
（修正役が走らせたテストのコードが書き換えた、という状況。手で組み立てた値だけでは、取り出しの側の
見落としが隠れるため）。手で組み立てた ``ChangeSet`` でも、数件を確かめる。

読み方: ``README.md`` の変更は G-1 に加えて G-10（拡張子が ``.py`` でない）にも落ちうるので、
「その検査が、落ちた検査に含まれる」ことを確かめる。
"""

from pathlib import Path

import pytest

from ..agent.diagnosis.factories import fake_tokens
from . import fakes
from .fakes import (
    all_of,
    append_text,
    apply_good_fix_directly,
    chmod,
    delete,
    make_report,
    symlink,
    write_bytes,
    write_text,
)
from .repo_factory import (
    EXISTING_CONFTEST,
    EXISTING_TEST,
    FILES,
    FIX_NEW,
    FIX_OLD,
    LEGACY,
    NEW_TEST,
    NEW_TEST_SOURCE,
    SRC,
)

STATIC_GATES = {"G1", "G2", "G3", "G4", "G9", "G10"}
DYNAMIC_GATES = {"G5", "G6", "G7", "G8"}
FIXED_SOURCE = FILES[SRC].replace(FIX_OLD, FIX_NEW)
A_TEST = "def test_extra():\n    assert True\n"
# 正しい修正の行数: 修正で 2 行足し、新しいテストファイルが 5 行
GOOD_FIX_LINES = 7


def static_results(workspace, tamper=None, *, with_good_fix=True, limits=None):
    from src.fixer.gates import check_static
    from src.fixer.models import FixLimits

    root = Path(workspace.root)
    if with_good_fix:
        apply_good_fix_directly(root)
    if tamper is not None:
        tamper(root)
    return check_static(workspace.changes(), workspace.baseline, limits or FixLimits())


def failed(results) -> set[str]:
    return {result.gate for result in results if not result.passed}


def dynamic_results(**overrides):
    from src.fixer.gates import check_dynamic

    existing = dict(fakes.EXISTING_IDS)
    arguments = {
        "pre_fix_new": make_report({"tests.unit.test_new::test_case": "failed"}),
        "post_fix_new": make_report({"tests.unit.test_new::test_case": "passed"}),
        "base_existing": make_report(existing),
        "post_fix_existing": make_report(dict(existing)),
        "lint_before": 1,
        "lint_after": 1,
    }
    arguments.update(overrides)
    return check_dynamic(**arguments)


def gate(results, gate_id):
    matching = [result for result in results if result.gate == gate_id]
    assert len(matching) == 1, f"{gate_id} appears {len(matching)} times"
    return matching[0]


# ---------------------------------------------------------------------------
# 静的な検査
# ---------------------------------------------------------------------------


class TestStaticResultShape:
    def test_check_static_good_fix_passes_all_six_static_gates(self, workspace):
        results = static_results(workspace)

        assert {result.gate for result in results} == STATIC_GATES
        assert len(results) == len(STATIC_GATES)
        assert failed(results) == set()
        for result in results:
            assert result.passed is True
            assert isinstance(result.detail, str)

    def test_check_static_reports_every_gate_even_when_several_fail(self, workspace):
        tamper = all_of(write_text("README.md", "# changed\n"), append_text(EXISTING_TEST, "\n# changed\n"))

        results = static_results(workspace, tamper)

        assert {result.gate for result in results} == STATIC_GATES
        assert {"G1", "G3"} <= failed(results)

    def test_check_static_with_hand_built_change_set_passes(self, workspace):
        from src.fixer.gates import check_static
        from src.fixer.models import FixLimits
        from src.fixer.workspace import ChangeSet

        changes = ChangeSet(
            added={NEW_TEST: NEW_TEST_SOURCE.encode()},
            modified={SRC: FIXED_SOURCE.encode()},
            deleted=(),
            irregular=(),
            oversized=(),
        )

        assert failed(check_static(changes, workspace.baseline, FixLimits())) == set()

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("deleted", (LEGACY,), "G1"),
            ("irregular", ("src/agent/link.py",), "G10"),
            ("oversized", ("src/agent/big.py",), "G9"),
        ],
    )
    def test_check_static_with_hand_built_irregular_change_set_fails(self, workspace, field, value, expected):
        from src.fixer.gates import check_static
        from src.fixer.models import FixLimits
        from src.fixer.workspace import ChangeSet

        fields = {
            "added": {NEW_TEST: NEW_TEST_SOURCE.encode()},
            "modified": {SRC: FIXED_SOURCE.encode()},
            "deleted": (),
            "irregular": (),
            "oversized": (),
        }
        fields[field] = value

        assert expected in failed(check_static(ChangeSet(**fields), workspace.baseline, FixLimits()))


class TestG1Scope:
    @pytest.mark.parametrize(
        "tamper",
        [
            write_text("README.md", "# changed\n"),
            write_text("notes.txt", "x\n"),
            write_text("scripts/tool.py", "X = 1\n"),
            write_text("new_top_level.py", "X = 1\n"),
            write_text(".env", "X=1\n"),
            write_text(".git/hooks/pre-commit", "#!/bin/sh\n"),
            write_text("conftest.py", "import os\n"),
            write_text("srcx/module.py", "X = 1\n"),
            delete("README.md"),
            delete(LEGACY),
            delete("src/agent/storage/__init__.py"),
        ],
        ids=[
            "readme",
            "new-text",
            "scripts",
            "top-level-py",
            "dotenv",
            "dot-git",
            "root-conftest",
            "srcx",
            "delete-readme",
            "delete-src",
            "delete-empty-init",
        ],
    )
    def test_check_static_change_outside_src_and_tests_or_deletion_fails_g1(self, workspace, tamper):
        """AC-11: ``README.md`` の変更など。正しい修正と一緒でも落ちる。削除は認めない。"""
        assert "G1" in failed(static_results(workspace, tamper))


FORBIDDEN_CHANGES = {
    # AC-12 に書かれた場所
    "template.yaml": write_text("template.yaml", "Resources: {X: 1}\n"),
    ".github/workflows/x.yml": write_text(".github/workflows/x.yml", "name: x\n"),
    "CLAUDE.md": write_text("CLAUDE.md", "# weaker rules\n"),
    ".steering/x/requirements.md": write_text(".steering/x/requirements.md", "# changed\n"),
    "src/agent/requirements.txt": write_text("src/agent/requirements.txt", "evil-package\n"),
    "src/fixer/gates.py": write_text("src/fixer/gates.py", "def check_static():\n    return ['always pass']\n"),
    "src/agent/diagnosis/tools.py": write_text("src/agent/diagnosis/tools.py", "def call(name):\n    return None\n"),
    # requirements.md §4.3 の G2 と design.md §6.2 の一覧の、ほかの場所
    "docs/guide.md": write_text("docs/guide.md", "# changed\n"),
    "Dockerfile": write_text("Dockerfile", "FROM scratch\n"),
    "src/agent/Dockerfile": write_text("src/agent/Dockerfile", "FROM evil\n"),
    "pyproject.toml": append_text("pyproject.toml", "\n# changed\n"),
    "requirements.txt": write_text("requirements.txt", "evil-package\n"),
    ".gitleaks.toml": write_text(".gitleaks.toml", 'title = "off"\n'),
    "src/fixer/new_module.py": write_text("src/fixer/new_module.py", "X = 1\n"),
    "src/agent/diagnosis/new_module.py": write_text("src/agent/diagnosis/new_module.py", "X = 1\n"),
    "src/agent/prompts/builder.py": write_text("src/agent/prompts/builder.py", "def build():\n    return 'x'\n"),
    "src/agent/main.py": write_text("src/agent/main.py", "def setup_credentials():\n    return 1\n"),
    "src/trigger/app.py": append_text("src/trigger/app.py", "X = 1\n"),
    "src/trigger/slack_verify.py": write_text(
        "src/trigger/slack_verify.py", "def verify(signature):\n    return True\n"
    ),
    "src/token_monitor/handler.py": append_text("src/token_monitor/handler.py", "X = 1\n"),
    "src/dashboard_api/authorizer/app.py": append_text("src/dashboard_api/authorizer/app.py", "X = 1\n"),
    "src/dashboard_api/auth_login/app.py": append_text("src/dashboard_api/auth_login/app.py", "X = 1\n"),
    "src/dashboard_api/auth_me/app.py": write_text("src/dashboard_api/auth_me/app.py", "X = 1\n"),
    "tests/unit/fixer/test_new.py": write_text("tests/unit/fixer/test_new.py", A_TEST),
    "tests/unit/agent/diagnosis/test_new.py": write_text("tests/unit/agent/diagnosis/test_new.py", A_TEST),
    "src/agent/conftest.py": write_text("src/agent/conftest.py", "import os\n"),
    "src/agent/sitecustomize.py": write_text("src/agent/sitecustomize.py", "import os\n"),
    "src/usercustomize.py": write_text("src/usercustomize.py", "import os\n"),
    "src/trigger/requirements-dev.txt": write_text("src/trigger/requirements-dev.txt", "evil-package\n"),
    "src/trigger/Dockerfile": write_text("src/trigger/Dockerfile", "FROM evil\n"),
    "delete src/agent/main.py": delete("src/agent/main.py"),
}


class TestG2ForbiddenPlaces:
    @pytest.mark.parametrize("name", list(FORBIDDEN_CHANGES))
    def test_check_static_change_in_forbidden_place_fails_g2_even_with_a_good_fix(self, workspace, name):
        """AC-12: 禁止の場所への変更。正しい修正と一緒に混ざっていても落ちる。"""
        assert "G2" in failed(static_results(workspace, FORBIDDEN_CHANGES[name]))

    @pytest.mark.parametrize(
        "rel",
        [
            "src/agent/diagnosis_notes.py",
            "src/fixer_helper.py",
            "src/agent/storage/notion_client.py",
            "src/dashboard_api/executions/app.py",
            "src/observability/emitter.py",
            "src/agent/maintenance.py",
        ],
    )
    def test_check_static_neighbour_of_a_forbidden_place_passes_g2(self, workspace, rel):
        """対象外の一覧が広すぎない（前方一致・完全一致の境目）。"""
        results = static_results(workspace, write_text(rel, "VALUE = 1\n"))

        assert "G2" not in failed(results)
        assert "G1" not in failed(results)

    def test_forbidden_lists_are_shared_constants_in_gates(self):
        """design.md §6.2: 場所の再検証・書き込みのツール・G-2 が、同じ一覧を使う。"""
        from src.fixer import gates

        prefixes = tuple(gates.FORBIDDEN_PREFIXES)
        for prefix in ("src/fixer/", "src/agent/diagnosis/", "src/agent/prompts/", "src/token_monitor/"):
            assert prefix in prefixes
        for prefix in ("src/dashboard_api/authorizer/", "src/dashboard_api/auth_", "tests/unit/fixer/"):
            assert prefix in prefixes
        assert "tests/unit/agent/diagnosis/" in prefixes
        assert len(tuple(gates.FORBIDDEN_NAMES)) >= 1


class TestG3ExistingTests:
    @pytest.mark.parametrize(
        "tamper",
        [
            append_text(EXISTING_TEST, "\n\ndef test_extra():\n    assert True\n"),
            write_text(EXISTING_TEST, "def test_average_returns_mean():\n    assert True\n"),
            delete(EXISTING_TEST),
            append_text(EXISTING_CONFTEST, "\n# changed\n"),
            delete(EXISTING_CONFTEST),
            write_text("tests/unit/__init__.py", "import os\n"),
            write_text("tests/unit/new_package/conftest.py", "import pytest\n"),
            write_text("tests/conftest.py", "collect_ignore = ['unit/test_orchestrator.py']\n"),
            write_text("tests/unit/new_package/__init__.py", "import os\n"),
            write_text("tests/unit/helper.py", "X = 1\n"),
            write_text("tests/unit/pytest.ini", "[pytest]\naddopts = -k nothing\n"),
            write_text("tests/unit/tox.ini", "[pytest]\n"),
            write_text("tests/unit/data.json", "{}\n"),
            write_text("tests/unit/testing.py", A_TEST),
        ],
        ids=[
            "append-existing-test",
            "rewrite-existing-test",
            "delete-existing-test",
            "modify-conftest",
            "delete-conftest",
            "fill-existing-init",
            "new-conftest",
            "new-top-conftest",
            "new-init-with-code",
            "new-helper",
            "pytest-ini",
            "tox-ini",
            "data-file",
            "not-test-prefix",
        ],
    )
    def test_check_static_change_to_existing_tests_or_non_test_addition_fails_g3(self, workspace, tamper):
        """AC-13: 既存のテスト・``conftest.py`` の変更と削除、``test_*.py`` と空の ``__init__.py`` 以外の追加。"""
        assert "G3" in failed(static_results(workspace, tamper))

    def test_check_static_new_test_package_with_empty_init_passes_g3(self, workspace):
        tamper = all_of(
            write_text("tests/unit/new_package/__init__.py", ""),
            write_text("tests/unit/new_package/test_more.py", A_TEST),
        )

        assert failed(static_results(workspace, tamper)) == set()


class TestG4ReproductionTest:
    def test_check_static_fix_without_new_test_fails_g4(self, workspace):
        """AC-14: 新しいテストがない。"""

        def only_fix(root):
            (root / SRC).write_text(FIXED_SOURCE, encoding="utf-8")

        assert "G4" in failed(static_results(workspace, only_fix, with_good_fix=False))

    @pytest.mark.parametrize(
        "rel",
        ["tests/unit/helper.py", "tests/unit/new_package/__init__.py", "src/agent/test_in_src.py", "test_at_top.py"],
    )
    def test_check_static_new_file_that_is_not_a_test_file_under_tests_does_not_satisfy_g4(self, workspace, rel):
        def tamper(root):
            (root / SRC).write_text(FIXED_SOURCE, encoding="utf-8")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("" if rel.endswith("__init__.py") else A_TEST, encoding="utf-8")

        assert "G4" in failed(static_results(workspace, tamper, with_good_fix=False))

    def test_check_static_new_test_alone_satisfies_g4(self, workspace):
        results = static_results(workspace, write_text(NEW_TEST, NEW_TEST_SOURCE), with_good_fix=False)

        assert "G4" not in failed(results)


class TestG9Size:
    def test_check_static_good_fix_is_exactly_seven_changed_lines(self, workspace):
        """行数の境界: 足した行＋消した行が、上限ちょうどなら通り、1 行でも超えれば落ちる。"""
        from src.fixer.models import FixLimits

        assert "G9" not in failed(static_results(workspace, limits=FixLimits(max_changed_lines=GOOD_FIX_LINES)))

    def test_check_static_one_line_over_the_limit_fails_g9(self, workspace):
        from src.fixer.models import FixLimits

        assert "G9" in failed(static_results(workspace, limits=FixLimits(max_changed_lines=GOOD_FIX_LINES - 1)))

    def test_check_static_removed_lines_are_counted(self, workspace):
        """消した行も数える（``legacy.py`` の 5 行のうち、末尾の 3 行を消す）。"""
        from src.fixer.models import FixLimits

        shortened = "".join(FILES[LEGACY].splitlines(keepends=True)[:2])
        tamper = write_text(LEGACY, shortened)

        assert "G9" not in failed(
            static_results(workspace, tamper, limits=FixLimits(max_changed_lines=GOOD_FIX_LINES + 3))
        )

    def test_check_static_removed_lines_over_the_limit_fail_g9(self, workspace):
        from src.fixer.models import FixLimits

        shortened = "".join(FILES[LEGACY].splitlines(keepends=True)[:2])
        tamper = write_text(LEGACY, shortened)

        assert "G9" in failed(static_results(workspace, tamper, limits=FixLimits(max_changed_lines=GOOD_FIX_LINES + 2)))

    @pytest.mark.parametrize(("padding_lines", "passes"), [(193, True), (194, False)])
    def test_check_static_default_limit_is_200_changed_lines(self, workspace, padding_lines, passes):
        """AC-18: 既定の上限（200 行）。7 行＋193 行は通り、7 行＋194 行は落ちる。"""
        body = "# padding\n" * (padding_lines - 2) + "def test_padding():\n    assert True\n"
        results = static_results(workspace, write_text("tests/unit/test_padding.py", body))

        assert ("G9" not in failed(results)) is passes

    @pytest.mark.parametrize(("extra_tests", "passes"), [(3, True), (4, False)])
    def test_check_static_default_limit_is_five_changed_files(self, workspace, extra_tests, passes):
        """AC-18: 既定の上限（5 ファイル）。修正 1 ＋ 新しいテスト 1 ＋ 追加ぶん。"""
        tamper = all_of(*(write_text(f"tests/unit/test_more_{index}.py", A_TEST) for index in range(extra_tests)))

        results = static_results(workspace, tamper)

        assert ("G9" not in failed(results)) is passes

    def test_check_static_empty_init_files_are_not_counted_as_changed_files(self, workspace):
        """design.md §18: 空の ``__init__.py`` は、ファイル数に数えない。"""
        tamper = all_of(
            *(write_text(f"tests/unit/test_more_{index}.py", A_TEST) for index in range(2)),
            write_text("tests/unit/pkg_a/__init__.py", ""),
            write_text("tests/unit/pkg_a/test_inside.py", A_TEST),
            write_text("tests/unit/pkg_b/__init__.py", ""),
        )

        assert "G9" not in failed(static_results(workspace, tamper))

    def test_check_static_oversized_file_fails_g9(self, workspace):
        tamper = write_bytes("src/agent/big.py", b"#" * 200_001)

        assert "G9" in failed(static_results(workspace, tamper))

    def test_check_static_one_very_long_line_over_the_write_limit_fails_g9(self, workspace):
        """上限のすり抜け: 行数では 1 行でも、大きさの上限を超える。"""
        tamper = write_text("tests/unit/test_long_line.py", "X = '" + "a" * 250_000 + "'\n")

        assert "G9" in failed(static_results(workspace, tamper))


def token_cases() -> dict[str, str]:
    tokens = fake_tokens()
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "IBAAKCAQEA\n-----END " + "RSA PRIVATE KEY-----"
    return {**tokens, "pem": pem}


class TestG10Content:
    @pytest.mark.parametrize(
        "tamper",
        [
            symlink("src/agent/link.py", "orchestrator.py"),
            symlink("src/agent/link.py", "/etc/hosts"),
            symlink("tests/unit/test_link.py", "test_orchestrator.py"),
            symlink("src/agent/linkdir", "storage"),
            chmod(SRC, 0o755),
            chmod(NEW_TEST, 0o755),
            chmod(LEGACY, 0o744),
            write_bytes("src/agent/blob.py", b"\xff\xfe\x00\x01binary"),
            write_bytes("src/agent/latin.py", "x = 'caf\xe9'\n".encode("latin-1")),
            write_bytes("src/agent/nul.py", b"X = 1\n\x00"),
            write_bytes(NEW_TEST, NEW_TEST_SOURCE.encode() + b"\x00"),
            write_text("src/agent/data.json", "{}\n"),
            write_text("src/agent/script.sh", "#!/bin/sh\n"),
            write_text("src/agent/module.py.txt", "X = 1\n"),
            write_text("tests/unit/data.bin", "x\n"),
        ],
        ids=[
            "symlink-inside",
            "symlink-outside",
            "symlink-test",
            "symlink-dir",
            "exec-on-source",
            "exec-on-new-test",
            "exec-on-untouched",
            "binary",
            "not-utf8",
            "nul-in-source",
            "nul-in-new-test",
            "json-in-src",
            "shell-script",
            "double-extension",
            "non-python-in-tests",
        ],
    )
    def test_check_static_irregular_or_non_text_change_fails_g10(self, workspace, tamper):
        """AC-19: シンボリックリンクの追加、実行権限の変更、バイナリ、``.py`` 以外。"""
        assert "G10" in failed(static_results(workspace, tamper))

    @pytest.mark.parametrize("kind", list(token_cases()))
    @pytest.mark.parametrize("target", [SRC, NEW_TEST, "tests/unit/test_secret.py"])
    def test_check_static_token_shaped_string_fails_g10_without_echoing_it(self, workspace, kind, target):
        """AC-19: トークンの形をした文字列の追加。検査の結果の文に、その文字列を入れない。"""
        token = token_cases()[kind]
        line = f'\nSECRET = """{token}"""\n'
        tamper = write_text(target, A_TEST + line) if target.endswith("test_secret.py") else append_text(target, line)

        results = static_results(workspace, tamper)

        assert "G10" in failed(results)
        for result in results:
            assert token not in result.detail
            assert token[:16] not in result.detail

    def test_check_static_detail_is_short_even_for_many_violations(self, workspace):
        tamper = all_of(*(write_text(f"junk/file_{index}.txt", "x\n") for index in range(300)))

        results = static_results(workspace, tamper)

        assert "G1" in failed(results)
        for result in results:
            assert len(result.detail) <= 2_000


# ---------------------------------------------------------------------------
# 動的な検査
# ---------------------------------------------------------------------------

NEW_ID = "tests.unit.test_new::test_case"
EXISTING = dict(fakes.EXISTING_IDS)
FLIPPED = {**EXISTING, "tests.unit.test_orchestrator::test_label_prefixes_name": "failed"}


class TestDynamicResultShape:
    def test_check_dynamic_all_conditions_met_passes_the_four_dynamic_gates(self):
        results = dynamic_results()

        assert {result.gate for result in results} == DYNAMIC_GATES
        assert len(results) == len(DYNAMIC_GATES)
        assert failed(results) == set()

    def test_check_dynamic_reports_every_gate_independently(self):
        results = dynamic_results(
            pre_fix_new=make_report({NEW_ID: "passed"}),
            post_fix_new=make_report({NEW_ID: "failed"}),
            post_fix_existing=make_report(FLIPPED),
            lint_after=2,
        )

        assert failed(results) == DYNAMIC_GATES


class TestG5FailsBeforeFix:
    @pytest.mark.parametrize(
        "report",
        [
            lambda: make_report({NEW_ID: "failed"}),
            lambda: make_report({NEW_ID: "error"}),
            lambda: make_report({NEW_ID: "failed", "tests.unit.test_new::test_other": "passed"}),
        ],
        ids=["failed", "error", "one-of-two-failed"],
    )
    def test_check_dynamic_new_test_failing_before_the_fix_passes_g5(self, report):
        assert gate(dynamic_results(pre_fix_new=report()), "G5").passed is True

    @pytest.mark.parametrize(
        "report",
        [
            lambda: make_report({NEW_ID: "passed"}),
            lambda: make_report({NEW_ID: "passed", "tests.unit.test_new::test_other": "passed"}),
            lambda: make_report({NEW_ID: "skipped"}, exit_code=0),
            lambda: make_report({}, exit_code=5),  # 1 件も集まらない
            lambda: make_report({}, exit_code=0),
            lambda: make_report({}, exit_code=3),
            lambda: make_report({}, exit_code=4),
            lambda: make_report({}, exit_code=None),
            lambda: make_report({NEW_ID: "failed"}, exit_code=None, timed_out=True),
            lambda: make_report({}, exit_code=None, timed_out=True),
            # レビュー対応 F5: 集めるときのエラー（終了コード 2）や、結果の無い失敗は、「落ちた」と認めない
            lambda: make_report({}, exit_code=1),
            lambda: make_report({}, exit_code=2),
            lambda: make_report({"::tests.unit.test_new": "error"}, exit_code=2),
            lambda: make_report({NEW_ID: "failed"}, exit_code=2),
        ],
        ids=[
            "passed",
            "all-passed",
            "only-skipped",
            "nothing-collected",
            "empty-exit-0",
            "internal-error",
            "usage-error",
            "no-exit-code",
            "timed-out-with-failure",
            "timed-out",
            "no-results-exit-1",
            "no-results-exit-2",
            "collection-error",
            "failed-but-exit-2",
        ],
    )
    def test_check_dynamic_new_test_not_failing_before_the_fix_fails_g5(self, report):
        """AC-14: 新しいテストが修正前でも通る（または、落ちたことを確かめられない）。"""
        assert gate(dynamic_results(pre_fix_new=report()), "G5").passed is False


class TestG6PassesAfterFix:
    @pytest.mark.parametrize(
        "report",
        [
            lambda: make_report({NEW_ID: "passed"}),
            lambda: make_report({NEW_ID: "passed", "tests.unit.test_new::test_other": "passed"}),
        ],
        ids=["one", "two"],
    )
    def test_check_dynamic_new_tests_all_passing_after_the_fix_pass_g6(self, report):
        assert gate(dynamic_results(post_fix_new=report()), "G6").passed is True

    @pytest.mark.parametrize(
        "report",
        [
            lambda: make_report({NEW_ID: "failed"}),
            lambda: make_report({NEW_ID: "error"}),
            lambda: make_report({NEW_ID: "passed", "tests.unit.test_new::test_other": "failed"}),
            lambda: make_report({NEW_ID: "skipped"}, exit_code=0),
            lambda: make_report({}, exit_code=0),
            lambda: make_report({}, exit_code=5),
            lambda: make_report({NEW_ID: "passed"}, exit_code=1),
            lambda: make_report({NEW_ID: "passed"}, exit_code=None),
            lambda: make_report({NEW_ID: "passed"}, exit_code=None, timed_out=True),
        ],
        ids=[
            "failed",
            "error",
            "one-of-two-failed",
            "only-skipped",
            "no-tests",
            "nothing-collected",
            "passed-but-exit-1",
            "no-exit-code",
            "timed-out",
        ],
    )
    def test_check_dynamic_new_tests_not_all_passing_after_the_fix_fail_g6(self, report):
        """AC-15: 新しいテストが修正後も落ちる（または、すべて通ったことを確かめられない）。"""
        assert gate(dynamic_results(post_fix_new=report()), "G6").passed is False


class TestG7NothingElseBroken:
    def test_check_dynamic_identical_existing_outcomes_pass_g7_even_with_baseline_failures(self):
        """AC-16: 基準の時点で落ちていたテストは、落ちたままでも通過を妨げない。"""
        assert "failed" in EXISTING.values()

        assert gate(dynamic_results(), "G7").passed is True

    @pytest.mark.parametrize(
        "after",
        [
            FLIPPED,
            {**EXISTING, "tests.unit.test_orchestrator::test_label_prefixes_name": "error"},
            {**EXISTING, "tests.unit.test_orchestrator::test_label_prefixes_name": "skipped"},
            {key: value for key, value in EXISTING.items() if not key.endswith("test_label_prefixes_name")},
            {**EXISTING, "tests.unit.test_other::test_extra": "passed"},
            {},
            # 落ちていたテストが通るようになるのも、「同じ」ではない（design.md §10.2 を文字どおりに読んだ）
            {**EXISTING, "tests.unit.test_orchestrator::test_known_failure_at_baseline": "passed"},
        ],
        ids=[
            "passed-to-failed",
            "passed-to-error",
            "passed-to-skipped",
            "test-missing",
            "test-added",
            "all-missing",
            "failed-to-passed",
        ],
    )
    def test_check_dynamic_changed_existing_outcomes_fail_g7(self, after):
        """AC-16: 修正で既存のテストが 1 件落ちるようになる（成否か、ID の集合が変わる）。"""
        assert gate(dynamic_results(post_fix_existing=make_report(after)), "G7").passed is False

    @pytest.mark.parametrize("which", ["base_existing", "post_fix_existing"])
    def test_check_dynamic_timed_out_existing_run_fails_g7(self, which):
        report = make_report(dict(EXISTING), exit_code=None, timed_out=True)

        assert gate(dynamic_results(**{which: report}), "G7").passed is False


class TestG8Lint:
    @pytest.mark.parametrize(("before", "after"), [(1, 1), (3, 2), (0, 0), (32, 32)])
    def test_check_dynamic_lint_count_not_increased_passes_g8(self, before, after):
        assert gate(dynamic_results(lint_before=before, lint_after=after), "G8").passed is True

    @pytest.mark.parametrize(
        ("before", "after"), [(1, 2), (0, 1), (32, 33), (None, 1), (1, None), (None, None), (None, 0)]
    )
    def test_check_dynamic_lint_count_increased_or_unknown_fails_g8(self, before, after):
        """AC-17: 指摘が 1 件増える。数えられなかったとき（None）も通さない。"""
        assert gate(dynamic_results(lint_before=before, lint_after=after), "G8").passed is False
