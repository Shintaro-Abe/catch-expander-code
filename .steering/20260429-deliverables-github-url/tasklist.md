# タスクリスト：deliverables への GitHub URL 永続化

## 概要

`design.md` の設計に基づくタスクリスト。依存関係順に並べている。

---

## Phase 1: 書き込み側 (Orchestrator)

### 1-1. `orchestrator.py` の put_deliverable ペイロード拡張

- [x] `src/agent/orchestrator.py:649-658` の put_deliverable 引数を変数 `deliverable_record` に組み立てる形へ書き換え
  - [x] 既存の 6 フィールド (`execution_id`, `deliverable_id`, `type`, `storage`, `external_url`, `quality_metadata`) は変更しない
  - [x] 直前で `deliverable_record = {...}` として辞書を作成
- [x] `if github_url: deliverable_record["github_url"] = github_url` を追加
- [x] `self.db.put_deliverable(deliverable_record)` で呼び出し
- [x] `ruff check src/agent/orchestrator.py` が pass
- [ ] `mypy src/agent/orchestrator.py` が pass (mypy は requirements-dev.txt 未収載のため後段でまとめて実行)

### 1-2. `test_orchestrator.py` への書き込み側ユニットテスト追加

- [x] `tests/unit/agent/test_orchestrator.py` に `test_put_deliverable_with_github_url` を追加
  - [x] `code_files` ありの run シナリオを構築
  - [x] `mock_db.put_deliverable.call_args` のペイロードに `github_url` キーが存在
  - [x] 値が `github_client.push_files()` の戻り値と一致
  - [x] `storage` が `"notion+github"` であることも併せて検証
- [x] `test_put_deliverable_without_github_url` を追加
  - [x] `code_files` なしの run シナリオを構築
  - [x] `mock_db.put_deliverable.call_args` のペイロードに `github_url` キーが**含まれない** (`assert "github_url" not in payload`)
  - [x] `storage` が `"notion"` であることも併せて検証
- [x] 既存テストの `assert_called_once_with(...)` 期待値を、条件付きペイロードに合わせて更新 (該当なし: 既存テストでは put_deliverable の引数を直接アサートしていなかった)
- [x] `pytest tests/unit/agent/test_orchestrator.py -v` で新規 2 ケース pass + 既存ケース全 pass (66 件)

### 1-3. Phase 1 ローカル差分確認

- [x] `git diff src/agent/orchestrator.py tests/unit/agent/test_orchestrator.py` で変更行数が見積もり (+33/-6 程度) と整合
- [x] `git status` で意図しないファイル変更がない

---

## Phase 2: 読み取り側 (F9 履歴コマンド)

### 2-1. `_get_deliverable_url` の改名と戻り値拡張

- [x] `src/trigger/app.py:83-93` の関数名を `_get_deliverable_url` → `_get_deliverable_urls` に変更
- [x] 戻り値の型注釈を `str | None` → `dict | None` に変更
- [x] 戻り値構造を `{"notion_url": item.get("external_url"), "github_url": item.get("github_url")}` に変更
- [x] docstring を「URL 群を取得」に更新
- [x] `grep -rn "_get_deliverable_url" src/ tests/` で旧名参照が残っていない (改名漏れ防止)
- [x] `ruff check src/trigger/app.py` pass
- [ ] `mypy src/trigger/app.py` pass (mypy 未導入のためスキップ)

### 2-2. `_handle_history_command` の呼び出し更新

- [x] `src/trigger/app.py:104-131` の `url = _get_deliverable_url(...)` 呼び出しを `urls = _get_deliverable_urls(...)` に変更
- [x] 例外時のフォールバックを `urls = None` に変更
- [x] `items.append({...})` のキーを以下に置換
  - [x] `"notion_url": urls["notion_url"] if urls else None`
  - [x] `"github_url": urls["github_url"] if urls else None`
  - [x] 旧 `"url"` キーは削除
- [x] `ruff check` pass

### 2-3. `_post_history_result` の表示拡張

- [x] `src/trigger/app.py:152-156` の URL 表示分岐を以下のロジックに置換
  - [x] `item.get("notion_url")` があれば `f"   📝 {item['notion_url']}"` を append
  - [x] `item.get("github_url")` があれば `f"   💻 {item['github_url']}"` を append
  - [x] 両方とも None のときのみ `"   （URL なし）"` を append
- [x] 旧 `item['url']` 参照が残っていない (`grep` 確認)
- [x] Slack 表示が design.md 5.2 のサンプルと一致する形になっているか目視レビュー
- [x] `ruff check` pass

### 2-4. `test_app.py` への読み取り側ユニットテスト追加 (4 ケース)

- [x] `test_history_command_displays_github_url` を追加
  - [x] レコードに `external_url` と `github_url` を両方設定
  - [x] `chat_postMessage` の `text` 引数に `📝 https://www.notion.so/...` 行を含む
  - [x] `chat_postMessage` の `text` 引数に `💻 https://github.com/...` 行を含む
- [x] `test_history_command_omits_github_url_when_absent` を追加
  - [x] レコードに `external_url` のみ設定 (`github_url` キーなし)
  - [x] `text` に `📝` 行を含む
  - [x] `text` に `💻` 行を**含まない**
- [x] `test_history_command_handles_legacy_record` を追加
  - [x] フィールド `github_url` 未存在の旧レコードを返すモック
  - [x] 例外なく完了
  - [x] `text` に `📝` 行のみ
- [x] `test_history_command_no_url` を追加
  - [x] `external_url` も `github_url` もない (deliverable レコードなしのケース)
  - [x] `text` に「（URL なし）」を含む
- [x] 既存の F9 関連テストの期待値を、新しい表示形式 (`📝` 接頭辞) に合わせて更新 (TestGetDeliverableUrls / TestHandleHistoryCommand / TestPostHistoryResult)
- [x] `pytest tests/unit/trigger/test_app.py -v` で新規 4 ケース pass + 既存ケース全 pass (60 件)

### 2-5. Phase 2 ローカル差分確認

- [x] `git diff src/trigger/app.py tests/unit/trigger/test_app.py` で変更行数が見積もりと整合
- [x] `git status` で意図しないファイル変更がない

---

## Phase 3: ドキュメント整合

### 3-1. `docs/functional-design.md` 第 2 節 ER 図 (DELIVERABLE エンティティ) の更新

- [x] DELIVERABLE エンティティに `github_url` 行を追加
  - [x] 型: `string`
  - [x] 説明: GitHub ディレクトリ URL（storage=notion+github の場合のみ）
- [x] `storage` の値を `"notion/notion+github"` に明記
- [x] `external_url` の説明を「Notion ページ URL」に明確化
- [x] 表記揺れがない (`github_url` で統一)

### 3-2. `docs/functional-design.md` 第 7 節 F9 履歴コマンド出力例の更新

- [x] 第 7 節フロー概要の deliverables 解決ステップを「external_url（Notion）と github_url（任意）を解決」に修正
- [x] 追加コンポーネント表の関数名・役割を `_get_deliverable_urls` に修正
- [x] Slack 投稿サンプルを design.md 5.2 の形式に書き換え
  - [x] `📝 https://www.notion.so/...` 行
  - [x] `💻 https://github.com/...` 行 (コード成果物がある成果物のみ)
  - [x] 旧フォーマット互換ルールを併記
- [x] 第 2 節と第 7 節で `github_url` の意味付けに矛盾がないことをレビュー

---

## Phase 4: 結合確認とデプロイ

### 4-1. ローカル全テスト実行

- [x] `pytest tests/ -v` で全テスト pass (新規 6 ケース含む) → 237 件全 pass
- [x] 新規 warning が発生していないことを確認

### 4-2. Lint / Type check / Format check

- [x] `ruff check src/ tests/` でエラー 0 (本変更スコープのファイル)
- [x] `ruff format --check src/ tests/` で差分 0 (本変更スコープのファイル)
- [ ] `mypy src/agent/orchestrator.py src/trigger/app.py --disallow-untyped-defs` でエラー 0 (mypy 未導入のためスキップ)

### 4-3. コミット作成 (1 コミット)

- [x] コミットメッセージを起案 (例: `feat: persist GitHub URL on deliverables and surface in F9 history`)
- [x] ステージ対象を明示的に追加
  - [x] `src/agent/orchestrator.py`
  - [x] `src/trigger/app.py`
  - [x] `tests/unit/agent/test_orchestrator.py`
  - [x] `tests/unit/trigger/test_app.py`
  - [x] `docs/functional-design.md`
  - [x] `.steering/20260429-deliverables-github-url/`
- [x] `git diff --cached` で意図したファイルのみがステージされていることを確認
- [x] `git commit` 実行 → `git push origin main` (commit: `d0900c8`)

### 4-4. `sam deploy` で本番反映

- [x] `sam build` 成功 (Python 3.13 ランタイムを uv 経由で確保)
- [x] `sam deploy` 実行 → CloudFormation Stack `catch-expander` を `UPDATE_COMPLETE` に
- [x] ECS タスク定義の新リビジョン `catch-expander-agent:3` が ACTIVE
- [x] Lambda `TriggerFunction` の最新版がデプロイされている (F9 履歴コマンドの新表示形式が反映)
- [x] GitHub Actions `build-agent.yml` がコミット `d0900c8` で自動発火 → ECR `:latest` 更新 (2026-04-29T04:38:03 UTC)
- [x] 次回 ECS タスク起動時に orchestrator.py の `github_url` 永続化が有効になる

### 4-5. E2E 動作確認

- [ ] Slack で `@Catch-Expander` に新規トピック (コード成果物が出るもの) を投稿
- [ ] 完了通知の到着まで待機
- [ ] `aws dynamodb get-item --table-name catch-expander-deliverables --key '{...}'` で `github_url` フィールドが保存されていることを確認
- [ ] Slack で `@Catch-Expander 履歴` を投稿
- [ ] 応答に `📝 https://www.notion.so/...` 行が含まれる
- [ ] 応答に `💻 https://github.com/Shintaro-Abe/catch-expander-deliverables/...` 行が含まれる
- [ ] 既存 (本変更前生成) の成果物項目では Notion 行のみが表示され、エラーが出ない (後方互換)

---

## Phase 5: クロージング

### 5-1. ステアリングドキュメントの完了マーク

- [ ] 本ファイルの全タスクの `[ ]` を `[x]` に更新
- [ ] 末尾に完了日と本実装で判明した特記事項を追記

### 5-2. ナレッジ化判断

- [ ] 本変更で得られた学び (例: 「`storage` のような複合値フィールドは付随 URL を別フィールド化する」「Slack メッセージ寿命に依存する設計の脆さ」) を obsidian ノートとして残すかを判断
- [ ] 残す場合: `obsidian/2026-04-29_*.md` を作成 (フォーマットは既存ノートに準拠)
- [ ] 残さない場合: 判断理由を本ファイル末尾に短く記録

---

## 完了条件

- [ ] 全テスト pass (`pytest tests/ -v`)
- [ ] Lint / Format / Type check クリーン
- [ ] 新規ワークフローで `github_url` が DynamoDB に保存されている
- [ ] F9 履歴コマンドで GitHub URL が表示される
- [ ] 既存レコードで F9 履歴コマンドがエラーなく動作する (後方互換)
- [ ] `docs/functional-design.md` 第 5 節 / 第 7 節が現状を正しく記述している

---

## リスクと対応

- [ ] **リスク**: `_get_deliverable_url` の改名で外部参照が壊れる
  - [ ] **対応**: 2-1 完了時に `grep -rn "_get_deliverable_url" .` で全件確認
- [ ] **リスク**: 既存テストの期待値変更で大量に壊れる
  - [ ] **対応**: 1-1 / 2-1 を小さな単位でその場で修正
- [ ] **リスク**: Slack 表示で URL 行が長すぎて折り返し崩れ
  - [ ] **対応**: 4-5 の動作確認で目視チェック
- [ ] **リスク**: デプロイ後に旧フォーマットレコードで `KeyError`
  - [ ] **対応**: `test_history_command_handles_legacy_record` で事前防御
