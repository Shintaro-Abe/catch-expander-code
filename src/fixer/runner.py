"""修正役の実行（起動の前の確かめ、オプション、受信ループ、合否の検査、コミット、公開する関数）。

設計詳細: `.steering/20261004-execution-fixer/design.md` §9・§10.2・§12

SDK の呼び出しは 1 回だけで、再試行も上位モデルへの切り替えもしない。上限への到達と SDK の失敗は、
例外にせず FixRun の status / stop_detail で返す。成果物として認めるかどうかは、モデルの申告ではなく、
コードが組み立て直したきれいな複製で、実際にテストとリントを走らせて決める。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import os
import pwd
import re
import sys
import sysconfig
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, ToolUseBlock
from claude_agent_sdk import query as _sdk_query

from src.fixer.draft import clean_explanation, render_pr_draft
from src.fixer.exception_types import is_known_exception_type
from src.fixer.gates import (
    TESTS_DIRECTORY,
    check_dynamic,
    check_static,
    count_changed_lines,
    display_path,
    is_under,
    new_test_files,
)
from src.fixer.gitio import Git, GitError
from src.fixer.handoff_input import HandoffError, is_eligible, load_handoff, verify_locations
from src.fixer.models import FixLimits, FixRun, FixToolCall, GateResult, HandoffInput, VerifiedLocation
from src.fixer.prompt import SYSTEM_PROMPT, build_user_prompt
from src.fixer.runners import LINT_TIMEOUT_SECONDS, LintRunner, PytestRunner, RuffRunner, TestReport, TestRunner
from src.fixer.sdk_server import SERVER_NAME, Handler, allowed_tool_names, build_handlers, build_server
from src.fixer.tools import FixToolSession
from src.fixer.workspace import (
    BaselineEntry,
    ChangeSet,
    PathDenied,
    Workspace,
    WorkspaceReplaced,
    build_tree,
    expected_manifest,
    remove_tree,
    tree_matches,
)

logger = logging.getLogger("catch-expander-fixer")

DEFAULT_MODEL = "sonnet"
MODEL_ENV = "FIXER_MODEL"
BRANCH_PREFIX = "fix/diagnosed-"
RESULT_FILE = "result.json"
TRACE_FILE = "trace.json"
DRAFT_FILE = "pr-draft.md"
COMMIT_NOTE = "修正役による提案。取り込む前に人が確認する"
MAX_REPORTED_CHANGED_FILES = 50

_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,80}")
_MAX_LOGGED_TOOL_NAMES = 10
# tools=[] が効かなかった場合に備えて、組み込みツールを名前でも外す。どちらが効いても、もう一方は害にならない。
DISALLOWED_BUILTIN_TOOLS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "Skill",
    "TodoWrite",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
)
_EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
_DRAFT_STATUSES = ("proposed", "rejected")
# 本物のホームにあれば、隔離されていない環境とみなすもの（存在だけを確かめる。中身は読まない）。
# `.claude` は、サブスクリプションの認証で同じ環境に必ず要るので、入れない
_CREDENTIAL_LOCATIONS = (
    ".aws",
    ".ssh",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".docker/config.json",
    ".codex",
    ".kube",
    ".gnupg",
)
# 検査に使う Python の環境のうち、site-packages のディレクトリを指す、sysconfig の名前
_SITE_PACKAGES_SCHEME_KEYS = ("purelib", "platlib")
# 検査の実行と、その実行が落とす検査（複製が書き換えられた、または走らせられなかったとき）
_GATE_OF_RUN = {"A": "G7", "B": "G5", "C": "G7", "D": "G6", "lint": "G8"}
_TREE_TAMPERED = "検査用の複製が、実行の間に書き換えられた"
_GATE_BUDGET_EXHAUSTED = "検査の合計の制限時間を超えたため、走らせなかった"


class _SdkStreamError(Exception):
    """SDK のストリームが出した例外。全体の時間切れ（TimeoutError）と区別するために包む。"""


class _ToolIsolationError(Exception):
    """自分で定義した 7 個以外のツールが、使える状態にある、または使われた。"""


@dataclass
class _Progress:
    """実行の途中経過。最後に FixRun にする。"""

    status: str = "failed"
    stop_detail: str = "internal_error"
    execution_id: str = ""
    base_commit: str = ""
    branch: str | None = None
    commit: str | None = None
    locations: list[VerifiedLocation] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    trace: list[FixToolCall] = field(default_factory=list)
    patch_kind: str = "unspecified"
    explanation: str = ""
    num_turns: int | None = None
    usage: dict[str, int] = field(default_factory=lambda: dict(_EMPTY_USAGE))
    total_cost_usd: float | None = None
    # 出力先に書くためのもの（FixRun には入れない）
    handoff: HandoffInput | None = None
    diff_stat: list[tuple[str, int, int]] = field(default_factory=list)
    write_outputs: bool = False  # 起動の前の確かめに落ちたときは、出力先に何も書かない
    # 元のリポジトリの設定とフックの、実行の前の要約（終わりにも比べる）
    git: Git | None = None
    fingerprint: str | None = None
    # 基準のコミットのツリーの記録（パス → モードとオブジェクト名）。モデルが書いたコードが走る前に取る
    base_tree: dict[str, tuple[int, str]] | None = None
    repository_changed: bool = False

    def stop(self, status: str, stop_detail: str) -> None:
        self.status, self.stop_detail = status, stop_detail


def build_options(*, model: str, limits: FixLimits, server: Any, cwd: str) -> ClaudeAgentOptions:
    """修正役に渡すオプションを組み立てる。

    組み込みツールをすべて外し、許可は自分で定義した 7 個だけにする。権限の確認を飛ばす設定、
    フック、サンドボックス、追加のディレクトリは使わない（固定している版で、効き方を確かめられていない）。
    作業ディレクトリを複製にするのは、万一組み込みツールが残っていても、起点がリポジトリの本体に
    ならないようにするため（縛りの本体は、自分で定義したツールのパスの検査）。
    """
    kwargs: dict[str, Any] = {}
    if limits.max_budget_usd is not None:
        kwargs["max_budget_usd"] = limits.max_budget_usd
    return ClaudeAgentOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,  # 素の文字列。claude_code のプリセットは使わない
        tools=[],
        allowed_tools=allowed_tool_names(),
        disallowed_tools=list(DISALLOWED_BUILTIN_TOOLS),
        mcp_servers={SERVER_NAME: server},
        strict_mcp_config=True,  # 設定ファイル由来の MCP サーバーを読ませない
        max_turns=limits.max_turns,
        setting_sources=[],  # 設定ファイル・CLAUDE.md を読ませない
        cwd=cwd,
        **kwargs,
    )


async def _invoke_sdk(prompt: str, options: ClaudeAgentOptions, handlers: dict[str, Handler]) -> AsyncIterator[Any]:
    """Agent SDK の query() を呼ぶ。ユニットテストはこの関数を patch する。

    handlers はここでは使わない。テストの偽物が、台本どおりにツールを呼ぶために受け取る。
    """
    async with contextlib.aclosing(_sdk_query(prompt=prompt, options=options)) as stream:
        async for message in stream:
            yield message


def _abort_on_unexpected_tools(names: list) -> None:
    """自分で定義した 7 個以外の名前があれば、警告を 1 行出して中断する。"""
    allowed = allowed_tool_names()
    unexpected = [name for name in names if name not in allowed]
    if not unexpected:
        return
    # ログに出すのは、英数字と下線だけの名前（_MAX_LOGGED_TOOL_NAMES 件まで）。それ以外は <other> とする
    shown = [name if isinstance(name, str) and _TOOL_NAME_PATTERN.fullmatch(name) else "<other>" for name in unexpected]
    logger.warning(
        "Fixer aborted: found tools outside the custom tools: %s",
        ", ".join(list(dict.fromkeys(shown))[:_MAX_LOGGED_TOOL_NAMES]),
    )
    raise _ToolIsolationError


async def _receive(prompt: str, options: ClaudeAgentOptions, handlers: dict[str, Handler]) -> ResultMessage | None:
    """最初の ResultMessage を返す（第 1 段階の `_receive` と同じ。最後まで回すと結果を失う）。

    自分で定義した 7 個以外のツールが見えたら、その場で中断する（_ToolIsolationError）。見るのは、
    起動時のツール一覧（初期化のシステムメッセージ）と、途中で流れてくるツールの呼び出し。
    """
    async with contextlib.aclosing(_invoke_sdk(prompt, options, handlers)) as stream:
        async for message in stream:
            if isinstance(message, SystemMessage):
                data = message.data if isinstance(message.data, dict) else {}
                # 一覧が無い・list でないときは、確かめようがないので続行する
                if message.subtype == "init" and isinstance(data.get("tools"), list):
                    _abort_on_unexpected_tools(data["tools"])
            elif isinstance(message, AssistantMessage):
                _abort_on_unexpected_tools([block.name for block in message.content if isinstance(block, ToolUseBlock)])
            elif isinstance(message, ResultMessage):
                return message
    return None


async def _receive_with_timeout(
    prompt: str,
    options: ClaudeAgentOptions,
    handlers: dict[str, Handler],
    timeout_seconds: float,
    session: FixToolSession,
) -> ResultMessage | None:
    async def _guarded() -> ResultMessage | None:
        try:
            return await _receive(prompt, options, handlers)
        except _ToolIsolationError:
            raise
        except Exception as e:
            # SDK が TimeoutError を出しても、全体の時間切れとは扱わない
            raise _SdkStreamError from e

    try:
        return await asyncio.wait_for(_guarded(), timeout=timeout_seconds)
    finally:
        # イベントループが閉じるとき、ツールのスレッドが終わるのを待つ。その前に閉じる印を立てて、
        # ロックを待っている呼び出しが、時間切れの後に順に実行されないようにする
        session.stop_accepting()


def _usage_of(result_message: ResultMessage | None) -> dict[str, int]:
    """ResultMessage からトークン数を取る。合計はキャッシュのぶんを足す（第 1 段階と同じ）。"""
    usage = getattr(result_message, "usage", None)
    if not isinstance(usage, dict):
        return dict(_EMPTY_USAGE)
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
    except (TypeError, ValueError):
        return dict(_EMPTY_USAGE)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens + cache_creation + cache_read,
    }


def _cost_of(result_message: ResultMessage | None) -> float | None:
    cost = getattr(result_message, "total_cost_usd", None)
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(cost):
        return None
    return float(cost)


def _real_home_directory() -> Path:
    """起動した利用者の、本物のホーム（環境変数ではなく、OS の利用者情報から得る）。テストは、これを差し替える。"""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _credentials_in_home() -> list[str]:
    """本物のホームにある、認証情報らしいものの名前。存在だけを確かめ、中身は読まない。"""
    home = _real_home_directory()
    return [name for name in _CREDENTIAL_LOCATIONS if os.path.lexists(home / name)]


def _writable_python_locations() -> list[str]:
    """検査に使う Python の環境（`sys.prefix` と site-packages）のうち、いまの利用者から書き込めるもの。

    修正役が書いたテストは、検査と同じ Python で走る。環境に書き込めると、site-packages に `.pth` を置くなどして、
    以後のリントとテストのすべてで、先に動くコードを仕込める（提案の差分には現れない）。確かめるのは権限だけで、
    何も書かない。テストは、これを差し替える。
    """
    candidates = [sys.prefix, *(sysconfig.get_paths().get(key) for key in _SITE_PACKAGES_SCHEME_KEYS)]
    found: dict[str, str] = {}  # 実際のパス → 見つけたパス（同じ場所を、2 度は挙げない）
    for path in candidates:
        if path and os.access(path, os.W_OK):
            found.setdefault(os.path.realpath(path), path)
    return list(found.values())


def _repository_was_changed(progress: _Progress) -> bool:
    """元のリポジトリの設定とフックが、実行の前から変わっていれば True（確かめられないときも True）。"""
    if progress.git is None or progress.fingerprint is None:
        return False
    try:
        return progress.git.repository_fingerprint() != progress.fingerprint
    except Exception:
        # 照合が、どんな形で失敗しても、外に漏らさない。確かめられなかったものは「変わった」として扱う
        logger.warning("Repository configuration could not be checked (non-fatal)", exc_info=True)
        return True


def _is_repository_root(git: Git, repo: Path) -> bool:
    """`repo` が、リポジトリの作業ツリーの根か。"""
    try:
        return git.toplevel().resolve() == repo.resolve()
    except (GitError, OSError, RuntimeError):
        return False


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def _is_valid_out_dir(out_dir: Path, repo: Path) -> bool:
    """出力先が、リポジトリの外で、無いか、空のディレクトリであること。

    リポジトリの中だと、元の作業ツリーに未追跡のファイルが増える。シンボリックリンクは、たどった先で確かめる。
    """
    try:
        resolved = out_dir.resolve()
        if _is_inside(resolved, repo.resolve()):
            return False
        if os.path.lexists(out_dir):
            return resolved.is_dir() and not any(resolved.iterdir())
        # まだ無い出力先は、いちばん近い実在の親がディレクトリであること
        ancestor = resolved.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        return ancestor.is_dir()
    except (OSError, RuntimeError):
        return False


def _unusable_report() -> TestReport:
    """「成否を確かめられなかった」結果。"""
    return TestReport(outcomes={}, exit_code=None, timed_out=False, output_tail="")


def _safe_test_report(
    runner: TestRunner, tree: Path, paths: Sequence[str], ignore: Sequence[str], timeout_seconds: float
) -> TestReport:
    """検査のテスト実行。実行の側が例外を出したときは、「成否を確かめられなかった」結果にする。"""
    try:
        report = runner.run(tree, list(paths), ignore=list(ignore), timeout_seconds=timeout_seconds)
        # 形の合わない結果は、使わない
        if isinstance(report.outcomes, dict) and isinstance(report.timed_out, bool):
            return report
    except Exception:
        logger.exception("Gate test run failed")
    return _unusable_report()


def _safe_lint_count(runner: LintRunner, tree: Path, timeout_seconds: float) -> int | None:
    try:
        count = runner.count(tree, timeout_seconds=timeout_seconds)
    except Exception:
        logger.exception("Gate lint run failed")
        return None
    return count if type(count) is int and count >= 0 else None


class _GateTrees:
    """検査用のきれいな複製を、使う実行の直前に 1 つずつ作り、終わったら消す。

    同時に存在する検査用の複製は 1 つだけ（ある実行の中で動いたコードが、ほかの実行の複製を書き換えられない）。
    実行の前と後で、複製の中身が、組み立てた中身（基準のコミット＋検査を通ったファイル）と一致することを確かめる。
    複製は、作業用の複製を作ったときの書き出し（メモリに持っているもの）から組み立てる。git は呼ばない
    （`git archive` は、設定のフィルターのコマンドを実行する。モデルが書いたコードが、その設定を置きうる）。
    """

    def __init__(
        self, git: Git, base: str, baseline: Mapping[str, BaselineEntry], archive: bytes | None, timeout_seconds: float
    ) -> None:
        self._git = git
        self._base = base
        self._baseline = baseline
        self._archive = archive
        self._deadline = time.monotonic() + timeout_seconds
        self.problems: dict[str, str] = {}  # 実行の名前 → 使えなかった理由

    def remaining_seconds(self) -> float:
        return self._deadline - time.monotonic()

    def use(self, label: str, files: Mapping[str, bytes], action: Callable[[Path, float], Any]) -> Any:
        """複製を作り、`action` を走らせて、結果を返す。使えなかったときは None（理由を `problems` に残す）。

        `action` には、複製と、検査の残り時間（正の値）を渡す。残り時間が 0 以下なら、複製を作った後でも、
        `action` を走らせない。

        Raises:
            GitError, PathDenied, OSError: 複製を組み立てられなかった。
        """
        if self.remaining_seconds() <= 0:
            self.problems[label] = _GATE_BUDGET_EXHAUSTED
            return None
        if self._archive is None:
            raise GitError("baseline archive is not available")
        parent = Path(tempfile.mkdtemp(prefix="fixer-gate-"))
        try:
            tree = parent / "tree"
            build_tree(self._git, self._base, tree, files, archive=self._archive)
            expected = expected_manifest(self._baseline, files)
            if not tree_matches(tree, expected):
                self.problems[label] = _TREE_TAMPERED
                return None
            remaining = self.remaining_seconds()
            if remaining <= 0:
                self.problems[label] = _GATE_BUDGET_EXHAUSTED
                return None
            result = action(tree, remaining)
            if not tree_matches(tree, expected):
                self.problems[label] = _TREE_TAMPERED
            return result
        finally:
            # 割り込みで抜けるときも、次の複製を作る前に消す
            remove_tree(parent)


def _run_dynamic_gates(
    git: Git,
    base: str,
    changes: ChangeSet,
    workspace: Workspace,
    limits: FixLimits,
    test_runner: TestRunner,
    lint_runner: LintRunner,
) -> list[GateResult]:
    """動的な検査（G-5〜G-8）を、コードが組み立て直したきれいな複製で走らせる。

    修正役が作業した複製では走らせない（残されたファイルやキャッシュが、結果に影響しないようにする）。
    信頼できるコードだけが動く処理（リントと、基準での既存のテスト）を先に済ませ、その後で、モデルが書いた
    コードが動く実行（B・C・D）を走らせる。

    Raises:
        GitError, PathDenied, OSError: 複製を組み立てられなかった。
    """
    new_tests = new_test_files(changes)
    # 実行 B の複製に足すのは、tests/ の下に足したもの（新しいテストファイルと、空の __init__.py）だけ
    test_additions = {rel: content for rel, content in changes.added.items() if is_under(rel, TESTS_DIRECTORY)}
    fix_files = {**changes.added, **changes.modified}
    trees = _GateTrees(git, base, workspace.baseline, workspace.archive, limits.gate_timeout_seconds)

    def run_tests(
        label: str, files: Mapping[str, bytes], paths: Sequence[str], ignore: Sequence[str] = ()
    ) -> TestReport:
        def action(tree: Path, remaining: float) -> TestReport:
            return _safe_test_report(test_runner, tree, paths, ignore, min(limits.test_timeout_seconds, remaining))

        return trees.use(label, files, action) or _unusable_report()

    def count_lint(files: Mapping[str, bytes]) -> int | None:
        # リントにも、検査の残り時間を渡す
        return trees.use(
            "lint",
            files,
            lambda tree, remaining: _safe_lint_count(lint_runner, tree, min(LINT_TIMEOUT_SECONDS, remaining)),
        )

    lint_before = count_lint({})
    lint_after = count_lint(fix_files)
    base_existing = run_tests("A", {}, [TESTS_DIRECTORY])
    pre_fix_new = run_tests("B", test_additions, new_tests)
    # 既存のテストだけを走らせる（集める順番で成否が変わるテストがあるので、基準と条件を揃える）
    post_fix_existing = run_tests("C", fix_files, [TESTS_DIRECTORY], ignore=new_tests)
    # 実行 C とは別に作り直した複製で走らせる（C の中で動いた修正のコードが、複製を書き換えていても響かない）
    post_fix_new = run_tests("D", fix_files, new_tests)
    results = check_dynamic(
        pre_fix_new=pre_fix_new,
        post_fix_new=post_fix_new,
        base_existing=base_existing,
        post_fix_existing=post_fix_existing,
        lint_before=lint_before,
        lint_after=lint_after,
    )
    # 複製が書き換えられた実行、走らせられなかった実行に対応する検査は、不合格にする
    failed_gates = {_GATE_OF_RUN[label]: reason for label, reason in trees.problems.items()}
    return [
        GateResult(result.gate, False, failed_gates[result.gate]) if result.gate in failed_gates else result
        for result in results
    ]


def _diff_stat(changes: ChangeSet, workspace: Workspace) -> list[tuple[str, int, int]]:
    """変更したファイルごとの（パス, 足した行数, 消した行数）。"""
    contents = {**changes.added, **changes.modified}
    stat = []
    for rel in sorted({*contents, *changes.deleted}):
        original = None if rel in changes.added else getattr(workspace.baseline.get(rel), "content", None)
        added, removed = count_changed_lines(original, contents.get(rel))
        stat.append((display_path(rel), added, removed))
    return stat


def _commit_message(handoff: HandoffInput, paths: list[str]) -> str:
    """コミットメッセージ。固定の文面と固定項目、検査を通ったパスだけからなる（モデルの文字列は入れない）。"""
    lines = [
        f"fix: 診断にもとづく修正案 ({handoff.execution_id})",
        "",
        f"原因の分類: {handoff.cause_category}",
        f"例外の型: {handoff.exception_type}",
        "変更したファイル:",
        *(f"- {path}" for path in paths),
        "",
        COMMIT_NOTE,
    ]
    return "\n".join(lines) + "\n"


def _run_agent(
    progress: _Progress,
    workspace: Workspace,
    handoff: HandoffInput,
    limits: FixLimits,
    model: str | None,
    test_runner: TestRunner,
    clock: Callable[[], float],
) -> FixToolSession | None:
    """修正役を 1 回走らせる。続けて検査に進めるなら、セッションを返す。進めないなら、終了理由を入れて None。"""
    try:
        # SDK を呼ぶ前の準備。ここでの失敗は setup_error とし、SDK の失敗（sdk_error）と分ける
        session = FixToolSession(workspace, limits, test_runner, clock=clock, deadline=clock() + limits.timeout_seconds)
        handlers = build_handlers(session)
        options = build_options(
            model=model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
            limits=limits,
            server=build_server(handlers),
            cwd=str(workspace.root),
        )
        prompt = build_user_prompt(handoff, progress.locations)
    except Exception:
        logger.warning("Fixer could not be set up (non-fatal)", exc_info=True)
        progress.stop("failed", "setup_error")
        return None

    result_message: ResultMessage | None = None
    timed_out = sdk_failed = isolation_failed = False
    try:
        result_message = asyncio.run(_receive_with_timeout(prompt, options, handlers, limits.timeout_seconds, session))
    except _ToolIsolationError:
        isolation_failed = True
    except TimeoutError:
        timed_out = True
        logger.warning("Fixer timed out after %s seconds", limits.timeout_seconds)
    except Exception:
        sdk_failed = True
        logger.warning("Fixer SDK call failed (non-fatal)", exc_info=True)
    finally:
        # 処理の途中のツールの呼び出しが終わるのを待ち、これ以降の呼び出しを断る（割り込みで抜けるときも）
        session.close()
    progress.trace = list(session.trace)
    num_turns = getattr(result_message, "num_turns", None)
    progress.num_turns = num_turns if isinstance(num_turns, int) else None
    progress.usage = _usage_of(result_message)
    progress.total_cost_usd = _cost_of(result_message)
    # 7 個以外のツールが見えた実行の申告は使わない（それまでの足取りだけを残す）
    if session.finished is not None and not isolation_failed:
        progress.patch_kind = session.finished.patch_kind
        progress.explanation = clean_explanation(session.finished.explanation, limits.max_explanation_chars)

    # ここから下で終わるときは、変更があっても検査に進まず、破棄する
    subtype = getattr(result_message, "subtype", None)
    if isolation_failed:
        progress.stop("failed", "tool_isolation_failed")
    elif sdk_failed or (result_message is None and not timed_out):
        progress.stop("failed", "sdk_error")
    elif timed_out:
        progress.stop("incomplete", "timeout")
    elif subtype == "error_max_turns":
        progress.stop("incomplete", "max_turns")
    elif subtype == "error_max_budget_usd":
        progress.stop("incomplete", "max_budget")
    elif getattr(result_message, "is_error", False):
        progress.stop("failed", "sdk_error")
    else:
        return session
    return None


def _check_and_commit(
    progress: _Progress,
    git: Git,
    workspace: Workspace,
    session: FixToolSession,
    handoff: HandoffInput,
    limits: FixLimits,
    test_runner: TestRunner,
    lint_runner: LintRunner,
) -> None:
    """変更を取り出し、合否の検査にすべて通ったものだけを、作業用ブランチにコミットする。"""
    try:
        changes = workspace.changes(limits.max_write_bytes)
    except WorkspaceReplaced:
        # 複製の根が、リンクや別のディレクトリに替えられた。替えられた先をたどらず、成果物を出さない
        logger.warning("Fixer workspace root was replaced during the run; nothing is proposed")
        progress.stop("failed", "workspace_tampered")
        return
    # 終了の申告に関わらず、変更がなければ「直せない」
    if changes.is_empty:
        progress.stop("declined", "no_changes")
        return
    contents = {**changes.added, **changes.modified}
    changed = sorted({*contents, *changes.deleted, *changes.irregular, *changes.oversized})
    progress.changed_files = [display_path(rel) for rel in changed[:MAX_REPORTED_CHANGED_FILES]]
    claim = session.finished
    if claim is None:
        progress.stop("incomplete", "no_finish")
        return
    if claim.outcome != "fixed":
        progress.stop("declined", "declined_with_changes")
        return

    # モデルの作業の間に、元のリポジトリの設定やフックが変わっていたら、検査にも進まない
    if _repository_was_changed(progress):
        progress.stop("failed", "repository_tampered")
        return

    progress.diff_stat = _diff_stat(changes, workspace)
    progress.gates = check_static(changes, workspace.baseline, limits)
    # ここから先は、取り出した中身だけを使う。作業した複製は、検査のテストが走る前に消す
    workspace.destroy()
    # 静的な検査に 1 つでも落ちたら、動的な検査は走らせない
    if all(gate.passed for gate in progress.gates):
        try:
            dynamic = _run_dynamic_gates(
                git, progress.base_commit, changes, workspace, limits, test_runner, lint_runner
            )
        except (GitError, PathDenied, OSError):
            logger.warning("Gate trees could not be built (non-fatal)", exc_info=True)
            progress.stop("failed", "setup_error")
            return
        progress.gates = [*progress.gates, *dynamic]
    failed = [gate.gate for gate in progress.gates if not gate.passed]
    if failed:
        progress.stop("rejected", "+".join(failed))
        return

    # 修正役が走らせたテストのコードは、元のリポジトリの .git に書ける。設定やフックが変わっていたら、
    # git の書き込みをしない（置かれたフックやフィルターを、修正役自身が実行しないようにする）。モデルが書いた
    # コードが走った後の git の呼び出しは、この照合の `rev-parse` と、この下のコミットの一連だけ
    if _repository_was_changed(progress):
        progress.stop("failed", "repository_tampered")
        return

    branch = BRANCH_PREFIX + handoff.execution_id
    try:
        # コミットに入るのは、検査を通ったファイルの中身（変更を取り出した時点のもの）だけ
        # 基準のツリーは、実行の最初に取った記録と比べる（コミットの時点で読み直した基準は、信用しない）
        commit = git.create_branch_commit(
            base=progress.base_commit,
            branch=branch,
            message=_commit_message(handoff, sorted(contents)),
            files=contents,
            expected_base=progress.base_tree,
        )
    except GitError:
        logger.warning("Fix could not be committed (non-fatal)", exc_info=True)
        progress.stop("failed", "commit_error")
        return
    progress.branch, progress.commit = branch, commit
    progress.stop("proposed", "committed")


def _execute(
    progress: _Progress,
    scratch_directories: list[Path],
    handoff_path: Path,
    repo: Path,
    out_dir: Path,
    base: str,
    limits: FixLimits,
    model: str | None,
    test_runner: TestRunner,
    lint_runner: LintRunner,
    clock: Callable[[], float],
    allow_unisolated: bool,
) -> None:
    """起動の前の確かめから、コミットまで。終了理由は `progress` に入れる。"""
    # ① 起動の前の確かめ。ここで落ちたときは、SDK を呼ばず、出力先にも何も書かない
    if not allow_unisolated:
        # 修正役が書いたテストは、起動した人の権限で走る。認証情報のある環境では、明示されない限り起動しない
        found = _credentials_in_home()
        if found:
            logger.warning(
                "Fixer refused to start in an unisolated environment: found %s in the home directory",
                ", ".join(found),
            )
            return progress.stop("failed", "unisolated_environment")
        # 検査に使う Python の環境に書き込めると、テストのコードが、以後の検査をすべて偽れる
        writable = _writable_python_locations()
        if writable:
            logger.warning(
                "Fixer refused to start in an unisolated environment: the Python environment used for the gates "
                "is writable by the current user: %s",
                ", ".join(writable),
            )
            return progress.stop("failed", "unisolated_environment")
    git = Git(repo)
    if not _is_repository_root(git, repo):
        return progress.stop("failed", "not_a_repository")
    try:
        progress.base_commit = git.resolve_commit(base)
    except GitError:
        return progress.stop("failed", "base_not_found")
    if not _is_valid_out_dir(out_dir, repo):
        return progress.stop("failed", "invalid_out_dir")
    try:
        # 元のリポジトリの設定とフックの、いまの状態を覚えておく（モデルの作業の後、コミットの直前、終わりに比べる）。
        # 上限を超えるなどで照合できないリポジトリは、複製を作る前（git archive を呼ぶ前）に断る
        fingerprint = git.repository_fingerprint()
    except Exception:
        logger.warning("Repository configuration could not be checked before the start (non-fatal)", exc_info=True)
        return progress.stop("failed", "setup_error")
    try:
        # 書き出し（git archive）が、コミットの中身と食い違うリポジトリは、扱わない
        if git.has_archive_attributes(progress.base_commit):
            return progress.stop("failed", "unsupported_attributes")
    except GitError:
        return progress.stop("failed", "setup_error")

    # ② 受け渡しの読み込みと検証。ブランチ名は実行 ID から決まるので、ブランチの有無は、この後で確かめる
    try:
        handoff = load_handoff(handoff_path)
    except HandoffError:
        # 検証に通らなかった値は、結果にもログにも残さない
        progress.write_outputs = True
        return progress.stop("failed", "invalid_handoff")
    progress.handoff, progress.execution_id = handoff, handoff.execution_id
    try:
        if git.branch_exists(BRANCH_PREFIX + handoff.execution_id):
            return progress.stop("failed", "branch_exists")
        progress.git, progress.fingerprint = git, fingerprint
        # 基準のツリーも、モデルが書いたコードが走る前に、ここで記録する（コミットを作るときに、これと比べる）
        progress.base_tree = git.tree_objects(progress.base_commit)
    except GitError:
        return progress.stop("failed", "setup_error")
    progress.write_outputs = True

    # ③ 作業用の複製を作る（リポジトリの外。.git は置かない）
    try:
        scratch = Path(tempfile.mkdtemp(prefix="fixer-"))
        scratch_directories.append(scratch)
        if _is_inside(scratch.resolve(), repo.resolve()):
            return progress.stop("failed", "setup_error")
        workspace = Workspace.create(git, progress.base_commit, scratch)
    except (GitError, OSError, RuntimeError):
        logger.warning("Fixer workspace could not be created (non-fatal)", exc_info=True)
        return progress.stop("failed", "setup_error")

    # ④ 場所の再検証と、条件の再計算。受け渡しの申告は信じず、複製のコードと項目から確かめ直す
    locations = verify_locations(handoff, workspace.root)
    if not is_known_exception_type(handoff.exception_type, workspace.root):
        return progress.stop("not_eligible", "unknown_exception_type")
    if not locations:
        return progress.stop("not_eligible", "no_verified_location")
    if not is_eligible(handoff, locations):
        return progress.stop("not_eligible", "conditions_not_met")
    progress.locations = locations

    # ⑤ 修正役を走らせる（SDK は 1 回。再試行なし）
    session = _run_agent(progress, workspace, handoff, limits, model, test_runner, clock)
    if session is None:
        return None
    # ⑥〜⑧ 変更の取り出し、合否の検査、コミット
    return _check_and_commit(progress, git, workspace, session, handoff, limits, test_runner, lint_runner)


def _write_new(path: Path, text: str) -> None:
    # "x" で開く: 既にあるファイルは上書きしない
    with path.open("x", encoding="utf-8") as file:
        file.write(text)


def _write_outputs(run: FixRun, progress: _Progress, out_dir: Path) -> None:
    """結果・足取り・下書きを、出力先に書く（best-effort）。本番の記録には書かない。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_new(out_dir / RESULT_FILE, json.dumps(dataclasses.asdict(run), ensure_ascii=False, indent=2) + "\n")
        trace = [dataclasses.asdict(call) for call in run.trace]
        _write_new(out_dir / TRACE_FILE, json.dumps(trace, ensure_ascii=False, indent=2) + "\n")
        if run.status in _DRAFT_STATUSES and progress.handoff is not None:
            _write_new(out_dir / DRAFT_FILE, render_pr_draft(run, progress.handoff, progress.diff_stat))
    except (OSError, ValueError):
        logger.warning("Failed to write fixer outputs (non-fatal)", exc_info=True)


def _log_completion(run: FixRun) -> None:
    logger.info(
        "Execution fix finished: execution_id=%s status=%s stop_detail=%s changed_files=%d tool_call_count=%d "
        "num_turns=%s duration_ms=%d",
        run.execution_id or "<invalid>",
        run.status,
        run.stop_detail,
        len(run.changed_files),
        len(run.trace),
        run.num_turns,
        run.duration_ms,
    )


def run_fix(
    handoff_path: Path,
    *,
    repo: Path,
    out_dir: Path,
    base: str = "HEAD",
    limits: FixLimits | None = None,
    model: str | None = None,
    test_runner: TestRunner | None = None,
    lint_runner: LintRunner | None = None,
    clock: Callable[[], float] = time.monotonic,
    allow_unisolated: bool = False,
) -> FixRun:
    """受け渡し用の診断結果を 1 つ受け取り、再現するテストと修正を作って、作業用ブランチに提案する。例外を出さない。

    元のリポジトリの作業ツリー・現在のブランチ・インデックスには触れない。プッシュもしない。

    Args:
        handoff_path: 受け渡し用の診断結果のファイル。信頼せず、形・場所・条件を検証し直す。
        repo: リポジトリの作業ツリーの根。
        out_dir: 結果・足取り・下書きの出力先。リポジトリの外で、無いか、空のディレクトリであること。
        base: 基準のコミット。作業用の複製は、ここから作る。
        limits: 上限。None なら既定値。
        model: モデル名。None なら環境変数 FIXER_MODEL、それも無ければ sonnet。
        test_runner: テストの実行。None なら実物の pytest。
        lint_runner: リントの実行。None なら実物の ruff。
        clock: 所要時間の計測に使う時計。
        allow_unisolated: 真なら、本物のホームに認証情報らしいもの（`~/.aws`・`~/.ssh` など）があっても、検査に使う
            Python の環境（`sys.prefix`・site-packages）に書き込めても、起動する。既定では、起動を断る
            （修正役が書いたテストは、起動した人の権限で、任意のコードとして走るため）。

    Returns:
        FixRun。status は proposed（検査にすべて通り、コミットした）/ rejected（検査に落ちた）/
        declined（直せない）/ incomplete（上限に達した、終了の申告が無い）/ not_eligible（条件を満たさない）/
        failed（起動の前の確かめ、受け渡しの形、準備、SDK、コミットの失敗）。
    """
    started = clock()
    limits = limits or FixLimits()
    progress = _Progress()
    scratch_directories: list[Path] = []
    try:
        _execute(
            progress,
            scratch_directories,
            Path(handoff_path),
            Path(repo),
            Path(out_dir),
            base,
            limits,
            model,
            test_runner if test_runner is not None else PytestRunner(limits.test_timeout_seconds),
            lint_runner if lint_runner is not None else RuffRunner(),
            clock,
            allow_unisolated,
        )
    except Exception:
        logger.exception("Fixer failed unexpectedly")
        if progress.commit is None:
            progress.stop("failed", "internal_error")
    finally:
        # 作業用の複製と、検査用の複製を、終了理由に関わらず消す
        for directory in scratch_directories:
            remove_tree(directory)

    # 終了理由に関わらず、元のリポジトリの設定とフックが変わっていないかを、終わりにも確かめる
    if _repository_was_changed(progress):
        progress.repository_changed = True
        logger.warning(
            "The repository's .git/hooks or .git/config changed during the run. "
            "Inspect them before the next git operation: execution_id=%s",
            progress.execution_id or "<invalid>",
        )

    run = FixRun(
        status=progress.status,
        stop_detail=progress.stop_detail,
        execution_id=progress.execution_id,
        base_commit=progress.base_commit,
        branch=progress.branch,
        commit=progress.commit,
        locations=progress.locations,
        changed_files=progress.changed_files,
        gates=progress.gates,
        trace=progress.trace,
        patch_kind=progress.patch_kind,
        explanation=progress.explanation,
        num_turns=progress.num_turns,
        duration_ms=round((clock() - started) * 1000),
        usage=progress.usage,
        total_cost_usd=progress.total_cost_usd,
        repository_changed=progress.repository_changed,
    )
    if progress.write_outputs:
        _write_outputs(run, progress, Path(out_dir))
    _log_completion(run)
    return run
