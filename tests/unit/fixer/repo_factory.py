"""検査の対象になる、小さな実物のリポジトリ（design.md §14）。

``src.fixer`` には依存しない（実装が無くても、このファイルは import でき、動く）。

git は必ず ``run_git`` を通して呼ぶ:

- ``cwd`` を必ず明示する（カレントディレクトリの取り違えで、開発中のリポジトリ本体に対して実行しない）。
- 環境変数は、ここで組み立てたものだけを渡す（``GIT_DIR``・``GIT_WORK_TREE`` などを引き継がない）。
- 開発者の手元の設定（署名、フック、テンプレート）に左右されないよう、``GIT_CONFIG_GLOBAL`` を空のファイルに、
  ``GIT_CONFIG_NOSYSTEM=1`` にし、``HOME`` を空のディレクトリに向ける。
- ``FIXER_TEST_HELPER=1`` を付ける（conftest の記録が、テストの準備の呼び出しと、実装の呼び出しを見分ける）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HELPER_ENV_MARK = "FIXER_TEST_HELPER"
EXECUTION_ID = "exec-20261004120000-abcdef12"
BRANCH = f"fix/diagnosed-{EXECUTION_ID}"
AUTHOR_NAME = "Fixer Test Author"
AUTHOR_EMAIL = "fixer-test" + "@" + "example.invalid"  # 連結で書く（.gitleaks.toml の検出を避ける）

SRC = "src/agent/orchestrator.py"
EXISTING_TEST = "tests/unit/test_orchestrator.py"
EXISTING_CONFTEST = "tests/unit/conftest.py"
LEGACY = "src/agent/legacy.py"  # 基準の時点で ruff の指摘が 1 件ある
OBSERVABILITY = "src/observability/emitter.py"
NOTION = "src/agent/storage/notion_client.py"
MAIN = "src/agent/main.py"  # 対象外（認証情報の準備と書き戻し）

ORCHESTRATOR_SOURCE = '''"""見本のモジュール。average は、空のリストを渡すと ZeroDivisionError になる。"""


class WorkflowError(Exception):
    """複製のコードで定義された例外。"""


def average(values):
    total = sum(values)
    return total / len(values)


def label(name):
    return "item:" + name


class Pipeline:
    def run(self, values):
        return average(values)
'''

EXISTING_TEST_SOURCE = """from orchestrator import average, label


def test_average_returns_mean():
    assert average([2, 4]) == 3


def test_label_prefixes_name():
    assert label("a") == "item:a"


def test_known_failure_at_baseline():
    assert label("a") == "never"
"""

# 修正役の台本が使う、正しい修正と再現テスト
NEW_TEST = "tests/unit/test_average_empty.py"
NEW_TEST_SOURCE = """from orchestrator import average


def test_average_of_empty_list_is_zero():
    assert average([]) == 0.0
"""
FIX_OLD = "    total = sum(values)\n"
FIX_NEW = "    if not values:\n        return 0.0\n    total = sum(values)\n"
FIX_MARKER = "if not values"

_HANDLER = "def handler(event, context):\n    return {}\n"

# パス → 中身。対象外のファイルの見本を含む
FILES: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "sample"\nversion = "0.0.0"\n\n'
        '[tool.ruff]\ntarget-version = "py313"\nline-length = 120\n\n'
        '[tool.ruff.lint]\nselect = ["E", "F", "I", "W"]\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src/agent"]\n'
    ),
    "README.md": "# sample\n",
    "CLAUDE.md": "# rules\n",
    "template.yaml": "Resources: {}\n",
    ".gitleaks.toml": 'title = "sample"\n',
    ".github/workflows/ci.yml": "name: ci\n",
    ".steering/x/requirements.md": "# requirements\n",
    "docs/guide.md": "# guide\n",
    SRC: ORCHESTRATOR_SOURCE,
    LEGACY: "import os\n\n\ndef legacy():\n    return 1\n",
    "src/agent/storage/__init__.py": "",
    NOTION: "def create_page(blocks):\n    return len(blocks)\n",
    "src/agent/main.py": "def setup_credentials():\n    return None\n",
    "src/agent/diagnosis/__init__.py": "",
    "src/agent/diagnosis/tools.py": "def call(name):\n    return name\n",
    "src/agent/prompts/builder.py": "def build():\n    return ''\n",
    "src/agent/requirements.txt": "requests\n",
    "src/agent/Dockerfile": "FROM scratch\n",
    "src/fixer/__init__.py": "",
    "src/fixer/gates.py": "def check_static():\n    return []\n",
    "src/observability/__init__.py": "",
    OBSERVABILITY: "class EmitterError(Exception):\n    pass\n\n\ndef emit(event):\n    return dict(event)\n",
    "src/token_monitor/handler.py": _HANDLER,
    "src/trigger/app.py": _HANDLER,
    "src/trigger/slack_verify.py": "def verify(signature):\n    return bool(signature)\n",
    "src/dashboard_api/authorizer/app.py": _HANDLER,
    "src/dashboard_api/auth_login/app.py": _HANDLER,
    "src/dashboard_api/executions/app.py": _HANDLER,
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    EXISTING_CONFTEST: "import pytest\n\n\n@pytest.fixture\ndef sample_values():\n    return [1, 2, 3]\n",
    EXISTING_TEST: EXISTING_TEST_SOURCE,
    "tests/unit/fixer/__init__.py": "",
    "tests/unit/fixer/test_gates.py": "def test_placeholder():\n    assert True\n",
    "tests/unit/agent/__init__.py": "",
    "tests/unit/agent/diagnosis/__init__.py": "",
    "tests/unit/agent/diagnosis/test_tools.py": "def test_placeholder():\n    assert True\n",
}

# 実物の pytest で既存のテストを走らせたときの成否（基準の時点）。ID の末尾（テスト関数の名前）で引く
EXISTING_OUTCOMES_BY_NAME = {
    "test_average_returns_mean": "passed",
    "test_label_prefixes_name": "passed",
    "test_known_failure_at_baseline": "failed",
}
HOOK_NAMES = ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit", "reference-transaction")


def function_span(rel_path: str, name: str) -> tuple[int, int]:
    """見本のファイルの中の、関数定義の開始行と終了行。"""
    for node in ast.walk(ast.parse(FILES[rel_path])):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node.lineno, node.end_lineno or node.lineno
    raise KeyError(name)


def helper_env(home: Path) -> dict[str, str]:
    """テストの準備が git を呼ぶときの環境変数。親の環境変数は引き継がない。"""
    home.mkdir(parents=True, exist_ok=True)
    empty_config = home / "empty-gitconfig"
    empty_config.touch()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        HELPER_ENV_MARK: "1",
    }


def run_git(repo: Path, *args: str, home: Path, stdin: str | None = None) -> str:
    """``repo`` を作業ディレクトリにして git を呼ぶ。失敗したら例外。"""
    repo = Path(repo)
    if not repo.is_dir():
        raise NotADirectoryError(repo)
    completed = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(repo),
        env=helper_env(home),
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _hash_tree(root: Path) -> dict[str, str]:
    """``.git`` を除く、作業ツリーのすべてのパスの（種類・モード・中身）の要約。"""
    summary: dict[str, str] = {}
    for directory, subdirectories, files in os.walk(root):
        if ".git" in subdirectories:
            subdirectories.remove(".git")
        for name in [*subdirectories, *files]:
            path = Path(directory, name)
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                summary[rel] = "link:" + os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                summary[rel] = "dir"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                summary[rel] = f"file:{stat.S_IMODE(info.st_mode):o}:{digest}"
    return summary


@dataclass(frozen=True)
class RepoState:
    """元のリポジトリの状態（AC-20・21・28 の「変わらない」を比べるためのもの）。"""

    head: str
    branch: str
    status: str
    worktree: dict[str, str]
    index_sha256: str
    hook_marks: tuple[str, ...]

    def differences(self, other: RepoState) -> list[str]:
        names = ("head", "branch", "status", "worktree", "index_sha256", "hook_marks")
        return [name for name in names if getattr(self, name) != getattr(other, name)]


@dataclass
class SampleRepo:
    path: Path
    home: Path
    hook_mark_dir: Path
    head: str = ""
    installed_hooks: list[str] = field(default_factory=list)

    def git(self, *args: str, stdin: str | None = None) -> str:
        return run_git(self.path, *args, home=self.home, stdin=stdin)

    def _refs(self) -> dict[str, str]:
        """``refs/heads/`` の下の参照（名前 → SHA）。git を起こさず、ファイルから読む。"""
        git_dir = self.path / ".git"
        refs: dict[str, str] = {}
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                sha, _, name = line.partition(" ")
                if name.startswith("refs/heads/"):
                    refs[name] = sha
        heads = git_dir / "refs" / "heads"
        for path in heads.rglob("*"):
            if path.is_file():
                refs[path.relative_to(git_dir).as_posix()] = path.read_text(encoding="utf-8").strip()
        return refs

    def _head(self) -> tuple[str, str]:
        """（``HEAD`` の指すコミット, 現在のブランチ）。切り離された ``HEAD`` なら、ブランチは空。"""
        text = (self.path / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if text.startswith("ref: "):
            name = text[len("ref: ") :]
            return self._refs().get(name, ""), name.removeprefix("refs/heads/")
        return text, ""

    def state(self, *, with_status: bool = True) -> RepoState:
        """いまの状態。``with_status=False`` なら ``git status`` を起こさない（``status`` は空のまま）。

        ``HEAD``・インデックス・作業ツリーの中身がすべて同じなら、``git status`` の出力も同じになるので、
        前後の比較だけが目的のときは省ける（git を起こす回数を減らして、テストを速くする）。
        """
        index = self.path / ".git" / "index"
        head, branch = self._head()
        return RepoState(
            head=head,
            branch=branch,
            status=self.git("status", "--porcelain", "--untracked-files=all") if with_status else "",
            worktree=_hash_tree(self.path),
            index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
            hook_marks=tuple(sorted(path.name for path in self.hook_mark_dir.glob("*"))),
        )

    def branches(self) -> list[str]:
        return sorted(name.removeprefix("refs/heads/") for name in self._refs())

    def rev(self, name: str) -> str:
        return self.git("rev-parse", "--verify", name).strip()

    def parents(self, commit: str) -> list[str]:
        return self.git("rev-list", "--parents", "-n", "1", commit).split()[1:]

    def message(self, commit: str) -> str:
        return self.git("log", "-1", "--format=%B", commit)

    def author(self, commit: str) -> str:
        return self.git("log", "-1", "--format=%an", commit).strip()

    def tree_files(self, commit: str) -> dict[str, tuple[str, str]]:
        """コミットのツリーのすべてのファイル: パス → (モード, ブロブの SHA)。"""
        entries: dict[str, tuple[str, str]] = {}
        for line in self.git("ls-tree", "-r", "-z", commit).split("\0"):
            if line:
                meta, path = line.split("\t", 1)
                mode, _kind, sha = meta.split()
                entries[path] = (mode, sha)
        return entries

    def changed_paths(self, base: str, commit: str) -> list[str]:
        before, after = self.tree_files(base), self.tree_files(commit)
        return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))

    def file_at(self, commit: str, path: str) -> str:
        return self.git("show", f"{commit}:{path}")

    def blob_at(self, commit: str, path: str) -> bytes:
        """コミットの中のファイルの中身を、バイト列のまま返す（行末の変換をしない）。"""
        completed = subprocess.run(  # noqa: S603
            ["git", "show", f"{commit}:{path}"],  # noqa: S607
            cwd=str(self.path),
            env=helper_env(self.home),
            capture_output=True,
            check=True,
            timeout=60,
        )
        return completed.stdout

    def create_branch(self, name: str) -> None:
        self.git("branch", name, "HEAD")

    def install_hooks(self) -> None:
        """コミット時のフックに、目印のファイルを作るスクリプトを置く（AC-21）。"""
        hooks = self.path / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in HOOK_NAMES:
            script = hooks / name
            script.write_text(f'#!/bin/sh\ntouch "{self.hook_mark_dir}/{name}"\n', encoding="utf-8")
            script.chmod(0o755)
            self.installed_hooks.append(name)

    def commit_all(self, message: str) -> str:
        """作業ツリーの変更をコミットする（フックを置く前に使う）。"""
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", message)
        self.head = self.rev("HEAD")
        return self.head


def init_repo(path: Path, home: Path, files: dict[str, str] | None = None) -> None:
    """``path`` に、見本のファイルを 1 コミットにしたリポジトリを作る。作者はリポジトリの設定に入れる。"""
    path.mkdir(parents=True, exist_ok=False)
    write_files(path, FILES if files is None else files)
    run_git(path, "init", "-q", "-b", "main", home=home)
    run_git(path, "config", "user.name", AUTHOR_NAME, home=home)
    run_git(path, "config", "user.email", AUTHOR_EMAIL, home=home)
    run_git(path, "config", "commit.gpgsign", "false", home=home)
    run_git(path, "add", "-A", home=home)
    run_git(path, "commit", "-q", "-m", "base", home=home)


def copy_repo(template: Path, dest: Path, home: Path) -> SampleRepo:
    """ひな形のリポジトリを複製する（テストごとに ``git init`` し直すより速い）。"""
    shutil.copytree(template, dest, symlinks=True)
    mark_dir = dest.parent / f"{dest.name}-hook-marks"
    mark_dir.mkdir()
    repo = SampleRepo(path=dest, home=home, hook_mark_dir=mark_dir)
    repo.head = repo._head()[0]
    return repo


def handoff_dict(**overrides: object) -> dict:
    """「修正役に渡せる」受け渡し。場所は、見本の ``average`` の中の行を指す。"""
    start, _end = function_span(SRC, "average")
    data: dict = {
        "schema_version": 1,
        "execution_id": EXECUTION_ID,
        "failed_stage": "generating",
        "cause_category": "output_validation_failed",
        "cause_nature": "code_defect",
        "confidence": "high",
        "exception_type": "ZeroDivisionError",
        "code_locations": [{"path": "/app/orchestrator.py", "function": "average", "line": start + 2}],
        "action_type": "fix_code",
        "valid_evidence_count": 1,
        "fixer_eligible": True,
    }
    data.update(overrides)
    return data


def location(path: str, function: str, line: int | None = None, *, rel: str | None = None) -> dict:
    """受け渡しの場所 1 件。``line`` を省くと、``rel`` のファイルのその関数の開始行。"""
    if line is None:
        if rel is None:
            raise ValueError("rel is required when line is omitted")
        line = function_span(rel, function)[0]
    return {"path": path, "function": function, "line": line}


def write_handoff(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path
