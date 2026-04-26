# 設計: コード成果物生成の失敗修正

> **⚠️ 注記 (2026-04-26 追加): 採用した方針は後に撤回**
>
> 本設計の Phase B-2（`_normalize_code_files_payload` でスキーマ正規化）は、`.steering/20260425-code-gen-redesign-filesystem/` により全面的に置き換えられました。
> 新方式は JSON パース自体を放棄し、Claude に Write ツールでファイルを直接書き出させる `call_claude_with_workspace` 方式です。
> 本ファイルは履歴として保持されます。

## 設計方針

requirements.md AC-1 / AC-2 を 2 段階で実施する:

- **Phase A（観測強化）**: 既存の警告ログをメッセージ本文に診断情報を埋め込む形に書き換える
  → デプロイ後 1 回の Slack 投入で root cause を確定する
- **Phase B（本対応）**: Phase A で得た root cause に応じて、3 ブランチのうち適合するものを実装する
  → ブランチごとの実装ポイントを本ドキュメントで先に確定しておき、観測後に design 書き直しを発生させない

Phase A 単独でも `pytest` 全件 pass / 既存挙動非破壊が成立すること。

## 影響範囲

| ファイル | Phase | 変更内容 |
|----------|-------|----------|
| `src/agent/orchestrator.py` | A | `Code generation failed for type` warning をメッセージ埋め込み形式に書換 |
| `src/agent/orchestrator.py` | B（条件付き） | `_parse_claude_response` 直後の正規化レイヤ or `_build_code_generation_prompt` 修正 |
| `src/agent/prompts/generator.md` | B（条件付き） | コード生成タイプ別の出力形式を明示（generator.md は影響しない可能性が高いが念のため確認） |
| `tests/unit/agent/test_orchestrator.py` | A | 警告メッセージに診断情報が含まれることのテスト追加 |
| `tests/unit/agent/test_orchestrator.py` | B | root cause 別のケーステスト追加 |
| `docs/development-guidelines.md` | A | コード生成失敗ログの読み方を 1 段落追記 |

`src/agent/main.py` のロガー設定（`basicConfig` の formatter）は **触らない**。
ロガー全体を JSON 化すると他の正常系ログにも影響が及び、blast radius が広がるため、
今回は対象の警告 1 か所に限ってメッセージ本文へ埋め込む方式を採用する。

## Phase A: 観測強化

### A-1. 警告メッセージの構造化

`src/agent/orchestrator.py:383-393` の warning 呼び出しを、診断情報をメッセージ本文に埋め込む形に変更する。

**変更前:**

```python
logger.warning(
    "Code generation failed for type",
    extra={
        "execution_id": execution_id,
        "code_type": code_type,
        "parse_error": code_result.get("parse_error", False)
        if isinstance(code_result, dict)
        else True,
    },
)
```

**変更後（イメージ）:**

```python
diag = _build_code_failure_diagnostics(code_raw, code_result)
logger.warning(
    "Code generation failed for type | execution_id=%s code_type=%s "
    "parse_error=%s files_kind=%s files_count=%s top_level_keys=%s "
    "response_chars=%d response_preview=%r",
    execution_id, code_type,
    diag["parse_error"], diag["files_kind"], diag["files_count"],
    diag["top_level_keys"], diag["response_chars"], diag["response_preview"],
)
```

`%r` を使うことで改行・制御文字をエスケープし、CloudWatch 上で 1 行に収める。

### A-2. 診断ヘルパーの追加

`src/agent/orchestrator.py` のモジュールレベルに以下のユーティリティを追加する:

```python
def _build_code_failure_diagnostics(raw: str, parsed: object) -> dict:
    """コード生成失敗時の診断情報を組み立てる。

    Returns:
        parse_error / files_kind / files_count / top_level_keys /
        response_chars / response_preview を含む dict
    """
```

実装メモ:

- `parse_error`: `isinstance(parsed, dict) and parsed.get("parse_error", False)` で判定
- `files_kind`: `parsed` が dict なら `type(parsed.get("files")).__name__` ／ それ以外は `"<not-dict>"`
  ／ `files` キー欠損なら `"missing"`
- `files_count`: `files` が dict なら `len(files)` ／ list なら `len(files)` ／ それ以外は `0`
- `top_level_keys`: `list(parsed.keys())[:10]` ／ dict でなければ空 list
- `response_chars`: `len(raw)` （raw は `call_claude` の生 stdout）
- `response_preview`: `raw[:500]`（500 文字制限）

### A-3. ドキュメント追記

`docs/development-guidelines.md` の「ログ運用」セクションに以下を追記:

- コード生成失敗ログの形式と各フィールドの意味
- root cause 種別別の判定方法（例: `parse_error=True` なら CLI 応答が JSON 化できない / `files_kind=missing` ならスキーマ違い）

該当セクションが無ければ「ログ運用」セクションを新設する。

### A-4. テスト

`tests/unit/agent/test_orchestrator.py` に以下を追加:

- `test_build_code_failure_diagnostics_parse_error`: `parse_error=True` のとき該当フィールドが `True`
- `test_build_code_failure_diagnostics_missing_files`: dict だが `files` キー無しのとき `files_kind="missing"`
- `test_build_code_failure_diagnostics_empty_files`: `files={}` のとき `files_count=0`
- `test_build_code_failure_diagnostics_files_as_list`: `files=[...]` のとき `files_kind="list"`
- `test_build_code_failure_diagnostics_response_preview_truncated`: 500 文字超で先頭 500 文字に切り詰められる

警告メッセージ自体の caplog テストは過剰になりがちなので、診断ヘルパーのユニットテストで AC-1 の品質を担保する。

## Phase A 完了条件

- 警告メッセージに 7 項目（execution_id, code_type, parse_error, files_kind, files_count, top_level_keys, response_chars, response_preview）が確実に含まれる
- `pytest tests/` 全件 pass（173 → +5 = 178 想定）
- 個別 commit でデプロイ → 同一トピック「API GatewayとLambdaの組み合わせについて」を Slack 投入
- CloudWatch から root cause を確定（次のいずれか）:
  - **R1（パーサ失敗）**: `parse_error=True` → 戦略 1〜4 が失敗
  - **R2（スキーマ違い）**: `parse_error=False` かつ `files_kind=missing`
  - **R3（型違い）**: `files_kind` が `list` / `str` / その他
  - **R4（空 dict）**: `files_kind=dict` かつ `files_count=0`
  - **R5（応答サイズ超過の部分パース）**: `response_chars` が極端に大 + `top_level_keys` がコード生成と無関係なキー

## Phase B: root cause 別本対応

| root cause | 対応ブランチ | 主な変更 |
|------------|--------------|----------|
| R1 | B-1: 戦略追加 | `_parse_claude_response` の戦略 1〜4 では拾えない応答パターンを観測例から追加。フォールバックで `files` 抽出専用の正規表現抽出を試行 |
| R2 / R3 | B-2: スキーマ正規化 | `_parse_claude_response` 直後に `_normalize_code_files_payload(parsed)` を挟み、`code_files.files` / `output.files` / トップレベル `*.tf` キー直接配置 などの揺れを `{"files": dict, "readme_content": str}` 形式に揃える |
| R4 | B-3: プロンプト改訂 | `_build_code_generation_prompt` の出力例にダミーファイル（例: `main.tf`）を 1 つ含めて空応答を抑制。空応答の場合は 1 回だけ retry も検討 |
| R5 | B-4: プロンプト分割粒度の引き下げ | `code_types` の各タイプを更に「main / variables / outputs / README」等に分割。または応答サイズが本当に Anthropic API 上限超過の場合は generator.md 同様にファイル数上限を 3 へ縮小 |

各ブランチは独立した commit で実装。複数 root cause が同時に観測された場合は該当ブランチを順次適用。

### Phase B の共通テスト方針

- 該当ブランチごとに「修正後のサンプル応答 → 正常に `code_files_merged` に取り込まれる」テストを追加
- 既存の M3 系テスト（`test_code_generation_per_type_merges_files` 等）が pass し続けることを確認

## Phase B 完了条件 = 全体完了条件

- AC-2 該当の本対応 commit が 1〜複数 push 済み
- AC-3 実機検証で 2 回連続して `storage = "notion+github"` 達成
  （間欠失敗を排除するため 2 回再現確認）
- GitHub catch-expander-code リポジトリに新 execution 向けディレクトリが作成され、
  IaC / プログラムコードのファイルが push されている
- CloudWatch に `Code generation failed for type` warning が出ない
  （または部分失敗のみで、もう一方が成功）
- ユニットテスト全件 pass

## 非機能要件

- **後方互換**: M1〜M3 / S1〜S2 の挙動を変えない（173 tests baseline を維持）
- **観測コスト**: 警告 1 行あたり最大 ~600 文字程度の増加。CloudWatch コストへの影響は無視できる範囲
- **セキュリティ**: `response_preview` には Claude 応答の冒頭 500 文字が出力されるが、
  生成対象が公開可能なコードであり、API キー等は generator 制約で禁止しているため秘匿情報混入リスクは低い
- **デプロイ**: GitHub Actions の build-agent.yml が `src/agent/**` 変更で発火するため、
  Phase A push と Phase B push のそれぞれで自動デプロイされる

## リスクと対処

| リスク | 対処 |
|--------|------|
| Phase A 観測 → 結果が R1〜R5 のどれにも当てはまらない | tasklist の最終項目に「該当しない場合は本 design に root cause 種別を追記して再ドラフト」を含める |
| Phase B 適用後も間欠的に失敗する | AC-3 で「2 回連続成功」を必須化することで間欠検出。失敗が再現する場合は別ブランチを追加適用 |
| `response_preview` でユーザー入力やトピックが秘匿情報を含む可能性 | 現状の Slack 投入トピックは公開可能な技術トピックのみだが、tasklist にプライバシー想定を 1 項目記録（将来センシティブ用途に拡張する場合は preview を絞る） |
| `_normalize_code_files_payload` 追加で他の generate ステップに影響 | 適用範囲をコード生成ループ内に限定（テキスト成果物パスは既存の `_parse_claude_response` のまま）|

## スコープ外（再掲）

- ロガー全体の JSON 化（影響範囲が大きすぎるため別 steering）
- 新 code_type 追加
- review loop の diagnostics（Followup-B）
- N1 (fix_prompt 差分化)
