# 設計：修正役（第 2 段階）

`requirements.md`（承認済み・2026-10-04 改訂）に対する設計。受け入れ条件は `AC-n`、要件は `R-n`、合否の検査は `G-n` で引く。
第 1 段階の実装（`src/agent/diagnosis/`）と、その `design.md` §17・§18 を前提にする。

## 1. 実装アプローチ

```
python -m src.fixer --handoff <file> --out-dir <dir> [--base <commit>] [--repo <path>]
  └─ run_fix(...)                                    ← 公開する関数（同期。例外を出さない）
       ├─ ① 起動の前の確かめ（リポジトリ、基準のコミット、ブランチ名、出力先）   → failed
       ├─ ② 受け渡しの読み込みと検証（形が合わなければ failed）
       ├─ ③ 作業用の複製を作る（git archive で書き出す。.git は置かない）
       ├─ ④ 場所の再検証（複製のコードと突き合わせる）と条件の再計算           → not_eligible
       ├─ ⑤ 修正役を走らせる（SDK は 1 回。再試行なし）
       │     └─ 自分で定義した 7 つのツールだけ（読む・探す・書く・テストを走らせる・終わる）
       ├─ ⑥ 変更の取り出し（基準の一覧と比べる）       変更なし → declined
       ├─ ⑦ 合否の検査（静的 → 動的。動的は、組み立て直したきれいな複製で）    → rejected
       ├─ ⑧ コミット（git の低レベルの操作で、作業ツリーに触れずに作る）        → proposed
       ├─ ⑨ 下書きと記録を出力先に書く
       └─ ⑩ 複製をすべて消す
```

- モデルが決めるのは「何を読み、どう直し、どのテストを書くか」。
- コードが握るのは「どこを読み書きできるか」「どのコマンドが走るか」「成果物として認めるか」「何がコミットに入るか」。
- 第 1 段階のコードと、`orchestrator.py`・`main.py` は変更しない。

## 2. 置き場所と import

### 2.1 置き場所

`src/fixer/`（新規のパッケージ）。

- 本番のイメージに入らない。Dockerfile が入れるのは `src/agent/` と `src/observability/` だけ（`src/agent/Dockerfile:25-26`）。`template.yaml` の `CodeUri` も `src/fixer/` を指していない（AC-25）。
- **`pyproject.toml` は変更しない**。テストからは `from src.fixer import ...` で引ける。既存のテストが `src.observability` を同じ形で引いており（`tests/unit/observability/test_event_emitter.py:28`）、リポジトリの根が import の起点になっているためである。
- テストは `tests/unit/fixer/`（`__init__.py` を置く）。

### 2.2 ファイル構成（`src/fixer/`、すべて新規）

| ファイル | 役割 | SDK への依存 |
|---|---|---|
| `__init__.py` | `run_fix`・`FixRun`・`FixLimits` の再公開 | なし |
| `models.py` | 決まった値、データの型（§3） | なし |
| `handoff_input.py` | 受け渡しの読み込み・形の検証・場所の再検証・条件の再計算（§4） | なし |
| `exception_types.py` | 例外の型の許可一覧（§4.3） | なし |
| `gitio.py` | git の操作（決まったサブコマンドだけ。§5） | なし |
| `workspace.py` | 作業用の複製、パスの検査、変更の取り出し、検査用の複製の組み立て（§6） | なし |
| `tools.py` | `FixToolSession`（7 つのツールの中身、入力の検証、上限、足取り。§7） | なし |
| `runners.py` | テストとリントの実行、環境変数の絞り込み（§8） | なし |
| `gates.py` | 合否の検査（§10）と、対象外の一覧 | なし |
| `prompt.py` | 指示文（§11） | なし |
| `sdk_server.py` | ツールの登録（§9.1） | **あり** |
| `runner.py` | オプション、受信ループ、`run_fix`（§9） | **あり** |
| `draft.py` | プルリクエスト本文の下書き（§12） | なし |
| `__main__.py` | 手動実行の入口（§13） | なし |

### 2.3 第 1 段階のコードの再利用

| 再利用するもの | 使い道 |
|---|---|
| `diagnosis.models` の決まった値（`FAILED_STAGES` など）、`CodeLocation`、`HANDOFF_SCHEMA_VERSION` | 受け渡しの検証 |
| `diagnosis.frames.FrameVerifier` | 場所の再検証（`code_root` に、複製の中のディレクトリを渡す） |
| `diagnosis.redaction.mask_secrets` | G-10（秘密情報らしい文字列）と、下書きに入れる説明のマスク |

- import の経路: テストでは `src/agent` が pytest の `pythonpath` にあるので `import diagnosis` が通る。手動実行では `PYTHONPATH=src/agent` を付ける（§13）。
- `diagnosis/__init__.py` は `diagnosis.runner` を読み込むので、`boto3` と `claude_agent_sdk` も読み込まれる（どちらも開発用の環境に入っている。クライアントは作られず、通信もしない）。
- **第 1 段階の `handoff.py` は使わない**。受け渡しの検証は修正役の側に独立して持つ（§4）。第 1 段階の形が変わったときに、修正役が黙って追随しないようにするためである。

## 3. データの型（`models.py`）

```python
RUN_STATUSES = ("proposed", "rejected", "declined", "incomplete", "not_eligible", "failed")
TOOL_OUTCOMES = ("ok", "invalid_input", "not_found", "denied", "call_limit", "size_limit",
                 "timeout", "internal_error")
TOOL_NAMES = ("list_files", "read_file", "search_code", "write_file", "replace_in_file",
              "run_tests", "finish")
GATE_IDS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10")
PATCH_KINDS = ("symptomatic", "structural", "unspecified")

@dataclass(frozen=True)
class FixLimits:
    max_turns: int = 30
    timeout_seconds: float = 1200.0          # 修正役（SDK）の全体
    max_tool_calls: int = 120                # run_tests と finish を除く
    max_test_runs: int = 10
    test_timeout_seconds: float = 300.0      # run_tests 1 回、検査のテスト実行 1 回
    max_read_bytes: int = 60_000             # read_file 1 回
    max_write_bytes: int = 200_000           # ファイル 1 つ
    max_changed_files: int = 5               # G-9
    max_changed_lines: int = 200             # G-9（足した行＋消した行）
    max_explanation_chars: int = 800
    max_budget_usd: float | None = None

@dataclass(frozen=True)
class VerifiedLocation:
    path: str            # 複製の根からの相対パス。例: src/agent/orchestrator.py
    function: str
    line: int

@dataclass(frozen=True)
class HandoffInput:      # 検証を通った受け渡し
    execution_id: str
    failed_stage: str
    cause_category: str
    cause_nature: str
    confidence: str
    exception_type: str | None
    code_locations: tuple[CodeLocation, ...]   # diagnosis.models.CodeLocation（/app/ のパス）
    action_type: str
    valid_evidence_count: int
    fixer_eligible: bool

@dataclass(frozen=True)
class FixToolCall:       # 足取りの 1 件
    seq: int
    tool: str            # TOOL_NAMES、または "unknown"
    path: str | None     # 検証を通った相対パス（無いツール、検証に落ちたときは None）
    outcome: str
    duration_ms: int
    tests_passed: int | None = None    # run_tests のときだけ
    tests_failed: int | None = None

@dataclass(frozen=True)
class GateResult:
    gate: str            # GATE_IDS
    passed: bool
    detail: str          # コードが作る短い文（決まった形。モデルの文字列は入れない）

@dataclass
class FixRun:
    status: str                      # RUN_STATUSES
    stop_detail: str                 # §9 の表
    execution_id: str                # 検証に通らなかったときは空
    base_commit: str                 # 解決した SHA。解決できなかったときは空
    branch: str | None               # proposed のときだけ
    commit: str | None
    locations: list[VerifiedLocation]
    changed_files: list[str]
    gates: list[GateResult]          # 走らせた検査だけ
    trace: list[FixToolCall]
    patch_kind: str
    explanation: str                 # モデルの説明（マスク済み・長さの上限つき・未検証）
    num_turns: int | None
    duration_ms: int
    usage: dict[str, int]
    total_cost_usd: float | None
```

## 4. 受け渡しの読み込みと再検証（`handoff_input.py`）

```python
class HandoffError(Exception): ...           # 文面は決まった形だけ（値を入れない）
def load_handoff(path: Path) -> HandoffInput
def verify_locations(handoff: HandoffInput, workspace_root: Path) -> list[VerifiedLocation]
def is_eligible(handoff: HandoffInput, locations: list[VerifiedLocation]) -> bool
```

### 4.1 形の検証（`load_handoff`）

次のどれかに当てはまれば `HandoffError`（→ `failed`・`invalid_handoff`）。

- ファイルが 100KB を超える、JSON として読めない、最上位が辞書でない。
- 項目の集合が、ちょうど次の 11 個でない（足りない・余る、のどちらも不可）: `schema_version`、`execution_id`、`failed_stage`、`cause_category`、`cause_nature`、`confidence`、`exception_type`、`code_locations`、`action_type`、`valid_evidence_count`、`fixer_eligible`。
- `schema_version` が `HANDOFF_SCHEMA_VERSION`（1）でない。
- `execution_id` が `exec-[0-9]{14}-[0-9a-f]{8}` に合わない。
- 決まった値の項目が、`diagnosis.models` の一覧にない。
- `exception_type` が `None` でも `[A-Za-z_][A-Za-z0-9_]{0,79}` でもない。
- `code_locations` が list でない、5 件を超える、各要素の項目がちょうど `path`・`function`・`line` でない、`path` が `/app/[A-Za-z0-9_/]+\.py`（120 字まで）に合わない、`function` が `[A-Za-z_<][A-Za-z0-9_<>]{0,79}` に合わない、`line` が 1 以上 9,999,999 以下の整数でない（真偽値は不可）。
- `valid_evidence_count` が 0 以上 10 以下の整数でない。`fixer_eligible` が真偽値でない。

正規表現は `re.fullmatch` で確かめ、数字は `[0-9]` と書く（第 1 段階 §17 の 9 と同じ理由）。

### 4.2 場所の再検証（`verify_locations`）

各場所を、**複製の中のコード**と突き合わせる（R3、AC-3）。

1. `FrameVerifier(code_root=<複製>/src/agent).verify(path, line, function)` を試す。`None` なら、`FrameVerifier(code_root=<複製>).verify(...)` を試す（`/app/src/observability/...` のフレームのため）。
2. どちらも `None` なら、その場所を捨てる。
3. 通ったら、`FrameVerifier` が組み立て直したパス（`/app/...`）を、複製の根からの相対パスに直す（1 つ目の根なら `src/agent/` を前に付け、2 つ目の根ならそのまま）。関数名は `FrameVerifier` が返した名前を使う。
4. 相対パスが、書き込めない場所（§6.2 の禁止の一覧、`tests/`）なら捨てる。
5. 同じ（パス・関数名）は 1 件にまとめる。

`FrameVerifier` は、ファイルの実在、根の外に出ないこと、`ast` での関数定義の存在、行番号がその範囲に入ることを確かめる（第 1 段階 §18 の S1）。

**対象外の場所を取り除く理由**（4 の手順。AC-33）: 第 1 段階のクロスレビューで、実在する関数の実在する行を指す偽の行を仕込むと、修正役の作業対象を任意の実在のファイルに向けられることが分かった（第 1 段階は、スタックトレースの最初のブロックからだけ場所を取るように直すが、記録そのものを書き換えられる相手には効かない）。安全の仕組み（`src/agent/diagnosis/`、`src/fixer/`）と、認証・秘密に関わるファイルを指す場所は、受け渡しに入っていても使わない。

### 4.3 例外の型の再検証（`exception_types.py`）

```python
def allowed_exception_types(workspace_root: Path) -> frozenset[str]
def is_known_exception_type(name: str | None, workspace_root: Path) -> bool
```

形（識別子）の検証だけでは通さない（R2a、AC-32）。許可一覧は、次の 3 つを合わせたもの。

1. **組み込みの例外クラスの名前**: `builtins` のうち、`BaseException` の派生クラスであるもの。
2. **複製の中のコードで定義されたクラスの名前**: `<複製>/src/` の下の `.py` を `ast` で解析し、`ClassDef` の名前を集める（1MB を超えるファイル、解析できないファイルは飛ばす）。
3. **依存ライブラリの例外の決まった一覧**（定数）: `ClientError`、`BotoCoreError`、`EndpointConnectionError`、`HTTPError`、`ConnectionError`、`Timeout`、`RequestException`、`JSONDecodeError`、`SlackApiError`、`ClaudeSDKError`、`ProcessError`、`CLINotFoundError`、`CLIConnectionError`、`CalledProcessError`、`TimeoutExpired`。

- 第 1 段階にも同じ照合が入る予定である（クロスレビューへの対応）。**第 1 段階が、一覧を作る関数を公開していれば、それを import して使う**（一覧が 2 か所でずれないようにする）。公開していなければ、上のとおり修正役の側に持つ。どちらにするかは、実装の最初のタスク（D0-1）で、第 1 段階の実物を見て決める。
- 複製（基準のコミット）のコードから集めるので、失敗した実行の版と基準の版が違うと、本物の例外の型が一覧に無いことがありうる（→ `not_eligible`。安全な側に倒れる）。

### 4.4 条件の再計算（`is_eligible`）

「修正役に渡せる」が真であることは、**修正役を起動してよい**ことを意味するだけで、不具合があることの証拠ではない（R2b）。受け渡しの「コードの不具合」「確からしさが高い」はモデルの申告で、記録の中の指示に誘導されうる。不具合があることの独立した確認は、合否の検査の G-5・G-6 が担う（§10.3）。

次をすべて満たすときだけ真（R2、AC-2）。

- `exception_type` が `None` でなく、許可一覧にある（§4.3）
- `cause_nature == "code_defect"`、`confidence == "high"`、`action_type == "fix_code"`
- `cause_category` が `unknown`・`insufficient_records` でない
- `valid_evidence_count >= 1`
- 再検証を通った場所が 1 件以上
- ファイルの `fixer_eligible` が真

偽なら `not_eligible`。`stop_detail` は、例外の型が通らなければ `unknown_exception_type`、場所が 0 件なら `no_verified_location`、それ以外は `conditions_not_met`。

## 5. git の操作（`gitio.py`）

```python
class GitError(Exception): ...
class Git:
    def __init__(self, repo: Path) -> None
    def resolve_commit(self, rev: str) -> str                  # 40 桁の SHA。無ければ GitError
    def branch_exists(self, name: str) -> bool
    def export_tree(self, commit: str, dest: Path) -> None     # git archive → 展開
    def create_branch_commit(self, *, base: str, branch: str, message: str,
                             files: dict[str, bytes]) -> str   # 新しいコミットの SHA
```

- git は `subprocess.run([...], shell=False)` で呼ぶ。**使うサブコマンドは決まった一覧だけ**: `rev-parse`、`show-ref`、`archive`、`hash-object`、`read-tree`、`update-index`、`write-tree`、`commit-tree`、`update-ref`。一覧にないものを呼ぼうとしたら `GitError`（`push`・`fetch`・`pull`・`clone`・`remote`・`ls-remote`・`submodule` は呼べない。R14、AC-22）。
- 環境変数は決まったものだけを渡す: `PATH`、`HOME`、`GIT_TERMINAL_PROMPT=0`、`GIT_OPTIONAL_LOCKS=0`、`LC_ALL=C`。各呼び出しに制限時間（60 秒）。
- `rev` は、`-` で始まる値を拒否し、`rev-parse --verify --end-of-options <rev>^{commit}` で解決する。

### 5.1 複製の書き出し（`export_tree`）

`git archive --format=tar <commit>` の出力を、Python の `tarfile` で `dest` に展開する（`filter="data"`。絶対パス・`..`・特殊なファイルを通さない）。**`.git` は含まれない**（R6）。

### 5.2 コミット（`create_branch_commit`）

作業ツリー・現在のブランチ・インデックスに触れず、フックも走らせない（R13、AC-21）。一時的なインデックスを使う。

1. 各ファイルを `git hash-object -w --stdin --path <path>` でオブジェクトにする。
2. `GIT_INDEX_FILE=<一時ファイル>` を付けて `git read-tree <base>`。
3. 同じ環境変数で、各ファイルを `git update-index --add --cacheinfo 100644,<sha>,<path>`。
4. `git write-tree` → `git commit-tree <tree> -p <base>`（メッセージは標準入力から）。作者・コミッターは、リポジトリの設定をそのまま使う。設定が無いときは、決まった名前（`catch-expander-fixer`）を環境変数で渡す。
5. `git update-ref refs/heads/<branch> <commit> 0000…0000`（古い値に全ゼロを渡すので、**既にあるブランチは上書きできない**）。

低レベルの操作（`commit-tree`・`update-ref`）は、コミット時のフックを起動しない。ファイルのモードは常に `100644`（実行権限やシンボリックリンクは、G-10 で先に落ちている）。

- ブランチ名: `fix/diagnosed-<execution_id>`。コミットメッセージ: 1 行目は `fix: 診断にもとづく修正案 (<execution_id>)`、本文は固定項目（原因の分類、例外の型、変更したファイルの一覧、「修正役による提案。取り込む前に人が確認する」）。モデルの文字列は入れない。

## 6. 作業用の複製（`workspace.py`）

```python
class PathDenied(Exception): ...
class Workspace:
    @classmethod
    def create(cls, git: Git, base: str, parent: Path) -> Workspace   # 書き出し＋基準の一覧
    root: Path
    def resolve_read(self, rel: str) -> Path        # 複製の中の、実在するパス。外なら PathDenied
    def resolve_write(self, rel: str) -> Path       # 書き込める場所だけ。それ以外は PathDenied
    def changes(self) -> ChangeSet
    def destroy(self) -> None
def build_tree(git: Git, base: str, dest: Path, files: dict[str, bytes]) -> None   # 検査用のきれいな複製
```

- 複製は、`tempfile.mkdtemp` で作ったディレクトリの下に置く（リポジトリの外）。`run_fix` の `finally` で必ず消す（AC-20）。
- **基準の一覧**: 書き出した直後に、すべてのパスについて（種類、モード、SHA-256）を記録する。

### 6.1 読み取りのパス（`resolve_read`）

1. `rel` が文字列で、空でなく、400 字以内、NUL を含まない。
2. 絶対パス（`/` で始まる、ドライブ名つき）でない。`\` を含まない。
3. `/` で区切った各要素が、空・`.`・`..` でない。
4. 先頭の要素が `.git` でない。
5. `(root / rel).resolve()` が `root.resolve()` の中にある（シンボリックリンクをたどった先も含めて確かめる）。

どれかに反すれば `PathDenied`（ツールの結果は `denied`）。

### 6.2 書き込みのパス（`resolve_write`）

読み取りの条件に加えて、次をすべて満たす（R6a、AC-6）。

- 先頭の要素が `src` または `tests`。
- 拡張子が `.py`。
- **禁止の一覧**（G-2 と同じ。`gates.FORBIDDEN_PREFIXES`・`FORBIDDEN_NAMES`）に当たらない:
  - 前方一致: `src/fixer/`、`src/agent/diagnosis/`、`src/agent/prompts/`、`src/token_monitor/`、`src/dashboard_api/authorizer/`、`src/dashboard_api/auth_`（`auth_login`・`auth_callback`・`auth_logout`・`auth_me`）、`tests/unit/fixer/`、`tests/unit/agent/diagnosis/`
  - 完全一致: `src/agent/main.py`（認証情報の準備と書き戻し）、`src/trigger/slack_verify.py`、`src/agent/Dockerfile`、`src/agent/requirements.txt`
  - ファイル名: `conftest.py`、`sitecustomize.py`、`usercustomize.py`、`requirements*.txt`、`Dockerfile`
- `tests/` の下では、基準の一覧にあるパスには書けない（既存のテストは変えられない）。新しく作れるのは `test_*.py` と `__init__.py` だけ。
- パスの途中の要素と、書き込み先そのものが、シンボリックリンクでない（`lstat` で確かめる）。

**対象外の範囲の決め方（選択肢と推奨）**

| 案 | 中身 | 長所 | 短所 |
|---|---|---|---|
| **a. ファイルの単位（採用）** | 上の一覧のファイル・ディレクトリを、まるごと対象外にする | 判定が単純で、すり抜けにくい。書き込みのツール・場所の再検証・G-2 が、同じ一覧を使える | `src/agent/main.py` のうち、認証に関わらない部分（失敗通知の文面など）も直せなくなる |
| b. 関数の単位 | 認証に関わる関数（`_setup_claude_credentials`・`_writeback_*` など）だけを対象外にする | 直せる範囲が広い | 「どの関数が変わったか」を、差分から確実に見分ける仕組みが要る（関数の外の、モジュールの先頭のコードや import を変えて挙動を変える手が残る）。一覧の保守も要る |

a を採る。対象外にするのは、安全の仕組み（診断役・修正役）、認証と秘密に関わるもの（`main.py`、`token_monitor/`、ダッシュボードの認証、Slack の署名検証）、プロンプト、依存と設定に限る。**残るのは、不具合が実際に起きてきた場所である**: `src/agent/orchestrator.py`、`src/agent/storage/`、`src/agent/notify/`、`src/agent/state/`、`src/agent/feedback/`、`src/trigger/app.py`、`src/dashboard_api/`（認証を除く）、`src/observability/`。過去の作業単位（`.steering/`）で直してきた不具合の大半は、この範囲にある。b は、使ってみて a で困る場面が出てから検討する（`requirements.md` §7）。

### 6.3 変更の取り出し（`changes`）

複製を歩いて、基準の一覧と比べる。`__pycache__/`、`*.pyc`、`.pytest_cache/`、`.ruff_cache/` は無視する。

```python
@dataclass(frozen=True)
class ChangeSet:
    added: dict[str, bytes]        # 新しい通常のファイル
    modified: dict[str, bytes]     # 中身が変わった通常のファイル
    deleted: tuple[str, ...]
    irregular: tuple[str, ...]     # シンボリックリンク、モードの変化、種類の変化、通常のファイルでないもの
    oversized: tuple[str, ...]     # max_write_bytes を超えるもの
```

## 7. ツール（`tools.py`）

```python
class FixToolSession:
    def __init__(self, workspace: Workspace, limits: FixLimits, test_runner: TestRunner, *,
                 clock: Callable[[], float] = time.monotonic) -> None
    def call(self, name: str, args: object) -> ToolResult     # 例外を出さない
    trace: list[FixToolCall]
    finished: FinishClaim | None     # outcome / patch_kind / explanation
```

`ToolResult` は第 1 段階と同じ形（`outcome`・`data`、`to_text()`、`is_error`）を、`src/fixer/models.py` に持つ。

| ツール | 入力 | 返すもの | 備考 |
|---|---|---|---|
| `list_files` | `path`（ディレクトリ） | 直下の名前と種類（200 件まで） | `.git`・キャッシュは出さない |
| `read_file` | `path`、`start_line`（任意）、`max_lines`（任意、既定 400） | 行番号つきの本文、総行数、切り詰めの印 | UTF-8 で読めないファイルは `invalid_input` |
| `search_code` | `pattern`（固定の文字列。200 字まで）、`path`（任意、ディレクトリ） | 一致した行（パス・行番号・行。100 件まで） | 正規表現は受け付けない。`.py` だけを探す |
| `write_file` | `path`、`content` | 書いたバイト数 | 新しいファイルを作る、または全体を書き直す |
| `replace_in_file` | `path`、`old`、`new` | 置き換えた位置の行番号 | `old` がちょうど 1 か所に現れるときだけ |
| `run_tests` | `paths`（1〜5 件。`tests/` の下のファイルかディレクトリ） | 成功・失敗・エラーの件数、落ちたテストの名前（20 件まで）、出力の末尾（4,000 字まで） | §8.1 |
| `finish` | `outcome`（`fixed` / `declined`）、`patch_kind`（`symptomatic` / `structural`）、`explanation`（800 字まで） | `accepted: true` | 最後に受理したものが有効 |

`call` の流れ（第 1 段階の `ToolSession.call` と同じ考え方）:

1. 名前が `TOOL_NAMES` にない → `invalid_input`（足取りには `unknown` と残す）。
2. 上限: `run_tests` は `max_test_runs`、`finish` は 5 回、それ以外は `max_tool_calls`。超えたら `call_limit`。
3. 入力を検証する。引数が辞書でない、スキーマにない項目がある、型・長さ・取りうる値が合わない → `invalid_input`。結果には不正な項目の名前だけを返す（値は返さない）。
4. パスを解決する（§6.1・§6.2）。`PathDenied` → `denied`。無い → `not_found`。
5. 実行する。想定外の例外 → `internal_error`（`logger.exception`。例外の文面は結果に入れない）。
6. 足取りに 1 件足す（検証に落ちた呼び出し、上限で断った呼び出しも残す）。

- 1 回の呼び出し全体をロックの中で処理する（ハンドラーは別スレッドから呼ぶ）。
- `run_tests` の `paths` は、`resolve_read` を通し、さらに `tests/` の下であること、`-` で始まらないこと、`::` を含まないことを確かめる（AC-7）。
- `run_tests` が返す「落ちたテストの名前」と「出力の末尾」は、修正役自身が書いたコードと既存のコードの出力である。実行記録の中身は含まれない。

## 8. テストとリントの実行（`runners.py`）

```python
@dataclass(frozen=True)
class TestReport:
    outcomes: dict[str, str]     # テストの ID → passed / failed / error / skipped
    exit_code: int | None
    timed_out: bool
    output_tail: str
class TestRunner(Protocol):
    def run(self, tree: Path, paths: Sequence[str], *, ignore: Sequence[str] = ()) -> TestReport
class LintRunner(Protocol):
    def count(self, tree: Path) -> int | None      # 指摘の数。走らせられなければ None
class PytestRunner: ...     # 実物
class RuffRunner: ...       # 実物
def safe_env(home: Path, tmp: Path) -> dict[str, str]
```

### 8.1 コマンド

```
<sys.executable> -m pytest -q -p no:cacheprovider --junitxml <tree の外の一時ファイル>
                 [--ignore <path> ...] -- <path> ...
<sys.executable> -m ruff check --output-format json --no-cache src tests
```

- `cwd` は `tree`。`shell=False`。制限時間は `test_timeout_seconds`（リントは 60 秒）。超えたら、プロセスを止めて `timed_out=True`。
- 成否は、標準出力ではなく **JUnit の XML** から読む（テストの ID は `classname::name`）。XML が無い・読めないときは、`outcomes` を空にし、`exit_code` で判断する。
- `sys.executable` は、修正役を動かしている Python（開発用の仮想環境）。依存を入れ直すことはしない。

### 8.2 環境変数（`safe_env`）

親の環境変数は引き継がない。次の決まった一覧だけを渡す（R8、AC-8）。

| 変数 | 値 |
|---|---|
| `PATH` | 親の値（実行ファイルを見つけるため） |
| `HOME` | 空の一時ディレクトリ |
| `TMPDIR` | 一時ディレクトリ（`/tmp` の下。macOS で既存の 2 件が落ちないようにする） |
| `LANG`、`LC_ALL` | `C.UTF-8` |
| `PYTHONDONTWRITEBYTECODE` | `1` |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD` | 設定しない（`pytest-cov` などは、あっても害がない） |
| `AWS_DEFAULT_REGION` | `ap-northeast-1` |
| `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` | `dummy` |
| `AWS_EC2_METADATA_DISABLED` | `true` |
| `AWS_SHARED_CREDENTIALS_FILE`、`AWS_CONFIG_FILE` | 存在しないパス |

`HOME` と `TMPDIR` は、実行のたびに作って消す。

## 9. 修正役の実行（`sdk_server.py`・`runner.py`）

### 9.1 SDK への登録

第 1 段階と同じ作り（`build_handlers(session)` と `build_server(handlers)` を分ける。ハンドラーは `asyncio.to_thread(session.call, name, args)`。ハンドラーでも例外を受けて `internal_error` の固定文を返す）。サーバー名は `fixer`。

**SDK に渡す入力スキーマは緩くする**（最上位は `type` と `properties` だけ、項目は `description` だけ）。インプロセスの MCP サーバーは、ハンドラーの前に入力をスキーマで検証するため、厳しいスキーマを渡すと、形の不正な呼び出しがハンドラーに届かず、足取りにも回数にも残らない（第 1 段階 §18 の M1）。検証は `FixToolSession` に一本化する（R7b、AC-29）。

### 9.2 オプション

```python
ClaudeAgentOptions(
    model=model,                              # 環境変数 FIXER_MODEL、既定 "sonnet"
    system_prompt=SYSTEM_PROMPT,
    tools=[],
    allowed_tools=allowed_tool_names(),       # mcp__fixer__<name> を 7 つ
    disallowed_tools=list(DISALLOWED_BUILTIN_TOOLS),   # 第 1 段階と同じ一覧を、fixer の側に持つ
    mcp_servers={"fixer": server},
    strict_mcp_config=True,
    max_turns=limits.max_turns,
    setting_sources=[],
    cwd=<複製の根>,
    # max_budget_usd は、設定があるときだけ
)
```

- `permission_mode`・`can_use_tool`・`hooks`・`sandbox`・`add_dirs` は使わない（`requirements.md` §6 の確認結果）。
- `cwd` を複製にするのは、万一組み込みツールが残っていても、起点がリポジトリの本体にならないようにするため（縛りの本体は、自分で定義したツールのパスの検査である）。
- **起動時のツール一覧と、途中のツール呼び出しの確認**: 第 1 段階の `_receive` と同じ。7 つ以外の名前が見えたら中断する（`failed`・`tool_isolation_failed`。R7a、AC-5）。
- 受信ループは、最初の `ResultMessage` で返る。時間切れは `asyncio.wait_for`。再試行しない（R10）。
- テストは `src.fixer.runner._invoke_sdk` を差し替える（引数は `prompt, options, handlers`）。

### 9.3 終了理由

上から順に当てはめる。

| 順 | 条件 | `status` | `stop_detail` |
|---|---|---|---|
| 1 | 起動の前の確かめに落ちた（リポジトリでない、基準を解決できない、ブランチが既にある、出力先が不正） | `failed` | `not_a_repository` / `base_not_found` / `branch_exists` / `invalid_out_dir` |
| 2 | 受け渡しの形が合わない | `failed` | `invalid_handoff` |
| 3 | 複製の準備に失敗した | `failed` | `setup_error` |
| 4 | 条件を満たさない | `not_eligible` | `unknown_exception_type` / `no_verified_location` / `conditions_not_met` |
| 5 | 7 つ以外のツールが見えた | `failed` | `tool_isolation_failed` |
| 6 | SDK が例外を出した、`ResultMessage` なしで終わった | `failed` | `sdk_error` |
| 7 | 時間切れ | `incomplete` | `timeout` |
| 8 | `subtype` が `error_max_turns` / `error_max_budget_usd` | `incomplete` | `max_turns` / `max_budget` |
| 9 | 複製に変更がない | `declined` | `no_changes` |
| 10 | 変更があるが、`finish(outcome="fixed")` が受理されていない | `incomplete` | `no_finish`（`finish(outcome="declined")` のときは `declined`・`declined_with_changes`） |
| 11 | 合否の検査のどれかに落ちた | `rejected` | 落ちた検査の ID を `+` でつないだもの（例: `G5`、`G1+G2`） |
| 12 | コミットに失敗した | `failed` | `commit_error` |
| 13 | すべて通り、コミットした | `proposed` | `committed` |

- 5〜8 では、変更があっても検査に進まず、破棄する（AC-9）。
- 9 は、`finish` の申告に関わらない（「直した」と申告しても、変更がなければ `declined`。AC-30）。
- 1〜4 では SDK を呼ばない。

### 9.4 公開する関数

```python
def run_fix(
    handoff_path: Path,
    *,
    repo: Path,
    out_dir: Path,
    base: str = "HEAD",
    limits: FixLimits | None = None,
    model: str | None = None,
    test_runner: TestRunner | None = None,     # None なら PytestRunner
    lint_runner: LintRunner | None = None,     # None なら RuffRunner
    clock: Callable[[], float] = time.monotonic,
) -> FixRun
```

- 例外を出さない（R11）。
- **出力先（`out_dir`）**: リポジトリの外で、無いか、空のディレクトリであること（AC-28）。リポジトリの中だと、元の作業ツリーに未追跡のファイルが増えるため。`run_fix` は、終了理由に関わらず（起動の前の確かめに落ちた場合を除く）、`result.json`・`trace.json` を書く。`proposed` と `rejected` のときは `pr-draft.md` も書く。

## 10. 合否の検査（`gates.py`）

```python
def check_static(changes: ChangeSet, baseline: Mapping[str, object], limits: FixLimits) -> list[GateResult]
def check_dynamic(*, pre_fix_new: TestReport, post_fix_new: TestReport,
                  base_existing: TestReport, post_fix_existing: TestReport,
                  lint_before: int | None, lint_after: int | None) -> list[GateResult]
```

静的な検査（G-1〜G-4、G-9、G-10）を先に行い、1 つでも落ちたら動的な検査（G-5〜G-8）は走らせない。どちらも、結果を返すだけの関数にして、実物のテスト実行なしで確かめられるようにする。

### 10.1 静的な検査

| 検査 | 落ちる条件 |
|---|---|
| G-1 範囲 | `added`・`modified`・`deleted`・`irregular` のどれかに、`src/`・`tests/` の外のパスがある。または `deleted` が空でない（削除は認めない） |
| G-2 禁止の場所 | 変更されたパスが、§6.2 の禁止の一覧に当たる |
| G-3 既存のテスト | `tests/` の下のパスが `modified`・`deleted` にある。`tests/` の下の `added` に、`test_*.py` でも空の `__init__.py` でもないものがある |
| G-4 再現テスト | `tests/` の下の `added` に、`test_*.py` が 1 つもない |
| G-9 大きさ | 変更されたファイルの数（空の `__init__.py` を除く）が `max_changed_files` を超える。足した行＋消した行（`difflib` で数える）が `max_changed_lines` を超える。`oversized` が空でない |
| G-10 差分の中身 | `irregular` が空でない。変更されたファイルが UTF-8 で読めない、NUL を含む。`mask_secrets(本文) != 本文`（秘密情報らしい文字列がある）。拡張子が `.py` でない |

書き込みのツールが同じ規則でその場で拒否するので、通常はここで落ちない。それでも検査するのは、修正役が走らせたテストのコードが、複製の中のファイルを直接書き換えられるためである。

### 10.2 動的な検査

**きれいな複製を 3 つ、コードが組み立てる**（`build_tree`。基準のコミットを書き出し、検査を通ったファイルだけを上書きする）。修正役が作業した複製では走らせない（AC-31）。

| 複製 | 中身 |
|---|---|
| `T_base` | 基準のコミットそのまま |
| `T_pre` | 基準＋新しいテストファイル（と空の `__init__.py`）だけ |
| `T_fix` | 基準＋変更されたファイルすべて |

| 実行 | 複製 | 走らせるもの | 使う検査 |
|---|---|---|---|
| A | `T_base` | `tests` | G-7 |
| B | `T_pre` | 新しいテストファイルだけ | G-5 |
| C | `T_fix` | `tests`（新しいテストファイルを `--ignore`） | G-7 |
| D | `T_fix` | 新しいテストファイルだけ | G-6 |

| 検査 | 通る条件 |
|---|---|
| G-5 修正前に落ちる | B が時間切れでなく、`failed` または `error` のテストが 1 件以上ある（集めるときのエラーで XML にテストが無い場合は、終了コードが 1 か 2）。「テストが 1 件も集まらない」（終了コード 5）は不可 |
| G-6 修正後に通る | D が時間切れでなく、終了コードが 0 で、テストが 1 件以上あり、すべて `passed` |
| G-7 ほかを壊さない | A と C が時間切れでなく、`outcomes` が等しい（テストの ID の集合と、それぞれの成否） |
| G-8 リント | `lint_before` と `lint_after` がどちらも取れて、`lint_after <= lint_before`（`T_base` と `T_fix` で数える） |

- A と C を「既存のテストだけ」に揃えるのは、このリポジトリに、テストを集める順番で成否が変わる既存のテストがあるためである（新しいテストファイルを混ぜると、無関係な差が出る）。
- 所要時間: 当初は 20 秒前後と見込んだが、実測では、このリポジトリのテスト一式は 1 回 2 分半ほどかかり（修正役自身のテストを含む。2026-10-05 の実測で 150 秒）、A・C で 2 回走るので、検査 1 回は 5〜6 分になる（2026-10-05 改訂。§16.2）。合計の上限は `gate_timeout_seconds`（既定 900 秒）。実行の順と、複製の作り方は、§20 の F1 で改めた（リント 2 回と A を先に、B・C・D を後に。複製は、実行ごとに 1 つずつ作って消す）。
- G-7 は、基準の時点で落ちているテストを問題にしない（同じように落ちていればよい。AC-16）。

### 10.3 独立した確認としての G-5・G-6

修正役は、診断役の「コードの不具合である」という申告を信じない（R2b）。**「新しいテストが、修正の前に落ち、修正の後に通る」ことを、コードが実際に走らせて確かめる**のが、診断とは独立した確認である。診断が誤っていた（不具合が無い、場所が違う）なら、修正役は落ちるテストを書けないか、書けても修正で通せず、成果物にならない。

**この確認の弱点**: 修正役が、実際の失敗と無関係な「落ちるテスト」と、それを通すだけの変更を作れば、G-5・G-6 は通る。検査が確かめるのは「このテストが、この変更で、落ちる状態から通る状態になった」ことだけである。これを自動で見分ける手段は、今回は持たない。備えは 3 つ。

1. 変更できる範囲と大きさを絞る（G-1・G-2・G-9）。安全の仕組みと認証まわりは、変えられない。
2. ほかを壊していないことを確かめる（G-7）。
3. **最後の守りは、人によるプルリクエストの確認**。下書きに固定の文面で明記する（§12）。プッシュも取り込みも、人が行う。

## 11. 指示文（`prompt.py`）

`SYSTEM_PROMPT`（定数）と、`build_user_prompt(handoff, locations) -> str`。

`SYSTEM_PROMPT` の要旨:

- 役割: 診断でコードの不具合と分かった失敗について、再現するテストと、最小の修正を作る。
- 使えるのは 7 つのツールだけ。読み書きできるのは、渡されたコードの複製の中だけ。
- 進め方: 手がかりの場所を読む → 周りを読んで不具合を自分で確かめる → 失敗を再現するテストを**新しいファイル**（`tests/` の下の `test_*.py`）に書く → `run_tests` で落ちることを確かめる → 最小の修正を書く → `run_tests` で通ることを確かめる → `finish` を呼ぶ。
- **渡された場所は「失敗が観測された場所」であり、「直すべき場所」ではない**。手がかりにすぎず、誤っていることもある。ほかの場所も読んでよい。場所が指す関数に不具合が見つからなければ、無理に直さない。
- 「コードの不具合である」という前提も、確かめられたものではない。自分でコードを読んで、不具合を具体的に説明できるときだけ直す。
- **直せないと判断したら、何も変更せずに `finish(outcome="declined")` を呼んでよい**。それは正当な結果である。
- 既存のテスト、`conftest.py`、設定、依存の定義は変えられない。変える必要がある修正は `declined` にする。
- 修正は小さく保つ（ファイル数と行数に上限がある）。関係のない整理はしない。
- `finish` では、症状への対処（`symptomatic`）か、構造の修正（`structural`）かを申告し、何をなぜ変えたかを短く書く。
- 成果物として認めるかどうかは、コードが実際にテストを走らせて決める。

`build_user_prompt` は、固定の文面に、次の項目だけを差し込む（R4、AC-4）。

- 失敗した段階、原因の分類（決まった値）
- 例外の型（§4.3 の照合を通ったもの。通らないものは、ここに来る前に `not_eligible` になっている）
- 失敗が観測された場所（§4.2 を通ったもの。相対パス・関数名・行番号）。見出しは「失敗が観測された場所（手がかり）」とし、「修正する場所」とは書かない

実行 ID、診断役の自由記述、実行記録の中身は入れない。関数は `HandoffInput` と `VerifiedLocation` の決まった項目だけを読む（余分な項目があっても使わない）。

## 12. 成果物（`draft.py`）と記録

```python
def render_pr_draft(run: FixRun, handoff: HandoffInput, diff_stat: list[tuple[str, int, int]]) -> str
```

`pr-draft.md` の節（順序は固定。R15・R16、AC-23）:

1. **取り込む前に人が行うこと**（固定の文面）: 内容を確認する / CLAUDE.md の作業規律どおり、steering を起こして承認を得る / プッシュの後、Codex レビューを通す / main への取り込みは人が行う。続けて、次の固定の文面を入れる（R16、AC-34）: 「この修正案は、自動の診断と自動の修正によるものです。自動の検査が確かめたのは、新しいテストがこの変更で落ちる状態から通る状態になったこと、既存のテストの結果が変わらないことだけです。**再現テストが、実際に起きた失敗と同じ原因を突いているかは確かめていません。最後の確認は、このプルリクエストを読む人が行ってください。**」
2. **診断**（固定項目だけ）: 実行 ID、失敗した段階、原因の分類、例外の型、手がかりの場所。
3. **変更点**（コードが出したもの）: ファイルごとの、足した行数・消した行数。ブランチ名、コミット、基準のコミット。
4. **検査の結果**（コードが実行したもの）: G-1〜G-10 の合否と `detail`。テストの件数（A〜D）。
5. **修正役の説明**（見出しに「モデルの説明・未検証」）: `patch_kind` と `explanation`。`explanation` は、長さの上限で切り、`mask_secrets` を通し、制御文字を除き、`://` を `[:]//` に置き換える。
6. **確かめていないこと**（固定の一覧）: 実機での確認は未実施 / 診断の場所が正しいかは未確認（手がかりとして使った）/ 再現テストが、実際に起きた失敗と同じ原因を突いているかは未確認 / 既存のテストに無い経路への影響は未確認。
7. **足取り**: ツール名・パス・結果の並び。テスト実行は件数つき。

`rejected` のときも下書きを書く（3 は「コミットなし」、4 にどの検査で落ちたかが出る）。人が、なぜ落ちたかを見られるようにするため。

- `result.json`: `FixRun` を JSON にしたもの。`trace.json`: 足取り。
- 本番の記録（events テーブル、S3）には書かない（R18）。
- 終了時にログ 1 行（`status`・`stop_detail`・変更したファイルの数・ツールの回数・ターン数・所要時間）。

## 13. 手動実行と文書

```bash
PYTHONPATH=src/agent .venv/bin/python -m src.fixer \
  --handoff /path/to/handoff.json --out-dir /path/outside/repo/fix-001 [--base <commit>] [--repo .]
```

- 終了コード: `proposed` は 0、それ以外の結果は 1、引数の誤りは 2。
- 標準出力には、終了理由、ブランチ名、検査の結果、出力先を出す（制御文字を除く）。
- 文書: `docs/execution-fixer.md`（新規）。中身は、前提（**本番の認証情報がない環境で動かす**。AWS のプロファイルや GitHub のトークンを持たないシェル、またはコンテナ）、サブスクリプション認証、受け渡しのファイルの作り方（第 1 段階の `--handoff-out`）、実行例、出力の読み方、人が行う 2 コマンド（`git push origin fix/diagnosed-<実行ID>` と PR の作成）、取り込む前に規律どおり行うこと、不要になったブランチの消し方、既知の制限。
- pytest の既定実行では走らない。

### 13.1 実際に直させる確認（任意条件 O1）の題材

過去の不具合は、events テーブルの導入前後のものなので、受け渡しは手で用意する。

1. 修正前のコミットを特定する（該当の steering の tasklist と `git log` から。人が行う）。
2. そのコミットのコードで、不具合のあった関数の名前と、その範囲内の行番号を調べる。
3. 次の形の JSON を書く（`execution_id` は形に合う仮の値でよい）。

```json
{
  "schema_version": 1,
  "execution_id": "exec-20260418123456-abcdef12",
  "failed_stage": "storing",
  "cause_category": "external_service_error",
  "cause_nature": "code_defect",
  "confidence": "high",
  "exception_type": "HTTPError",
  "code_locations": [{"path": "/app/storage/notion_client.py", "function": "<2 で調べた関数名>", "line": 123}],
  "action_type": "fix_code",
  "valid_evidence_count": 1,
  "fixer_eligible": true
}
```

   `line` の `123` は例。2 で調べた、その関数の範囲内の行番号に置き換える（範囲の外だと、場所の再検証で捨てられる）。

4. `--base <修正前のコミット>` を付けて実行する。

| 題材 | steering | 見たいこと |
|---|---|---|
| Notion のブロック長の上限 | `.steering/20260418-notion-block-length-limit/` | 直す場所が 1 ファイルに閉じた、小さな修正を出せるか |
| 生成結果が dict でない場合の `TypeError` | `.steering/20260512-parse-claude-response-dict-contract/` | 実際の対処は構造の変更に及んだ。症状への対処を出すか、`declined` を返すか |

注意: 古いコミットを、今の開発用の環境（今の依存）でテストすることになる。基準の時点で落ちるテストが多くても G-7 は通りうるが、環境の違いで再現テストが書けない場合がある。その場合は、結果をそのまま記録する。

## 14. テストの方針

置き場所: `tests/unit/fixer/`。

| 差し替える境界 | 方法 |
|---|---|
| SDK | `src.fixer.runner._invoke_sdk` を、台本どおりにツールを呼ぶ偽物に差し替える。**ツールの呼び出しは、実物の MCP サーバーを通す**（第 1 段階の `tests/unit/agent/diagnosis/sdk_fake.py` と同じやり方。サーバー名が違うので、修正役用の偽物を `tests/unit/fixer/sdk_fake.py` に置く。共通の部品は第 1 段階のものを import してよい） |
| テスト・リントの実行 | `run_fix` の `test_runner`・`lint_runner` に偽物を渡す（決めた `TestReport` と件数を返す）。大半のテストは、これで実物の pytest を起動しない |
| git | **実物を使う**。テストごとに、一時ディレクトリに `git init` した小さなリポジトリを作る（`src/agent/sample.py`、`tests/unit/test_sample.py`、`pyproject.toml` など数ファイル） |
| 時計 | `clock` 引数 |

- **偽の修正役**: 台本は「`read_file` → `write_file`（新しいテスト）→ `run_tests` → `replace_in_file`（修正）→ `run_tests` → `finish`」のように、ツールを順に呼ぶ関数。合否の検査で落ちる差分（範囲外、禁止の場所、既存のテストの変更など）は、ツールが書き込みを拒否するので、**台本の中で複製のファイルを直接書き換えて**作る（修正役が走らせたテストのコードが書き換えた、という状況に当たる）。
- **検査の部品**（`check_static`・`check_dynamic`）は、`ChangeSet` と `TestReport` を手で作って確かめる（実物の実行なし）。
- **通しの確認**（実物の git と、実物の pytest・ruff を使う）は、数件に絞る: すべて通って `proposed` になる / 修正前でも通るテストで G-5 に落ちる / 既存のテストを壊して G-7 に落ちる / テストから見える環境変数（AC-8）。小さなリポジトリなので、1 件あたり数秒。
- **git の隔離**: テストでは `GIT_CONFIG_GLOBAL` を空のファイルに、`GIT_CONFIG_NOSYSTEM=1` にし、作者の名前とメールは、作ったリポジトリの設定に入れる（開発者の手元の設定・署名・フックに左右されないようにする）。
- **ネットワークの禁止**（AC-26）: 第 1 段階の conftest と同じく、`socket.socket.connect`・`connect_ex`・`socket.create_connection`・`socket.getaddrinfo` を止める（`socket.socket` そのものは止めない）。
- **リモートに触れる操作が 1 度も実行されないこと**（AC-22）: `tests/unit/fixer/conftest.py` の自動適用フィクスチャで、`subprocess.run`・`subprocess.Popen` を、引数を記録してから本物を呼ぶものに差し替える。テストの終わりに、記録した git の呼び出しのサブコマンドが、§5 の決まった一覧（とテストの準備が使う `init`・`add`・`commit`・`config`）に収まっていること、`push`・`fetch`・`pull`・`clone`・`remote`・`ls-remote`・`submodule` が無いことを確かめる。あわせて、`Git` が一覧にないサブコマンドを拒否することを直接確かめる。
- **フックが走らないこと**（AC-21）: 小さなリポジトリの `.git/hooks/pre-commit` と `commit-msg` に、目印のファイルを作るスクリプトを置き、`proposed` の後に目印が無いことを見る。
- **元のリポジトリが変わらないこと**（AC-20・21・28）: 実行の前後で、`HEAD` の指す先、現在のブランチ、`git status --porcelain` の出力、作業ツリーのファイルの一覧と中身、インデックスのファイルのハッシュを比べる。
- **既存のテストの土台の癖**: `tests/unit/trigger/conftest.py` が、収集のときに `boto3.dynamodb.conditions` を `sys.modules` から外す。修正役のテストは `boto3` のクラスの同一性に頼らないので影響しないが、偽物を作るときは、モジュールのクラスの同一性に頼らない。
- 基準のコマンドとリントは、第 1 段階と同じ（変更前は 1610 passed）。

## 15. 影響範囲

| 対象 | 影響 |
|---|---|
| `src/agent/`、`src/observability/`、`template.yaml`、`Dockerfile`、`pyproject.toml`、依存の定義 | 変更なし |
| 本番のイメージ・本番の挙動 | 変化なし（`src/fixer/` は入らない） |
| 元のリポジトリ | `proposed` のとき、ブランチが 1 つと、そのコミット・オブジェクトが増える。作業ツリー・現在のブランチ・インデックスは変わらない |
| 既存のテスト | 変更なし |

## 16. 既知の制限

（2026-10-05、レビュー指摘への対応（§20）にあわせて、実態に合うよう書き直した。同日、最終の再レビューへの対応（§21）にあわせて、位置づけと、隔離された環境の条件を足した。）

### 16.0 位置づけ

- **修正役は、「隔離された環境で、試験的に動かせる段階」であり、ふだんの開発の環境で使えるものではない。**
- レビューを重ねるたびに、「修正役が書いたテストのコードが、修正役自身の処理（検査、コミット、後始末）を騙す・踏み台にする」経路が見つかっている。1 巡目（§20）: 検査用の複製の書き換え、フックの実行。2 巡目（§21）: 仮想環境への書き込み、置換参照、フィルター。
- **§21 の修正の後も、この種の経路が残っていないとは言えない。** §21 の修正そのものは、再レビューを受けていない。
- 根本の解決は、テストの実行を、通信もファイルも遮断した実行環境（サンドボックス）に移す設計の変更で、別の作業単位である。それまでは、動かす環境の側で隔離する（§16.1）。

### 16.1 前提: 隔離された環境で動かす

- **修正役が書いたテストは、開発者本人の権限で、通信を含む任意のコードとして走る**。元のリポジトリの `.git`、開発用の仮想環境、シェルの設定にも書ける。複製の外を読んで、テストの出力としてモデルに返すこともできる（読み書きのツールの縛りは、テストのコードには効かない）。環境変数の `HOME` を差し替えても、本物のホームは分かる。`setsid` でグループを抜けた子プロセスは止められない。環境変数とホームの絞り込みは、緩和であって隔離ではない。
- **使い捨てのコンテナか、専用のユーザーで動かす**。リポジトリは複製を使う。プッシュの認証情報・`~/.ssh`・`~/.aws` を置かない。**Claude のサブスクリプションの認証は、同じ環境に必ず要るので、それが読まれうる**。コードでは塞げない。
- **検査に使う Python の環境（仮想環境と Python 本体）を、テストを走らせる利用者から書き込めない所有者にする**。コンテナなら、環境は root の所有にし、修正役の実行は別の利用者で行う。同じ利用者のまま読み取り専用にするだけでは足りない（テストのコードが、権限を戻せる）。書き込めると、site-packages に `.pth` を置くなどして、以後のリントとテストのすべてで先に動くコードを仕込め、仕掛けは提案の差分に現れない（§21 の H1）。
- **リポジトリは、部分クローンでない完全な複製を使う**（足りないオブジェクトの取得は、止めてある。§21 の H3）。
- うっかりを防ぐ歯止めとして、本物のホームに認証情報らしいもの（`.aws`・`.ssh` など 9 種）があると、既定では起動を断る（`unisolated_environment`。§20 の F11）。`.claude` は、上の理由で一覧に入れていない。検査に使う Python の環境（`sys.prefix` と site-packages）が、いまの利用者から書き込めるときも、同じく断る（§21 の H1）。見るのは、この 2 か所の権限だけである（Python 本体の場所（`sys.base_prefix`）や、標準ライブラリのディレクトリは見ていない）。**どちらも歯止めであって、隔離の代わりではない。**

### 16.2 合否の検査の限界

- **成否は、テスト対象のプロセスが書ける JUnit の XML と、終了コードから読む**。修正されたコード（と、それが読み込むモジュール）は、そのプロセスの中で動くので、結果を偽れる（XML を自分で書いて `os._exit(0)` する、など）。最後の守りは、人によるプルリクエストの確認である。
- **検査用の複製の書き換え（F1）の、残る限界**。複製は、使う実行の直前に 1 つずつ作り、実行の前後で中身を照合する。それでも、`setsid` で残ったプロセスが、次の複製が作られるのを待ち伏せして書き換え、照合の前に戻す、という競合は防げない。
- **元のリポジトリに置かれたフック・設定（F2）の、残る限界**。修正役は、置かれたフックを実行せず、変わっていたら git の書き込みをしない（`repository_tampered`）。しかし、置かれたものは、元のリポジトリに残る（修正役は、元のリポジトリを書き換えない）。`repository_tampered` や、終わりの注意（`repository_changed`）が出たら、`.git/hooks` と `.git/config` を確かめてから、次の git の操作をすること。照合するのは `config`・`config.worktree`・`hooks`・`info/attributes` で、`.git` のそれ以外（オブジェクト、参照、`info/exclude` など）は見ていない。
- **「本体で失敗し、後始末でもエラー」のテストが 1 件でもあると、提案できない**（§21 の H6）。実物の pytest は、そういうテストについて、同じ ID の結果を 2 つ書く。同じ ID の結果が複数ある実行は「検査できなかった」として扱うので、既存のテストにそういうものがあると、G-7 に必ず落ちる（安全な側の割り切り）。
- **置換参照（H2）と、フィルター（H4）の、残る限界**。git は、置換参照を読まず、足りないオブジェクトを取りに行かない。コミットを作るときは、作ったツリーを、実行の最初に取った基準のツリーの記録と比べる。モデルが書いたコードが走った後は、`git archive` を呼ばない。しかし、置かれた置換参照や設定は、元のリポジトリに残る（人がふつうに使う git は、置換参照を読む）。照合の対象に、`refs/replace/` は入っていない。照合の後、コミットの一連（10 回ほどの git の呼び出し）の間に、`setsid` で残ったプロセスが設定を書き換える競合は、防げない。
- **独立した確認（G-5・G-6）は、無関係な「落ちるテスト」と、それを通すだけの変更を見分けられない**（§10.3）。
- **G-5 を厳しくした結果、まだ無い関数やモジュールを足す種類の修正は、成果物にならない**。新しいテストが、修正の前に「集めるときのエラー」（終了コード 2）になるためである。新しいテストは、テストの失敗（終了コード 1）として落ちなければならない。
- **G-7 を文字どおりに読む**。基準の時点で落ちていたテストが、修正で通るようになった場合も不合格になる（安全な側。その修正は、人が取り込む）。基準と修正後のどちらかで、テストが集まらない・終了コードが 0 でも 1 でもない場合も、不合格になる。
- **既存のテスト 2 件が、検査の実行では落ちる**。`TestCallClaudeWithWorkspace::test_creates_and_cleans_sandbox` と `::test_passes_cwd_in_options` は、作業ディレクトリが `/tmp/agent-output-` で始まることを求めるが、検査では `TMPDIR` が `/tmp/fixer-run-*/tmp` になる（§8.2 の「落ちないようにする」は、実現できていない）。基準と修正後で同じように落ちるので、G-7 は通る。**この 2 件は、修正で壊れても検出されない**。筋のよい直し方は、その 2 件のテストの側を `tempfile.gettempdir()` から組み立てることで、元からあるテストの変更になるため、ユーザーの判断事項である。
- **検査 1 回の所要時間は、このリポジトリでは 5〜6 分**。テスト一式が 1 回 2 分半ほどかかり（修正役自身のテストを含む。2026-10-05 の実測で 150 秒）、基準と修正後で 2 回走る。合計の上限は `gate_timeout_seconds`（既定 900 秒）。
- **検査の実行が、複製の中にファイルを残す既存のテストがあると、G-7 に必ず落ちる**（実行の前後の照合のため）。このリポジトリのテスト一式は、複製を書き換えない（2026-10-05 に、作業ツリーの写しで確かめた）。
- **順番に左右される既存のテスト**。A と C で条件を揃えているが、修正がモジュールの読み込み順を変える場合は、無関係なテストの成否が変わって G-7 に落ちうる（安全な側に倒れる）。
- **リントの比べ方は件数だけ**。指摘が 1 件減って 1 件増えた場合は通る。

### 16.3 入力（診断と場所）の限界

- **診断役の申告は、誘導されうる**。記録の中の指示にモデルが誘導されれば、通常の失敗のほとんどで「修正役に渡せる」は真になりうる（判定は、例外・場所・根拠のつながりを見ているだけで、「不具合かどうか」はモデルの申告である）。修正役は、この申告を信じない（§10.3）。
- **場所は手がかりにすぎない**。第 1 段階は、スタックトレースの最初のブロックからだけ場所を取るが、記録そのものを書き換えられる相手は、1 行目から偽のトレースを書ける。誤った場所を指されたとき、修正役は「直せない」と返すか、無関係な修正案を出す。
- **根本策は別の作業単位**。記録する側が、文字列のトレースではなく、`traceback.extract_tb` で取り出した場所と例外の型を、payload の別の項目として残す（`requirements.md` §7）。
- **版のずれ**。基準のコミットが、失敗した実行の版と違うと、場所の行番号が関数の範囲から外れて捨てられる（→ `not_eligible`）。`--base` で、失敗した実行に載っていたコミットを指定できる。

### 16.4 直せる範囲と、環境への依存

- **直せる範囲が狭い**。エラーメッセージの文面を渡さない。既存のテスト・`conftest.py`・設定を変える必要がある修正、`main.py`・`src/trigger/app.py`・認証まわり・診断役・修正役自身の修正、`src/` の下の既にある `__init__.py` の変更、実行権限のあるファイルの修正は、対象外。`src/` と、その直下のディレクトリの直下には、標準ライブラリ・依存パッケージ・ほかの根のモジュールと同じ名前のファイルやディレクトリを足せない（正当な名前でも、当たれば断る）。
- **`export-ignore`・`export-subst` を使うリポジトリは、扱わない**（`unsupported_attributes`）。`git archive` がこれらの属性に従い、複製の中身がコミットの中身と食い違うためである。1,000,000 バイトを超える `.gitattributes` があるコミットも、扱わない（§21 の H7）。`.git/info/attributes` は、先頭の 1,000,000 バイトだけを見る（それより後ろの行は、見落とす）。
- **依存の名前の一覧（H5）の限界**。import の乗っ取りになる名前は、手書きの一覧と、基準のコミットの `requirements*.txt` の配布物の名前から作る。配布物の名前と import の名前が違うもの（`PyYAML` → `yaml` など）は、手書きの一覧にあるものしか拾えない。依存の依存（`requirements*.txt` に書かれていないもの）、1MB を超える定義、`requirements*.txt` 以外の定義（`pyproject.toml` など）も、拾えない。
- **パスの要素に、ピリオドを使えない**（H6）。変更してよいパスは、最後の要素の末尾の `.py` のほかに、ピリオドを含められない。
- **コミットに入る中身は、フィルターを通さない**。`hash-object` に `--path` を付けないので、`.gitattributes` の行末の正規化や `filter` は効かない。CR を含む中身は、G-10 で不合格にする。
- **開発用の環境に依存する**。検査のテストは、修正役を動かしている Python と、その依存で走る。基準のコミットが古いと、依存の違いで結果が変わりうる。
- **大文字と小文字を区別しないファイルシステム**（macOS の既定）への備えは、対象外の一覧と `tests/` の既存のファイルを、大文字と小文字を畳んで比べることと、書き込み先の綴りを、ディレクトリの一覧と突き合わせることである。Unicode の正規化の違いは、既にあるパスについてだけ、一覧との突き合わせで断る。
- **パスの解決と使用の間の入れ替え**（テストのコードが残したプロセスによる）は、防いでいない。
- **SDK の未確認の点**（`tools=[]` の実際の挙動、初期化のメッセージのツール一覧）は、第 1 段階と同じ備えで、どちらでも壊れない。実際に確かめるのは、手動確認のとき。

## 17. ADR について

**ADR-0003 を起こすことを推奨する**（作成はユーザーの承認事項。本設計では作らない）。

- 題: 実行記録を読む役と、コードを書き換える役を分ける。
- 決定の中身: (1) 記録を読む役は読み取りだけ、コードを書き換える役は記録の生の文面を受け取らない。(2) 間は、決まった値と、コードが記録から取り出して実際のコードと突き合わせた識別子だけでつなぐ。(3) 修正の合否は、コードが実際の実行結果で決める。(4) 修正は、手元から自動では出さない（プッシュと取り込みは人）。
- 3 条件: 安全の境界であり、いったん緩めると戻しにくい / 文脈がないと「なぜエラーメッセージを渡さないのか」「なぜ読み書きのツールを自作したのか」が分からない / 直せる範囲を狭める代わりに安全を取る、本物のトレードオフ。
- 今がよい理由: 第 1 段階のレビューで、この境界をすり抜ける経路（偽のフレーム）が実際に見つかり、境界の引き方が具体的になった。残る弱点（場所を記録の側から選べる）も、ADR に書いておく価値がある。

## 18. 要求定義から変えた点

`requirements.md` §9 に一覧がある（18 点）。設計で新たに決めた主な点:

| 点 | 決めた内容 | 理由 |
|---|---|---|
| 複製の作り方 | `git archive` で書き出す。`git worktree` も、`.git` を含む複製も使わない | 複製に `.git` があると、修正役が走らせたテストのコードが、フックや設定を書き換えられる。`worktree` は、元のリポジトリの `.git` につながる |
| コミットの作り方 | 一時的なインデックスと、低レベルの操作（`commit-tree`・`update-ref`） | 作業ツリー・現在のブランチ・インデックスに触れず、フックも走らない |
| 読み書きのツール | 自作（7 つ） | `can_use_tool` は文字列のプロンプトで使えず、許可済みのツールには呼ばれない。組み込みツールを複製の中に確実に閉じ込める手段を確かめられなかった |
| 出力先 | リポジトリの外を必須にした | 元の作業ツリーに、未追跡のファイルを増やさないため |
| `finish` ツール | 終了の申告と、症状への対処か構造の修正かの申告を、ツールで受ける | 最終テキストから読み取らない（第 1 段階の提出と同じ考え方）。CLAUDE.md 作業規律 §2 が求める「症状パッチか構造修正かの明示」を、下書きに載せるため |
| G-9 の数え方 | 空の `__init__.py` は、ファイル数に数えない | 新しいテストのディレクトリを作るときに要るため |

### Codex のクロスレビュー（第 1 段階）を受けて決めた点

| 点 | 決めた内容 | 理由 |
|---|---|---|
| 例外の型 | 許可一覧と照合する（§4.3）。一覧にない・型が無い受け渡しは `not_eligible` | 形だけの検証では、記録の中の文を「例外の型」として通せた |
| 対象外の場所 | 安全の仕組みと、認証・秘密に関わるファイルを、ファイルの単位で対象外にする。受け渡しの場所として来たら取り除き、書き込みのツールで拒否し、G-2 で不合格にする（同じ一覧を 3 か所で使う。§6.2） | 実在する場所を偽装して、修正役の作業対象を安全の仕組みに向けられた |
| 診断の申告の扱い | 信じない。独立した確認は G-5・G-6 が担う、と位置づけた（§10.3） | 「修正役に渡せる」は、構造のつながりを見ているだけである |
| 指示文の言い方 | 場所を「失敗が観測された場所（手がかり）」として渡す（§11） | 「ここを直せ」と読める渡し方をしない |
| 下書きの固定の文面 | 「再現テストが実際の失敗と同じ原因を突いているかは確かめていない。最後の確認は人が行う」を足した（§12） | 自動の検査の限界を、プルリクエストを読む人に必ず伝える |

## 19. 実装時に決めた点

（2026-10-05、developer。番号は、この節の中の通し番号。）

> §20（レビュー指摘への対応）で改めた点がある: 9（G-7 の条件を厳しくした）、13・34（下書きの説明は、引用の形をやめ、囲みのコードブロックに入れる）、23（`list_files` で複製の根を一覧できる）、28（プロセスは `process_group=0` で起こす。ID は切らない。出力は、開いたままのファイル記述子から読む）。食い違うところは、§20 が正しい。
> §21（最終の再レビューへの対応）で改めた点がある: 15（パスの要素に、ピリオドを使えない）、28（同じ ID に結果が 2 つあるときは、「検査できなかった」とする）。§20 の F1 の「検査用の複製」は、git を呼ばずに、メモリに持った書き出しから組み立てる。食い違うところは、§21 が正しい。

### 19.1 D0-1（前提の確認）の結果

| # | 確かめたこと | 結果 |
|---|---|---|
| 1 | `diagnosis.frames.FrameVerifier` を、`code_root` に任意のディレクトリを渡して使えるか | 使える。`FrameVerifier(code_root=<複製>/src/agent)` と `FrameVerifier(code_root=<複製>)` の 2 つを作って、順に試している（§4.2） |
| 2 | 例外の型の許可一覧を作る関数が、第 1 段階から公開されているか | **一覧を返す関数は公開されていない**。公開されているのは、判定だけを返す `ExceptionTypeAllowlist(code_root).allows(name)` と、定数 `LIBRARY_EXCEPTION_TYPES`（23 個。§4.3 の 15 個をすべて含む）。組み込みの例外の一覧と、コードの走査は、第 1 段階では非公開である。→ **依存ライブラリの一覧は、第 1 段階の定数を import して使い、複製のコードの走査（`<複製>/src/` の下）は、修正役の側に持った**（`allowed_exception_types` が `frozenset` を返す必要があり、根と返す形が違うため。走査の決まり（隠しディレクトリ・`__pycache__` を飛ばす、2,000 ファイルまで、1MB まで、根の外を指すものは読まない）は、第 1 段階と同じにした） |
| 3 | 受け渡しの形が、§4.1 の 11 項目と一致するか | 一致する（`DiagnosisHandoff.to_dict()`） |
| 4 | §5 の git の操作が、手元の git（2.50.1）で使えるか | 使える（一時ディレクトリのリポジトリで確かめた）。`commit-tree` は、作者の設定が無いと、ホスト名などから推測する。推測させないために `-c user.useConfigOnly=true` を付けている |

### 19.2 設計に無かった点・矛盾していた点（tester の読み方を、司令塔が承認したもの）

| # | 点 | 決めた内容 | 理由 |
|---|---|---|---|
| 1 | G-9 が消した行を数えるのに、`check_static` に元の中身が渡らない（§10） | `Workspace.baseline` を、相対パス → `BaselineEntry`（種類・モード・大きさ・SHA-256・元の中身）の `Mapping` にした。キーの集合は、基準のコミットの全ファイル。元の中身は 1MB までのファイルだけ持ち、持っていないファイルが変更されたら、G-9 を不合格にする | `check_static(changes, baseline, limits)` の形を変えずに、消した行を数えるため |
| 2 | AC-12 の禁止の場所が、§6.2 の一覧に無い | 対象外の一覧（`gates.FORBIDDEN_PREFIXES`・`FORBIDDEN_PATHS`・`FORBIDDEN_NAMES`）に、`.github/`・`.steering/`・`docs/`・`template.yaml`・`CLAUDE.md`・`pyproject.toml`・`.gitleaks.toml` を足した。`Dockerfile` と `requirements*.txt` は、どのディレクトリにあっても対象外 | `requirements.md` の AC-12 と §4.3 の G2 に従う |
| 3 | 作者の設定が無いときの決まった名前（§5.2 の 4） | まず `git -c user.useConfigOnly=true commit-tree` を試し、失敗したら、作者・コミッターを環境変数で `catch-expander-fixer` にして、やり直す | 設定の有無を調べるサブコマンド（`config`）を、決まった一覧に足さずに済ませるため |
| 4 | 起動の前の確かめの順（§9.3 の 1・2） | リポジトリ → 基準のコミット → 出力先 → **受け渡しの読み込みと検証** → ブランチの有無、の順。`branch_exists` のときも、出力先に何も書かない | ブランチ名は、受け渡しの実行 ID から決まるため |
| 5 | `is_eligible(handoff, locations)` に、複製の根が無い（§4.4） | `is_eligible` は、例外の型が「ある」ことだけを確かめる。許可一覧との照合は `is_known_exception_type(name, root)` で行い、`run_fix` が両方を確かめる（例外の型 → 場所 → そのほかの条件、の順） | 関数の形を変えないため |
| 6 | `../` を含むパスや、200 字を超える関数名（AC-3） | `run_fix` では、形の検証で落ちて `failed`・`invalid_handoff` になる。`verify_locations` 単体も、形に合わない場所を捨てる | 形の検証が先に走るため |
| 7 | `run_tests` のパス（AC-7 と §7） | 複製の外・`tests/` の外は `denied`、無いパスは `not_found`、`-` で始まる値と `::` を含む値は `invalid_input`。どれも、何も実行しない | 読み取りのツールと、分類を揃えるため |
| 8 | 整数の項目 | 真偽値と小数（`true`、`1.0`）は通さない（受け渡しの `schema_version`・`line`・`valid_evidence_count`、ツールの `start_line`・`max_lines`） | 型の取り違えを通さないため |
| 9 | G-7 の「等しい」（§10.2） | 文字どおりに読む（落ちていたテストが通るようになっても不合格）。加えて、A と C の終了コードが同じで、どちらも時間切れでなく、終了コードが取れていることを求める | 成否を確かめられなかった実行どうし（どちらも XML なし）を「同じ」と扱わないため |
| 10 | 足取りの `path` | 残すのは、コードが解決した相対パスで、決まった文字だけのもの。`denied`・`invalid_input`・`call_limit`・`not_found` のときは `None`。`run_tests` は、走らせたパスを空白でつないだもの | 形を確かめていない文字列を、足取りと下書きに入れないため |
| 11 | SDK が `TimeoutError` を出したとき | `sdk_error`（全体の時間切れ `timeout` と区別する。第 1 段階 §17 の 14 と同じ包み方） | 取り違えないため |
| 12 | テストの件数（R12） | `GateResult.detail` に入れる（G-5〜G-7） | `GateResult` の形を変えないため |
| 13 | 下書きの説明の処理順（§12 の 5） | 制御文字を除く → マスク → 切り詰め、の順にした（§12 は「切ってからマスク」） | 上限をまたぐトークンが途中で切れて、マスクを逃れるのを防ぐため（第 1 段階と同じ順） |

### 19.3 実装で決めた点

| # | 点 | 決めた内容 | 理由 |
|---|---|---|---|
| 14 | 大文字と小文字を区別しないファイルシステム | 対象外の一覧との照合は、大文字と小文字を畳んで行う（`src/agent/MAIN.py` も対象外）。`tests/` の既存のファイルかどうかも、畳んで比べる。書き込み先は、途中の要素と末尾が、その綴りのまま親ディレクトリの一覧にあることを確かめ、別の綴りなら `denied`。読み取りは、別の綴りなら `not_found` | macOS の既定のファイルシステムでは、別の綴りで同じファイルに届くため |
| 15 | 変更してよいパスの形 | 各要素が英数字か下線で始まり、英数字・下線・ピリオド・ハイフンだけからなること。全体で 200 字まで。書き込みのツールが断り、G-1 も不合格にする。結果・下書きに出すパスは、決まった文字だけで、秘密情報らしい部分のないものに限る（それ以外は `<other>`） | コミットメッセージと下書きに入るパスは、修正役が決められる文字列なので、形を限る |
| 16 | `hash-object`（§5.2 の 1） | `--path` を付けない（フィルターを通さない） | コミットに入る中身を、検査を通った中身そのものにするため。`filter` の設定で、外部のコマンドが走るのも避ける |
| 17 | `commit-tree`・`update-ref` の引数 | `commit-tree --no-gpg-sign`、`update-ref --no-deref` | 署名の設定で、鍵の入力待ちにならないようにする。シンボリックな参照をたどって、別の参照を書き換えないようにする |
| 18 | `not_a_repository` の判定 | `rev-parse --show-toplevel` の結果が、渡された `repo` と同じディレクトリであること（`Git.toplevel()` を足した） | リポジトリの下位のディレクトリを渡されて、親のリポジトリを取り違えないようにする |
| 19 | 実行権限のあるファイルの変更（G-10） | 基準で実行権限のあるファイルを変更したら、不合格 | コミットは常に `100644` で作るので、権限が黙って落ちるのを防ぐ |
| 20 | `tests/` の下の `__init__.py` | 書き込みのツールは、空でない中身を `denied` にする | G-3 で落ちる変更を、その場で断る |
| 21 | `replace_in_file` の結果 | ファイルが無い → `not_found`（`missing: file`）。`old` が 0 か所 → `not_found`（`missing: old`）。2 か所以上 → `invalid_input`（`invalid_fields: ["old"]` と、`occurrences`） | モデルが、理由を見分けて出し直せるようにする |
| 22 | `run_tests` | 指定できるのは、ディレクトリと `.py` のファイルだけ。結果に `skipped` と `exit_code` も返す。落ちたテストの名前は 1 件 200 字まで。足取りの `tests_failed` は、失敗とエラーの合計 | `.txt` を指定すると、pytest が doctest として集めるため。集めるときのエラーを、モデルが終了コードで見分けられるようにする |
| 23 | 読むツールの上限 | `read_file` は、2MB を超えるファイルを `size_limit` にする。行は LF で分け、`max_read_bytes` は行番号を含めて数え、超える行は途中で切る。`max_lines` は 2,000 まで。`list_files`・`search_code` は、結果がおよそ 16,000 字を超えたら打ち切る。検索は、1 行 200 字まで、5,000 ファイルまで、1MB までのファイル | ツールの結果の大きさを、入力に依らず抑えるため |
| 24 | 通常のファイルでないもの | 読むのは通常のファイルだけ（`O_NONBLOCK`・`O_NOFOLLOW` で開き、`fstat` で確かめる）。`list_files` は、種類を `other` と返す。`search_code` と変更の取り出しは、シンボリックリンクをたどらない | 複製の中の FIFO を開いて、止まらないようにする |
| 25 | 変更の取り出し | 取り出す中身の合計は 20MB まで（超えたぶんは `oversized`）。読めないファイル、ディレクトリに置き換わったファイルは `irregular`。`__pycache__` などの中でも、基準の一覧にあるパスは比べる | 大量のファイルを置かれても、メモリを使い切らないようにする |
| 26 | `finish` | `patch_kind` は、`declined` のときも必須 | §7 の表のとおり（入力の形を 1 つにする） |
| 27 | ツールのセッションを閉じる | 修正役の実行が終わったら（時間切れを含む）、`FixToolSession.close()` を呼ぶ。処理の途中の呼び出しが終わるのを待ち、これ以降の呼び出しは、複製に触れずに `internal_error` を返す | 変更を取り出す間や、複製を消した後に、遅れて届いた呼び出しが複製に触れないようにする |
| 28 | テストとリントのプロセス | 標準出力はファイルに書かせ、末尾だけを読む。プロセスは専用のグループで起こし、終わったとき（時間切れを含む）に、回収する前にグループごと止める。時間切れの実行の XML は使わない。XML は 20MB まで、テストは 100,000 件まで、ID は 500 字まで。同じ ID に結果が 2 つあるときは、悪いほうを残す | 出力をメモリに溜めない。テストのコードが起こした子プロセスを残さない |
| 29 | リントの数え方 | 終了コードが 0 か 1 のときだけ数える。標準エラーは捨てる。検査では、その複製でテストを走らせる前に数える | 実行の失敗を「指摘 0 件」と取り違えない。テストのコードが複製を書き換えても、数に響かないようにする |
| 30 | 検査の実行の側が例外を出したとき | テストは「成否を確かめられなかった」結果（`exit_code` が `None`）として扱い、リントは `None` として扱う（G-5〜G-8 が不合格になり、`rejected`）。検査用の複製を組み立てられなかったときは `failed`・`setup_error` | 例外を外に出さず、通さない側に倒す |
| 31 | 終了理由の追加 | 想定外の例外は `failed`・`internal_error`。`ResultMessage` の `is_error` が真で、`subtype` が上限のものでないときは `failed`・`sdk_error` | §9.3 の表に無い場合を、通さない側に倒す |
| 32 | 受け渡しの読み込み | 同じ名前の項目が 2 つある JSON、パスの空の要素（`/app//x.py`）を拒否する。通常のファイルでないものは読まない。`HandoffError` には、元の例外をつながない | 合わないものは、すべて拒否する。`UnicodeDecodeError` は、ファイルの中身を引数に持つ |
| 33 | 指示文の組み立て | `build_user_prompt` の中でも、差し込む値の形（決まった値の一覧、識別子、パスの形、行番号）を確かめ、合わないものは差し込まない | 検証を通さずに組み立てた値が渡されても、指示文に入らないようにする |
| 34 | 下書きのモデルの説明 | 引用の形（各行の先頭に `> `）で入れる。制御文字、行・段落の区切りの文字（U+2028・U+2029）、単独のサロゲートを除き、`://` を `[:]//` に、`<`・`>`・`@` を全角に置き換える | 見出しや固定の文面を偽造できないようにする。リンクとメンションが効かないようにする |
| 35 | 出力先のファイル | 既にあるファイルは上書きしない（`"x"` で開く）。`changed_files` は 50 件まで | 出力先は、起動の前に空であることを確かめている |
| 36 | ツールの結果の文字列 | UTF-8 にできない文字（ファイル名に由来する単独のサロゲートなど）は、置き換える | SDK に渡すときに、例外にならないようにする |

## 20. レビュー指摘への対応

（2026-10-05。reviewer と Codex のクロスレビューで、どちらも独立に「このまま実際に動かす確認へ進むべきでない」と結論した。受け渡しの再検証・指示文の組み立て・ツールによる複製への閉じ込め・低レベルのコミットは堅い、という評価で、弱かったのは「修正役が書いたテストが、開発者の権限で実際に走る」ことから先である。司令塔が修正の仕様（F1〜F12）を決め、tester がテストを先に書いた。）

| # | 指摘（再現されたもの） | 直し方 |
|---|---|---|
| F1 | 検査用の複製 3 つを、同じ親ディレクトリの下に先に作っていた。実行 B（モデルが書いた新しいテスト）が、隣の `T_fix` を正しい実装に書き換え、壊れた修正が `proposed` になり、コミットされたのは壊れた版だった。実行 C と D が同じ複製を使う点も同じ | 検査用の複製は、使う実行の直前に、別々の `mkdtemp` の下に 1 つずつ作り、実行が終わったら、次を作る前に消す（`runner._GateTrees`）。C と D も別の複製。順は、リント（基準・修正後）と実行 A を先に、B・C・D を後に。各実行の前と後で、複製の中身（ファイルとディレクトリの一覧・種類・モード・SHA-256）を、基準の一覧と検査を通ったファイルから求めた中身と照合し（`workspace.expected_manifest`・`tree_matches`）、合わなければ、その実行の検査を不合格にする（A・C は G-7、B は G-5、D は G-6、リントは G-8）。作業した複製は、動的な検査の前に消す |
| F2 | 新しいテストが、元のリポジトリの `.git/hooks/reference-transaction` を書き、修正役の `update-ref` がそれを実行した | すべての git の呼び出しに `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null` を付け、環境変数に `GIT_CONFIG_GLOBAL=/dev/null`・`GIT_CONFIG_NOSYSTEM=1` を足した。作者は、リポジトリの中の設定にあればそれ、無ければ `catch-expander-fixer`。複製を作る前と、git の書き込みの直前で、`.git` の `config`・`config.worktree`・`hooks`・`info/attributes`（一覧・種類・モード・中身）の要約を比べ（`Git.repository_fingerprint`）、変わっていたら、書き込まずに `failed`・`repository_tampered`。照合は、終了理由に関わらず終わりにも行い、変わっていたら、WARNING のログと、結果の `repository_changed`（`result.json` にも入る）、入口の標準エラーへの注意で知らせる |
| F3 | 全体の制限時間 0.5 秒で、1 回 2 秒のテスト実行を 4 件並べて呼ぶと、`run_fix` が返るのは 8.1 秒後だった（ロック待ちの呼び出しが、時間切れの後も順に実行された） | 閉じる印を、ロックの外で先に立てる（`FixToolSession.stop_accepting`。受信ループを抜けるときに、イベントループが閉じる前に呼ぶ）。`FixToolSession` に `deadline` を足し、期限を過ぎてから始まる呼び出しは、何もせずに `deadline_exceeded` を返す。`run_tests` と検査の実行は、必ず `timeout_seconds=` を渡す（1 回の制限時間と、残り時間の短いほう）。検査の合計の上限 `FixLimits.gate_timeout_seconds`（既定 900 秒）を超えたら、残りは走らせず、対応する検査を不合格にする |
| F4 | `src/trigger/app.py`・`src/__init__.py`・`src/agent/__init__.py` を変えられた。`src/agent/secrets.py`（標準ライブラリと同名）と `src/trigger/diagnosis.py`（`pythonpath` の順で `diagnosis` を差し替える）を足せた | 対象外の一覧に `src/trigger/app.py` を足した。`gates.ChangeScope` を足し、場所の再検証・書き込みのツール・G-2 が、同じ判定（`forbids`）を使う: 対象外の一覧、`src/` の下の `__init__.py`（既にあるものは変更・削除できない。新しく足せるのは空だけ）、import の乗っ取りになる名前（`src/` と、その直下の基準にあるディレクトリの、直下に足す名前が、標準ライブラリ・ほかの根のモジュールやパッケージ・決まった依存の名前に当たる）。トークンを受け取る 3 ファイルを変えた成果物では、下書きの冒頭に、固定の注意書き（「認証情報を扱うファイル」）を入れる |
| F5 | G-7 が、基準と修正後のどちらも結果が空で通った。G-5 が、集めるときのエラー（終了コード 2）で通った。G-6 が、落ちたテストとは別のテストが通るだけで通った。テストの ID を 500 字で切って比べていた | G-7: 両方の終了コードが 0 か 1 で等しく、結果が 1 件以上あり、成否が等しいこと。G-5: 終了コードが 1 で、落ちたテストが 1 件以上。G-6: 終了コードが 0 で、すべて通り、実行 B で落ちたテストの ID が、すべて、実行 D で通った ID に含まれること。ID は全文で比べる。結果が上限（10 万件）を超えた実行は、「検査できなかった」（`outcomes` が空、`exit_code` が `None`）として扱う。指示文に、集めるときのエラーは認められないことを足した |
| F6 | テストが、出力ファイルを消して同名の FIFO を作ると、親が読み出しで待ち続けた。`$HOME` の下に権限 000 のディレクトリを作ると、`/tmp/fixer-run-*` が残った | 出力は、親が最初に開いたファイル記述子から読む（名前で開き直さない）。JUnit の XML は `O_NOFOLLOW`・`O_NONBLOCK` で開き、通常のファイルであることを確かめる。出力が `MAX_OUTPUT_BYTES`（5,000 万バイト）を超えたら、実行を止め、「検査できなかった」とする（`run_tests` は `size_limit`）。後始末は `workspace.remove_tree` で、権限を戻して消し切る |
| F7 | 行数の計算が、行数の 2 乗の時間だった（空行 8,000 行で 4.57 秒）。同じ計算を、G-9 と差分の要約とで 2 回していた | 先頭と末尾の同じ行を除き、残りを「両方に 1 回ずつだけ現れる行」で区切って、区間ごとに数える。区切れない小さな区間（行数の積が 25 万まで）は difflib で、大きな区間は、区間ごと変更として数える（多めに数える側）。結果は覚えておき、2 回計算しない |
| F8 | 複製の中で `src/agent` が `fixer` へのリンクのとき、`/app/gates.py` が `src/agent/gates.py` として検証を通った | 場所の再検証で、複製の根から対象のファイルまでの途中（検証の根と、末尾を含む）にシンボリックリンクがあれば、その場所を取り除く |
| F9 | 下書きの説明に、U+202E・U+2066・U+200B、`[x](//host/…)`、`![i](//host/p.png)` が、そのまま残った。G-10 は、双方向の制御文字を調べていなかった | 下書きのモデルの説明は、囲みのコードブロックに、そのまま入れる（引用の形と、`://`・`<`・`>`・`@` の置き換えは、やめた）。フェンスは、説明の中のどのバッククォートの連続よりも長くする。Unicode の分類 Cf・Cc（改行を除く）・Cs・Zl・Zp の文字は取り除く。G-10 と書き込みのツールは、U+202A〜U+202E、U+2066〜U+2069、U+200B〜U+200F、U+2060、U+FEFF、CR を含む中身を断る |
| F10 | 複製の根を一覧できない。ハードリンク越しに、複製の外のファイルを読み書きできる。`export-ignore`・`export-subst`。先の無いシンボリックな参照を「無い」と見る。`HEAD` などの名前でコミットを載せられる。割り込みで、一時ディレクトリが残る | `list_files` は、`.` と空文字で根を一覧できる。リンク数が 2 以上の通常のファイルは、読み書きとも `denied`（検索は飛ばし、変更の取り出しでは `irregular`）。基準のコミットの `.gitattributes`（どの階層でも）か `info/attributes` に、属性として `export-ignore`・`export-subst` があるか、書き出しの一覧がツリーの一覧と食い違えば、`failed`・`unsupported_attributes`（ツリーの一覧は、一時的なインデックスのファイルを読んで得る。`Git.tree_entries`）。`branch_exists` は、参照のファイルがあれば真。`create_branch_commit` のブランチ名は、`fix/diagnosed-exec-<数字 14 桁>-<16 進 8 桁>` だけ。割り込みでも、セッションを閉じ、一時ディレクトリを消し、テストのプロセスを止めてから、例外を上げ直す |
| F11 | 修正役が書いたテストは、開発者本人の権限で、通信を含む任意のコードとして走る（コードでは塞げない） | `run_fix(..., allow_unisolated=False)`。本物のホーム（OS の利用者情報から得る。`runner._real_home_directory`）に `.aws`・`.ssh`・`.netrc`・`.git-credentials`・`.config/gh`・`.docker/config.json`・`.codex`・`.kube`・`.gnupg` のどれかがあれば、何もせずに `failed`・`unisolated_environment`（見つけたものの名前だけを、ログに出す）。入口に `--allow-unisolated` を足した |
| F12 | プロセスを専用のセッションで起こす引数の名前（`subprocess.Popen` の、`start_new_…` で始まるもの）と、実行 ID の例が、gitleaks の独自ルールに当たる（誤検出） | テストとリントのプロセスは `process_group=0` で起こす（グループごとの停止は、今までどおり）。実行 ID の例を、当たらない値に変えた。書き出しの属性を調べる関数の名前（`export` と `attributes` を下線でつないだもの）も同じルールに当たったので、`has_archive_attributes` などに改めた |

### 20.1 この対応で決めた点

| # | 点 | 決めた内容 | 理由 |
|---|---|---|---|
| 1 | F2 の、終わりの照合の出し方 | `FixRun` に `repository_changed: bool`（既定は偽）を足した。`result.json` に入る。入口は、真のとき、標準エラーに注意を出す。ログは WARNING | 終了理由（`rejected` など）を変えずに、置かれたことを、機械的に読める形でも残すため |
| 2 | F2 で照合する範囲 | `config`・`hooks` に加えて、`config.worktree` と `info/attributes` | どちらも、git の動きを変える（後者は、書き出しの中身を変える） |
| 3 | F2 の `-c` | `core.attributesFile=/dev/null` も付ける | 利用者ごとの属性ファイル（`~/.config/git/attributes`）は、全体設定を読まなくても読まれるため |
| 4 | F5 の「検査できなかった」の表し方 | `TestReport` の `outcomes` を空に、`exit_code` を `None` にする（出力の超過は、加えて `output_overflow` を真にする）。検査用の複製が書き換えられた実行・走らせられなかった実行は、対応する検査の結果を、理由つきの不合格に置き換える | `check_dynamic` の形を変えずに、G-5〜G-7 を不合格にできる。G-6 の「実行 B で落ちたテスト」は、実行 B が終了コード 1 で終わったときだけ見る（集めるときのエラーの項目は、テストの ID ではないため。その場合は G-5 が不合格になる） |
| 5 | F4 の、新しい `__init__.py` | `src/` の直下・根の直下でも、空なら足せる（仕様どおり）。名前の照合からは `__init__` を除く | 仕様に従った。パッケージかどうかが変わる影響は、G-7 が見る |
| 6 | F4 の、名前の取り方 | ファイル名の最初のピリオドより前（`json.py`・`json.pyi` は、どちらも `json`）。大文字と小文字を畳んで比べる | 拡張子の違いですり抜けないようにする |
| 7 | F7 の、大きな区間 | 区切れない区間が大きいとき（行数の積が 25 万を超える）は、区間ごと変更として数える | 多めに数える側に倒す（G-9 に落ちる側） |
| 8 | F10 のツリーの一覧 | `read-tree` で作った一時的なインデックス（版 2）のファイルを、Python で読む | 一覧を出すサブコマンド（`ls-tree`）を、決まった 9 個に足さずに済ませるため。属性のファイル自身が `export-ignore` で隠されている場合も、一覧の食い違いで分かる |
| 9 | 作業した複製を消す時点 | 変更を取り出し、静的な検査を終えた後、動的な検査の前 | 検査のテストから、作業した複製が見えないようにする。検査とコミットは、取り出した時点の中身で行う |

## 21. 最終の再レビューへの対応

（2026-10-05。§20 の修正の後、Codex の再レビューで、§20 の指摘（F1〜F12）はすべて塞がっていることが確かめられた。あわせて、新しい指摘が挙がり、窓口が再現した。どれも、「修正役が書いたテストのコード」が、修正役自身の処理（検査、コミット、後始末）を騙す・踏み台にする経路である。司令塔が修正の仕様（H1〜H9）を決め、tester がテストを先に書いた。**この節の修正は、再レビューを受けていない**（§16.0）。）

| # | 指摘（再現されたもの） | 直し方 |
|---|---|---|
| H1 | 検査は、修正役を起動したのと同じ Python で走る。モデルが書いたテストが、その仮想環境の site-packages に `.pth` を置くと、以後のリントと実行 A〜D のすべてで、そのコードが先に動く。仕掛けは、提案の差分に現れない | 起動の前の確かめ（F11 と同じ場所。リポジトリや受け渡しを見る前）に、「検査に使う Python の環境が、いまの利用者から書き込めるか」を足した。`runner._writable_python_locations()` が、`sys.prefix` と site-packages（`sysconfig` の `purelib`・`platlib`）のうち、`os.access(パス, os.W_OK)` が真のものを返す（実パスが同じものは、1 度だけ）。1 つでもあれば、何もせずに `failed`・`unisolated_environment`（見つけたパスを、ログに出す）。`allow_unisolated=True` なら、この確かめも飛ばす |
| H2 | テストのコードが、元のリポジトリに `refs/replace/<基準のコミット>` を作ると、最後の `read-tree <基準>` が、置換先のツリーを読む。設定とフックの照合の対象は変わらない。作られたコミットに、対象外の `src/agent/main.py` の、検査していない変更が入った | (1) すべての git の呼び出しの環境変数に `GIT_NO_REPLACE_OBJECTS=1` を足した。(2) 二重の守りとして、`create_branch_commit` は、`update-ref`（と `commit-tree`）の前に、作ったツリーを読み直し（`Git.tree_objects`）、「基準のツリー＋承認した変更」と、パス・モード・オブジェクト名まで一致することを確かめる。合わなければ、参照を作らずに `GitError`（`run_fix` は `failed`・`commit_error`）。基準の側は、実行の最初（モデルが書いたコードが走る前）に取った記録（`_Progress.base_tree`）を渡す |
| H3 | 部分クローンでは、足りないオブジェクトを読むときに、取得が走りうる（未確認） | すべての git の呼び出しの環境変数に `GIT_NO_LAZY_FETCH=1` を足した |
| H4 | テストのコードが、元のリポジトリの `.git/config` に `filter.x.smudge` を、`.git/info/attributes` に `*.py filter=x` を書くと、検査用の複製を作る `git archive` が、そのコマンドを走らせた。実行 B の間に置かれたフィルターも、次の複製（C・D）を作る `git archive` で走る（tester が見つけた、同じ穴） | (1) モデルの作業が終わった後、検査に進む前に、設定とフックを照合する。変わっていたら、`failed`・`repository_tampered`（`repository_changed` は真。照合に要る `rev-parse` のほかは、git を呼ばない。テストもリントも走らせない）。(2) **モデルが書いたコードが 1 度でも走った後は、`git archive` を呼ばない**。作業用の複製を作ったときの書き出し（tar のバイト列）を `Workspace.archive` に持っておき、検査用の複製は、そこから展開する（`build_tree(..., archive=)`・`gitio.extract_archive`）。モデルが書いたコードが走った後の git の呼び出しは、照合の `rev-parse` と、照合の直後のコミットの一連だけになった |
| H5 | `src/trigger/aws_lambda_powertools.py` を追加できた。対象外の `src/trigger/app.py` は、この名前を import する | import の乗っ取りになる名前に、基準のコミットの `requirements*.txt`（どの階層でも）の配布物の名前から作った名前を足した（小文字にし、`-` と `.` を `_` に。コメント、`-` で始まるオプションの行、版の指定、`[...]`、環境の条件からは作らない）。中身は、基準の一覧（`BaselineEntry.content`）から読む。`ChangeScope` に基準の一覧（`Mapping`）を渡すと読むので、書き込みのツールと G-2 は、同じ判定になる（場所の再検証は、既にあるファイルだけを通すので、名前の規則は関わらない）。手書きの一覧に `aws_lambda_powertools` を足した |
| H6 | pytest は、パスの `/` を `.` に変えて classname を作る。`tests/unit/foo.bar/test_x.py` と `tests/unit/foo/bar/test_x.py` の同名のテストが、同じ ID になり、落ちるほうを隠せた | (1) 変更してよいパスの各要素に、ピリオドを含めない（最後の要素の、末尾の `.py` だけ）。書き込みのツールは `denied`、G-1 は不合格。(2) JUnit の XML に、同じ ID の結果が複数あれば、「検査できなかった」（`outcomes` が空、`exit_code` が `None`）。悪いほうを残す合流（§19.3 の 28）は、やめた |
| H7 | 属性ファイルを先頭の 1MB しか読んでおらず、それより後ろの `export-subst` を見落とした | 基準のコミットの `.gitattributes`（どの階層でも）が 1,000,000 バイトを超えていたら、`has_archive_attributes` は真（→ `unsupported_attributes`）。ちょうど 1,000,000 バイトは、読み切る |
| H8 | 消す対象がシンボリックリンクに替えられていると、`remove_tree` の「権限を戻してやり直す」処理が、リンクの先をたどり、権限を 500 から 700 に変えた。作業用の複製の根をリンクや別のディレクトリに替えると、読み書きのツールと変更の取り出しが、外に触れた | `remove_tree` は、対象がディレクトリでなければ（リンクを含む）、それだけを消して終わる。権限を戻す前に、`lstat` で本物のディレクトリであることを確かめ、使える環境では `follow_symlinks=False` で変える。`Workspace` は、作ったときの根の実体（デバイスと inode）を覚え、根がそのディレクトリでなくなっていたら（`is_intact()` が偽）、読み書きの 6 ツールは、外に触れずに `denied`（結果は空、足取りの `path` は `None`、`TestRunner` は呼ばない）。`finish` は、今までどおり受ける。`Workspace.changes()` は、`WorkspaceReplaced` を上げ、`run_fix` は `failed`・`workspace_tampered`（成果物なし） |
| H9 | 既にあるファイルを巨大な疎なファイルに伸ばされると、変更の取り出しと、検査用の複製の照合が、全体を読んだ。リントには、検査の残り時間が渡っていなかった | 変更の取り出し: 開いた後の大きさが基準と違えば、変わっている。上限を超えるものは、読まずに `oversized`。大きさが同じなら、基準の大きさまでを読んで比べる。照合（`tree_matches`）: 期待の大きさ（`Manifest` に足した）と違う時点で、読まずに不一致。違いを 1 つ見つけたら、やめる。`LintRunner.count(tree, *, timeout_seconds=None)` にし、検査は、リントにも残り時間を渡す（`LINT_TIMEOUT_SECONDS` と、残り時間の短いほう）。残り時間が 0 以下なら、複製を作った後でも、テストもリントも起こさない（対応する検査は不合格） |

### 21.1 この対応で決めた点

| # | 点 | 決めた内容 | 理由 |
|---|---|---|---|
| 1 | H1 で見る場所 | `sys.prefix` と、`sysconfig.get_paths()` の `purelib`・`platlib` だけ。`sys.base_prefix`（Python 本体）、標準ライブラリのディレクトリ、利用者ごとの site-packages は、見ない | 仕様どおり。利用者ごとの site-packages と、Python 本体の場所は、ホームの下にありうる（テストや手動の確認で、開発者のホームの中を見ないため）。隔離の条件としては、§16.1 に書いた |
| 2 | H2 の、基準の記録の渡し方 | `create_branch_commit(..., expected_base=None)`。`None` のときは、その場で基準を読む（`GIT_NO_REPLACE_OBJECTS` に頼る。置換には、気づけない）。`run_fix` は、必ず記録を渡す | 既にある呼び方（テストを含む）を変えないため |
| 3 | H2 の、比べ方 | 承認した中身のオブジェクト名は、git の答え（`hash-object` の出力）に頼らず、Python で計算する。作ったツリーは、`write-tree` の後に、ツリーのオブジェクトを一時的なインデックスに読み直して比べる（インデックスではなく、書かれたツリーそのものを見る） | 「読んだつもりのもの」ではなく、コミットに入るものを確かめるため |
| 4 | H2 の、確かめ | 環境変数を一時的に外した状態で、(a) 小さなリポジトリで `create_branch_commit` を直接呼ぶ、(b) `TestH2ReplaceReferences` の `run_fix` の 2 件（偽の実行と、実物の git・pytest・ruff）を走らせる、の両方で、ツリーの確かめが止めること（`GitError`、`failed`・`commit_error`、ブランチなし）を確かめた。記録を渡さない呼び方では、止まらない（2 のとおり） | 環境変数が入ると、テストでは、確かめの手前で塞がるため |
| 5 | H4 の、メモリに持つもの | 基準の一覧（`BaselineEntry.content`）ではなく、`git archive` の出力そのもの（`Workspace.archive`） | 基準の一覧は、1MB を超えるファイルの中身と、シンボリックリンクの指す先を持っていない。書き出しそのものなら、作業用の複製と同じ展開の処理を使える。展開した結果は、今までどおり、基準の一覧と照合する |
| 6 | H4 の、照合の場所 | 終了の申告を確かめた後（`declined`・`incomplete` になる場合は、今までどおり）、静的な検査の前 | 「直せない」という結果を、変えないため。その場合も、終わりの照合（`repository_changed`）は行う |
| 7 | H6 の、G-1 への影響 | ピリオドを含む名前のファイル（`.py` 以外）は、今までは G-10 だけで落ちていたが、G-1 でも落ちる | 仕様どおり（パスの形を 1 か所で決めている） |
| 8 | H8 の、変更の取り出しの失敗 | 終了理由 `workspace_tampered` を足した（`failed`。出力先には `result.json` と `trace.json` を書く。下書きは書かない） | テストが無かったので、仕様の「安全な側に倒す」に従った。`setup_error` や `internal_error` と見分けられるようにした |
| 9 | H8 の、根の確かめと使用の間 | 根の実体を確かめてから、実際に読み書きするまでの間の入れ替えは、防いでいない | §16.4 の「パスの解決と使用の間の入れ替え」と同じ（既知の制限） |
| 10 | H9 の、既にあるファイル | 大きさが基準と違い、リンク数が 2 以上のファイルは、`irregular` になる（読み取りを、新しいファイルと同じ関数に揃えたため。今までは、変更として取り出していた） | 厳しい側。ハードリンクは、F10 で読み書きを断っている |
