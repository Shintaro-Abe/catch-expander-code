"""偽の ``TestRunner``・``LintRunner``、偽の修正役の台本、``run_fix`` を呼ぶ補助（design.md §14）。

``src.fixer`` の import は関数の中で行う（実装が無くても、このファイルは import できる）。

偽の ``TestRunner`` は、**渡された複製の中身を見て**結果を決める、pytest の単純な模型である:

- 修正が入っているか（``fix_marker`` が ``fix_path`` にあるか）
- 新しいテストファイルがあるか（基準の一覧に無い ``tests/**/test_*.py``）
- 走らせるパスと ``ignore``

A〜D のどの実行かを、呼ばれた順番や回数で決め打ちしない。実装が、検査を「修正役が作業した複製」で
走らせたり、修正の入っていない複製で G-6 を走らせたりすれば、結果が変わって検査の合否に現れる。
呼び出しのたびに、複製のファイルの一覧も記録する（AC-31 の「きれいな複製」の確認に使う）。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from . import repo_factory
from .repo_factory import EXISTING_TEST, FIX_MARKER, FIX_NEW, FIX_OLD, NEW_TEST, NEW_TEST_SOURCE, SRC
from .sdk_fake import Agent, FakeSdk

EXISTING_IDS = {
    "tests.unit.test_orchestrator::test_average_returns_mean": "passed",
    "tests.unit.test_orchestrator::test_label_prefixes_name": "passed",
    "tests.unit.test_orchestrator::test_known_failure_at_baseline": "failed",
}
BASELINE_TEST_FILES = frozenset(
    rel for rel in repo_factory.FILES if rel.startswith("tests/") and Path(rel).name.startswith("test_")
)
EXPLANATION = "空のリストで 0 除算になるため、先に空を確かめるようにした。"


def list_tree(tree: Path) -> list[str]:
    """複製の中のすべてのパス（ファイルとシンボリックリンク）。根からの相対パス。"""
    found = []
    for directory, subdirectories, files in os.walk(tree):
        for name in files:
            found.append(Path(directory, name).relative_to(tree).as_posix())
        for name in subdirectories:
            if Path(directory, name).is_symlink():
                found.append(Path(directory, name).relative_to(tree).as_posix())
    return sorted(found)


def _report(outcomes: dict[str, str], *, exit_code: int | None, timed_out: bool = False, tail: str = "") -> Any:
    from src.fixer.runners import TestReport

    return TestReport(outcomes=outcomes, exit_code=exit_code, timed_out=timed_out, output_tail=tail)


AUTO_EXIT_CODE: Any = object()


def make_report(outcomes: dict[str, str], *, exit_code: Any = AUTO_EXIT_CODE, timed_out: bool = False) -> Any:
    """``TestReport`` を作る。``exit_code`` を省くと、pytest と同じ決め方（0 / 1 / 5。時間切れなら None）にする。"""
    if exit_code is AUTO_EXIT_CODE:
        exit_code = None if timed_out else exit_code_for(outcomes)
    return _report(outcomes, exit_code=exit_code, timed_out=timed_out)


def exit_code_for(outcomes: dict[str, str]) -> int:
    if not outcomes:
        return 5
    return 1 if any(value in ("failed", "error") for value in outcomes.values()) else 0


def module_id(rel: str) -> str:
    """``tests/unit/test_x.py`` → ``tests.unit.test_x``（JUnit の classname）。"""
    return rel.removesuffix(".py").replace("/", ".")


@dataclass(frozen=True)
class TestRun:
    __test__ = False  # pytest に、テストのクラスとして集めさせない

    tree: Path
    paths: tuple[str, ...]
    ignore: tuple[str, ...]
    has_fix: bool
    new_tests: tuple[str, ...]
    listing: tuple[str, ...]
    timeout_seconds: float | None = None  # 呼ぶ側が渡した制限時間（F3）
    earlier_trees_alive: tuple[str, ...] = ()  # この実行が始まった時点で残っていた、前の実行の複製（F1）


@dataclass
class FakeTestRunner:
    """複製の中身から結果を決める、偽の ``TestRunner``。"""

    __test__ = False

    existing: dict[str, str] = field(default_factory=lambda: dict(EXISTING_IDS))
    existing_after: dict[str, str] | None = None  # 修正が入った複製での、既存のテストの成否
    new_before: str = "failed"  # 修正が入っていない複製での、新しいテストの成否
    new_after: str = "passed"
    tests_per_new_file: int = 1
    fix_path: str = SRC
    fix_marker: str = FIX_MARKER
    override: Callable[[TestRun], Any] | None = None  # None 以外を返したら、それを結果にする
    report_factory: Callable[..., Any] = _report
    calls: list[TestRun] = field(default_factory=list)

    def run(
        self, tree: Any, paths: Sequence[str], *, ignore: Sequence[str] = (), timeout_seconds: float | None = None
    ) -> Any:
        tree = Path(tree)
        earlier = tuple(
            dict.fromkeys(str(call.tree) for call in self.calls if call.tree != tree and call.tree.exists())
        )
        listing = tuple(list_tree(tree))
        fix_file = tree / self.fix_path
        has_fix = fix_file.is_file() and self.fix_marker in fix_file.read_text(encoding="utf-8", errors="replace")
        new_tests = tuple(
            rel
            for rel in listing
            if rel.startswith("tests/")
            and Path(rel).name.startswith("test_")
            and rel.endswith(".py")
            and rel not in BASELINE_TEST_FILES
        )
        run = TestRun(tree, tuple(paths), tuple(ignore), has_fix, new_tests, listing, timeout_seconds, earlier)
        self.calls.append(run)
        if self.override is not None:
            custom = self.override(run)
            if custom is not None:
                return custom

        ignored = {item.rstrip("/") for item in ignore}
        outcomes: dict[str, str] = {}
        for raw in paths:
            path = raw.rstrip("/")
            if (tree / path).is_dir():
                if EXISTING_TEST.startswith(path + "/") and EXISTING_TEST not in ignored:
                    outcomes.update(
                        self.existing_after if has_fix and self.existing_after is not None else self.existing
                    )
                selected = [rel for rel in new_tests if rel.startswith(path + "/") and rel not in ignored]
            elif path == EXISTING_TEST:
                outcomes.update(self.existing_after if has_fix and self.existing_after is not None else self.existing)
                selected = []
            else:
                selected = [rel for rel in new_tests if rel == path]
            for rel in selected:
                for number in range(self.tests_per_new_file):
                    outcomes[f"{module_id(rel)}::test_case_{number}"] = self.new_after if has_fix else self.new_before
        return self.report_factory(outcomes, exit_code=exit_code_for(outcomes), timed_out=False, tail="")

    def gate_runs(self, workspace: Path) -> list[TestRun]:
        """合否の検査のための実行（修正役が作業した複製の外で走ったもの）。"""
        return [run for run in self.calls if run.tree.resolve() != Path(workspace).resolve()]

    def agent_runs(self, workspace: Path) -> list[TestRun]:
        return [run for run in self.calls if run.tree.resolve() == Path(workspace).resolve()]


@dataclass
class FakeLintRunner:
    """修正が入っている複製かどうかで、指摘の数を返す偽の ``LintRunner``。"""

    before: int | None = 1
    after: int | None = 1
    fix_path: str = SRC
    fix_marker: str = FIX_MARKER
    trees: list[Path] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)  # 呼ぶ側が渡した制限時間（H9）

    def count(self, tree: Any, *, timeout_seconds: float | None = None) -> int | None:
        tree = Path(tree)
        self.trees.append(tree)
        self.timeouts.append(timeout_seconds)
        fix_file = tree / self.fix_path
        has_fix = fix_file.is_file() and self.fix_marker in fix_file.read_text(encoding="utf-8", errors="replace")
        return self.after if has_fix else self.before


# ---------------------------------------------------------------------------
# 台本
# ---------------------------------------------------------------------------

FINISH_FIXED = {"outcome": "fixed", "patch_kind": "symptomatic", "explanation": EXPLANATION}
FINISH_DECLINED = {"outcome": "declined", "patch_kind": "symptomatic", "explanation": "不具合を確かめられなかった。"}


async def good_fix(agent: Agent) -> None:
    """AC-24 の順: 読む → 書く → テスト実行（失敗）→ 書く → テスト実行（成功）→ 終了の申告。"""
    await agent.call("read_file", {"path": SRC})
    await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
    await agent.call("run_tests", {"paths": [NEW_TEST]})
    await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
    await agent.call("run_tests", {"paths": [NEW_TEST]})
    await agent.call("finish", dict(FINISH_FIXED))


async def good_fix_without_test_runs(agent: Agent) -> None:
    await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
    await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
    await agent.call("finish", dict(FINISH_FIXED))


async def good_fix_without_finish(agent: Agent) -> None:
    await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
    await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})


async def no_changes_declined(agent: Agent) -> None:
    await agent.call("read_file", {"path": SRC})
    await agent.call("finish", dict(FINISH_DECLINED))


async def no_changes_claims_fixed(agent: Agent) -> None:
    await agent.call("read_file", {"path": SRC})
    await agent.call("finish", dict(FINISH_FIXED))


async def no_changes_no_finish(agent: Agent) -> None:
    await agent.call("read_file", {"path": SRC})


def apply_good_fix_directly(root: Path) -> None:
    """ツールを通さずに、正しい修正と再現テストを複製に置く。"""
    (root / NEW_TEST).write_text(NEW_TEST_SOURCE, encoding="utf-8")
    source = root / SRC
    source.write_text(source.read_text(encoding="utf-8").replace(FIX_OLD, FIX_NEW), encoding="utf-8")


def with_tamper(tamper: Callable[[Path], None], *, finish: dict | None = None) -> Callable[[Agent], Any]:
    """正しい修正に加えて、複製のファイルを直接書き換える台本（合否の検査で落ちる差分を作る）。"""

    async def script(agent: Agent) -> None:
        await agent.call("write_file", {"path": NEW_TEST, "content": NEW_TEST_SOURCE})
        await agent.call("replace_in_file", {"path": SRC, "old": FIX_OLD, "new": FIX_NEW})
        tamper(agent.root)
        await agent.call("finish", dict(FINISH_FIXED if finish is None else finish))

    return script


def only_tamper(tamper: Callable[[Path], None]) -> Callable[[Agent], Any]:
    """複製のファイルを直接書き換えるだけの台本（修正も再現テストも無い）。"""

    async def script(agent: Agent) -> None:
        tamper(agent.root)
        await agent.call("finish", dict(FINISH_FIXED))

    return script


def write_text(rel: str, content: str) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    return tamper


def append_text(rel: str, content: str) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        with (root / rel).open("a", encoding="utf-8") as file:
            file.write(content)

    return tamper


def delete(rel: str) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        (root / rel).unlink()

    return tamper


def symlink(rel: str, target: str) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        link = root / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)

    return tamper


def chmod(rel: str, mode: int) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        (root / rel).chmod(mode)

    return tamper


def write_bytes(rel: str, content: bytes) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    return tamper


def all_of(*tampers: Callable[[Path], None]) -> Callable[[Path], None]:
    def tamper(root: Path) -> None:
        for item in tampers:
            item(root)

    return tamper


# ---------------------------------------------------------------------------
# run_fix を呼ぶ
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    run: Any
    sdk: FakeSdk
    repo: repo_factory.SampleRepo
    out_dir: Path
    before: repo_factory.RepoState
    after: repo_factory.RepoState
    branches_before: list[str]
    branches_after: list[str]
    test_runner: Any
    lint_runner: Any
    temp_dirs: list[Path]

    @property
    def new_branches(self) -> list[str]:
        return sorted(set(self.branches_after) - set(self.branches_before))

    @property
    def leftover_temp_dirs(self) -> list[Path]:
        return [path for path in self.temp_dirs if path.exists()]

    @property
    def failed_gates(self) -> list[str]:
        return [gate.gate for gate in self.run.gates if not gate.passed]

    def out_text(self) -> str:
        """出力先のすべてのファイルの中身をつないだもの（漏れの確認用）。"""
        if not self.out_dir.is_dir():
            return ""
        parts = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(self.out_dir.rglob("*"))
            if path.is_file()
        ]
        return "\n".join(parts)


_UNSET: Any = object()


def run_fixer(
    repo: repo_factory.SampleRepo,
    tmp_path: Path,
    *,
    handoff: object = None,
    handoff_path: Path | None = None,
    script: Callable[[Agent], Any] | None = good_fix,
    sdk: FakeSdk | None = None,
    test_runner: Any = _UNSET,
    lint_runner: Any = _UNSET,
    out_dir: Path | None = None,
    repo_path: Path | None = None,
    with_status: bool = False,
    **kwargs: Any,
) -> Outcome:
    """``run_fix`` を、偽の SDK に差し替えて呼ぶ。

    前後で、元のリポジトリの状態と、実行の間に ``tempfile.mkdtemp`` で作られたディレクトリを記録する。
    ``test_runner``・``lint_runner`` を省くと偽物を渡す。実物を使うときは ``None`` を明示する。
    前後の比較は、``HEAD``・現在のブランチ・作業ツリーの中身・インデックスで行う。``with_status=True`` なら、
    ``git status --porcelain`` の出力も比べる（同じ結論になるので、既定では git を起こさない）。
    """
    from src.fixer import run_fix

    if sdk is None:
        sdk = FakeSdk(script=script)
    if handoff_path is None:
        payload = repo_factory.handoff_dict() if handoff is None else handoff
        handoff_path = repo_factory.write_handoff(tmp_path / "handoff.json", payload)
    if out_dir is None:
        out_dir = tmp_path / "fix-out"
    if test_runner is _UNSET:
        test_runner = FakeTestRunner()
    if lint_runner is _UNSET:
        lint_runner = FakeLintRunner()

    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kw: Any) -> str:
        path = real_mkdtemp(*args, **kw)
        created.append(Path(path))
        return path

    before, branches_before = repo.state(with_status=with_status), repo.branches()
    with patch("src.fixer.runner._invoke_sdk", sdk), patch.object(tempfile, "mkdtemp", recording_mkdtemp):
        run = run_fix(
            handoff_path,
            repo=repo.path if repo_path is None else repo_path,
            out_dir=out_dir,
            test_runner=test_runner,
            lint_runner=lint_runner,
            **kwargs,
        )
    return Outcome(
        run=run,
        sdk=sdk,
        repo=repo,
        out_dir=out_dir,
        before=before,
        after=repo.state(with_status=with_status),
        branches_before=branches_before,
        branches_after=repo.branches(),
        test_runner=test_runner,
        lint_runner=lint_runner,
        temp_dirs=created,
    )


def assert_discarded(outcome: Outcome) -> None:
    """成果物が破棄されている: ブランチが無く、複製が消え、元のリポジトリに変化がない（AC-20）。"""
    assert outcome.run.branch is None
    assert outcome.run.commit is None
    assert outcome.new_branches == []
    assert outcome.before.differences(outcome.after) == []
    assert outcome.leftover_temp_dirs == []
    for root in outcome.sdk.roots:
        assert not root.exists()


def assert_sdk_not_called(outcome: Outcome) -> None:
    assert outcome.sdk.invocation_count == 0
    assert outcome.sdk.script_started is False
    assert_discarded(outcome)


# ---------------------------------------------------------------------------
# 手で組み立てる結果（下書きと入口のテスト用）
# ---------------------------------------------------------------------------

GATE_ORDER = ("G1", "G2", "G3", "G4", "G9", "G10", "G5", "G6", "G7", "G8")


def gate_detail(gate: str) -> str:
    return f"detail-of-{gate}-check"


def make_fix_run(**overrides: Any) -> Any:
    """``proposed`` の ``FixRun`` を手で組み立てる。"""
    from src.fixer.models import FixRun, FixToolCall, GateResult, VerifiedLocation

    trace = [
        FixToolCall(seq=1, tool="read_file", path=SRC, outcome="ok", duration_ms=3),
        FixToolCall(seq=2, tool="write_file", path=NEW_TEST, outcome="ok", duration_ms=4),
        FixToolCall(
            seq=3, tool="run_tests", path=NEW_TEST, outcome="ok", duration_ms=150, tests_passed=0, tests_failed=1
        ),
        FixToolCall(seq=4, tool="replace_in_file", path=SRC, outcome="ok", duration_ms=2),
        FixToolCall(
            seq=5, tool="run_tests", path=NEW_TEST, outcome="ok", duration_ms=140, tests_passed=1, tests_failed=0
        ),
        FixToolCall(seq=6, tool="finish", path=None, outcome="ok", duration_ms=1),
    ]
    fields: dict[str, Any] = {
        "status": "proposed",
        "stop_detail": "committed",
        "execution_id": repo_factory.EXECUTION_ID,
        "base_commit": "a" * 40,
        "branch": repo_factory.BRANCH,
        "commit": "b" * 40,
        "locations": [VerifiedLocation(path=SRC, function="average", line=9)],
        "changed_files": [SRC, NEW_TEST],
        "gates": [GateResult(gate=gate, passed=True, detail=gate_detail(gate)) for gate in GATE_ORDER],
        "trace": trace,
        "patch_kind": "symptomatic",
        "explanation": EXPLANATION,
        "num_turns": 6,
        "duration_ms": 4321,
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 165},
        "total_cost_usd": 0.0456,
    }
    fields.update(overrides)
    return FixRun(**fields)


# ---------------------------------------------------------------------------
# 下書きの読み取り
# ---------------------------------------------------------------------------

SECTIONS = ("取り込む前に人が行うこと", "診断", "変更点", "検査の結果", "修正役の説明", "確かめていないこと", "足取り")


def heading_positions(text: str) -> list[int]:
    """各節の見出し（``#`` で始まる行）の位置。無ければ -1。"""
    positions = []
    offset = 0
    lines = text.splitlines(keepends=True)
    for title in SECTIONS:
        found = -1
        cursor = 0
        for line in lines:
            if line.lstrip().startswith("#") and title in line and cursor >= offset:
                found = cursor
                break
            cursor += len(line)
        positions.append(found)
        if found >= 0:
            offset = found + 1
    return positions


def section(text: str, title: str) -> str:
    positions = dict(zip(SECTIONS, heading_positions(text), strict=True))
    start = positions[title]
    later = [position for position in positions.values() if position > start]
    return text[start : min(later)] if later else text[start:]


# ---------------------------------------------------------------------------
# テスト実行の環境変数（AC-8）
# ---------------------------------------------------------------------------

SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONDONTWRITEBYTECODE",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
}
# 実行時に、pytest と macOS が自分で足す変数（T0-1 で確かめた）
RUNTIME_ADDED_KEYS = {"PYTEST_CURRENT_TEST", "PYTEST_VERSION", "__CF_USER_TEXT_ENCODING"}
SECRET_MARK = "PARENT_SECRET_MARK_4471"
PARENT_SECRETS = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
    "SLACK_BOT_TOKEN",
    "NOTION_TOKEN",
    "PYTHONPATH",
    "PYTEST_ADDOPTS",
    "SOME_UNRELATED_VARIABLE",
)


# ---------------------------------------------------------------------------
# 多くの値を、1 つのテストの中で順に確かめる
# ---------------------------------------------------------------------------


def check_each(cases: Any, check: Callable[[Any], None]) -> None:
    """``cases`` のそれぞれに ``check`` を当て、落ちたものをまとめて報告する。

    状態を書き換えない確認を、値ごとに複製を作り直さずに行うためのもの（複製の書き出しは、実物の git を使うので遅い）。
    実装が無いことによる import の失敗は、そのまま外に出す。
    """
    failures = []
    for case in cases:
        try:
            check(case)
        except ImportError:
            raise
        except (AssertionError, pytest.fail.Exception) as error:
            failures.append(f"{case!r:.90}: {str(error).splitlines()[0] if str(error) else 'assertion failed'}")
        except Exception as error:  # noqa: BLE001
            failures.append(f"{case!r:.90}: raised {type(error).__name__}")
    assert not failures, f"{len(failures)} of {len(list(cases))} cases failed:\n" + "\n".join(failures)


def cases_of(_names: Any, values: Any, ids: Any = None) -> list:
    """``pytest.mark.parametrize`` と同じ並びの引数から、値の一覧だけを取り出す（``check_each`` に渡す）。"""
    return list(values)
