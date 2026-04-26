# タスクリスト: 認証方式の再設計（ai-papers-digest 方式の採用）

作成日: 2026-04-25
対応 requirements: `.steering/20260425-auth-redesign-aipapers/requirements.md`
対応 design: `.steering/20260425-auth-redesign-aipapers/design.md`

> **ステータス: 完了 (2026-04-25 push 済み・2026-04-26 状態更新)**
>
> - 実装 push 完了: `947bc3b feat: adopt ai-papers-digest auth scheme with auto refresh` および `4ac050f fix: add User-Agent header to OAuth refresh request`
> - `src/token_monitor/handler.py` を Token Refresher Lambda として全面書き換え済み（`call_claude_with_workspace` などとは無関係）
> - `src/agent/main.py` の `_setup_claude_credentials` 改修済み・`entrypoint.sh` 経由で Secrets Manager 連携稼働中
> - 実機 refresh 成功: メモリ参照 `project_auth_redesign_status.md`（2026-04-25）
> - 個別タスクのチェックボックスは履歴保持のため未更新。**実装側のソースで完了状態を確認可能**
> - 関連ナレッジ: `feedback_oauth_user_agent.md`（platform.claude.com への OAuth は User-Agent 必須）

## 完了条件

- 全タスクが `[x]` になる
- `pytest tests/` で全件パス（既存 + 新規）
- `sam build` がエラーなく通る
- main へのマージ可能な状態（commit 済み）
- 実機デプロイと実機検証はユーザー判断（T11 で扱う、本 tasklist では push まで）

---

## T1. 実装前調査 [ ]

### T1.1 SAM template の現状確認 [ ]
- `template.yaml` の以下を Read してフィールド配置・命名規則を把握
  - `TokenMonitorFunction`（:481-510 付近）
  - `AgentTaskRole` の Policy（:296-310 付近）
  - 既存 IAM Statement のフォーマット規則

### T1.2 `src/agent/main.py` の既存テストへの影響確認 [ ]
- `tests/unit/agent/test_main.py` を Read し、以下を確認
  - `_setup_claude_credentials` を直接呼んでいるテストの有無
  - 戻り値を `None → str` に変更した時の影響範囲
- 影響あるテストはタスク T3 内で同時に修正

### T1.3 既存 `tests/unit/token_monitor/test_app.py` の構造把握 [ ]
- 既存テストクラス（`TestIsTokenStale`, `TestPostSlackNotification`, `TestLambdaHandler`）の mock 戦略を確認
- 新規テストでの命名規則・fixture スタイルを揃える

### T1.4 ai-papers-digest の `token_refresher` を再確認 [ ]
- `_refresh_token` のヘッダ・ペイロード詳細を再確認（特に Content-Type と HTTP メソッド）
- `expires_in` のデフォルト値（28800 = 8h）が妥当か確認

---

## T2. Token Refresher Lambda 実装 [ ]

### T2.1 `src/token_monitor/handler.py` を全面書き換え [ ]
- design.md `3.1.1` の関数構造で再構築
- 定数: `TOKEN_URL`, `CLIENT_ID`, `SCOPES`, `REFRESH_BUFFER_MS = 60 * 60 * 1000`
- 関数: `_get_secret`, `_put_secret`, `_parse_credentials`, `_needs_refresh`, `_call_refresh_endpoint`, `_build_updated_credentials`, `_post_slack_failure`, `lambda_handler`
- `# AS OF 2026-04-25` コメントで CLIENT_ID / TOKEN_URL に注記
- ログ出力で accessToken / refreshToken の値を**一切出さない**

### T2.2 lambda_handler のフロー実装 [ ]
- design.md `3.1.2` の擬似コード通りに実装
- 以下のレスポンス形式を厳守:
  - `{"refreshed": false, "reason": "still_valid"}`
  - `{"refreshed": false, "reason": "no_refresh_token"}`
  - `{"refreshed": false, "reason": "http_<code>"}`
  - `{"refreshed": false, "reason": "url_error"}`
  - `{"refreshed": true, "new_expires_at_ms": <ms>}`

### T2.3 `_post_slack_failure` の文面実装 [ ]
- design.md `3.1.6` の文面を採用
- `reason` パラメータを文面に含める
- Slack 投稿失敗は WARN ログのみ（再 raise しない）

### T2.4 自己チェック [ ]
- `python -c "import ast; ast.parse(open('src/token_monitor/handler.py').read())"` で構文確認
- accessToken / refreshToken / clientId を `logger.info` / `logger.error` の format に含めていないか目視確認

---

## T3. ECS Task の credentials 書き戻し実装 [ ]

### T3.1 `src/agent/main.py` に hash 関数と書き戻し関数を追加 [ ]
- `_hash_text(s: str) -> str` を追加（`hashlib.sha256` 使用）
- `_writeback_claude_credentials(secret_arn: str, initial_hash: str) -> None` を追加（design.md `3.2.1`）
- `_setup_claude_credentials` の戻り値を `None → str`（initial_hash）に変更

### T3.2 `main()` 関数の改修 [ ]
- design.md `3.2.2` の通り
- `claude_secret_arn` ローカル変数を冒頭で取得
- `initial_hash: str | None = None` を try の外で初期化
- `finally` ブロックで `_writeback_claude_credentials(claude_secret_arn, initial_hash)` を呼ぶ（`initial_hash is not None` ガード付き）

### T3.3 既存テスト (`test_main.py`) への対応 [ ]
- T1.2 で確認した影響箇所を修正
- `_setup_claude_credentials` の戻り値変更に追従

### T3.4 自己チェック [ ]
- `python -c "import ast; ast.parse(open('src/agent/main.py').read())"` で構文確認
- finally の到達順を脳内シミュレーション（正常系 / 例外系で書き戻しが両方走ること）

---

## T4. SAM template 更新 [ ]

### T4.1 `TokenMonitorFunction` の IAM Policy 拡張 [ ]
- `template.yaml` の `TokenMonitorFunction.Policies` に `secretsmanager:PutSecretValue` を追加
- Resource は `!Ref ClaudeOAuthSecretArn` のみ（最小権限）
- design.md `3.3.1` の YAML 構造に揃える

### T4.2 `AgentTaskRole` の IAM Policy 拡張 [ ]
- `template.yaml:296-310` 付近の AgentTaskRole に `secretsmanager:PutSecretValue` を追加
- Resource は `!Ref ClaudeOAuthSecretArn` のみ

### T4.3 環境変数の整理 [ ]
- `STALE_THRESHOLD_HOURS` は **削除しない**（既存値を保ったまま、Lambda 側で参照しないだけにする）
  - 理由: CloudFormation 差分を最小化する（design.md `3.3.3` 採用）
- `SLACK_NOTIFICATION_CHANNEL_ID` は維持

### T4.4 `sam build` の実行 [ ]
```bash
sam build
```
エラーなく完了することを確認

---

## T5. テスト追加 [ ]

### T5.1 `tests/unit/token_monitor/test_app.py` 書き換え [ ]
- 旧テストクラス（`TestIsTokenStale`, `TestPostSlackNotification`）を削除（旧関数が削除されるため）
- 新テストクラスを追加:

#### T5.1.1 `TestNeedsRefresh` [ ]
- 残り 2h で False
- 残り 30min で True
- 失効済み（残り -1h）で True
- 残り ちょうど 1h ジャストで True（境界）

#### T5.1.2 `TestCallRefreshEndpoint` [ ]
- `urlopen` を mock して 200 成功レスポンスのパース確認
- HTTP 401 の場合 `HTTPError` が伝播
- HTTP 500 の場合 `HTTPError` が伝播
- ネットワーク到達不能（`URLError`）が伝播
- POST ペイロードの構造確認（`grant_type`, `refresh_token`, `client_id`, `scope`）

#### T5.1.3 `TestBuildUpdatedCredentials` [ ]
- `expires_in=7200` から `expiresAt = now + 7200000` を計算
- レスポンスに `refresh_token` あり → 新値で上書き
- レスポンスに `refresh_token` なし → 旧値を維持
- `scope` の split → 配列化
- `oauthAccount` 等他のキーが温存される

#### T5.1.4 `TestLambdaHandlerFlow` [ ]
- still valid シナリオ（残り 2h）→ `refreshed: false, reason: "still_valid"`、`put_secret_value` 呼ばれない
- no refresh_token シナリオ → Slack 通知あり、`refreshed: false, reason: "no_refresh_token"`
- http_401 シナリオ → Slack 通知あり、`refreshed: false, reason: "http_401"`
- success シナリオ（stale + refresh_token あり + 200）→ `refreshed: true`、`put_secret_value` 呼ばれる、Slack 通知なし

### T5.2 `tests/unit/agent/test_main.py` に書き戻しテスト追加 [ ]

#### T5.2.1 `test_writeback_skips_when_unchanged` [ ]
- 起動時 hash と現在 hash が同じ → put_secret_value が呼ばれない

#### T5.2.2 `test_writeback_calls_put_when_changed` [ ]
- credentials ファイルを書き換えた状態 → put_secret_value 呼ばれる

#### T5.2.3 `test_writeback_handles_missing_file` [ ]
- credentials ファイル削除済み → WARN ログ + put_secret_value 呼ばれない、例外なし

#### T5.2.4 `test_writeback_swallows_put_exception` [ ]
- `put_secret_value` が ClientError を raise → caller に例外が伝播しない

### T5.3 既存テスト非回帰確認 [ ]
- `tests/unit/agent/test_main.py` の他のテストが影響を受けていないことを確認
- 特に `_setup_claude_credentials` の呼び出し箇所が新シグネチャに追従しているか

---

## T6. 初回 bootstrap 手順書作成 [ ]

### T6.1 `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を作成 [ ]
- design.md `3.4.1` の構成に従う
- 手順 1〜6 をすべて記載
- トラブルシューティングセクション込み
- セキュリティ注意（投入後ローカルファイル削除推奨、シェル履歴サニタイズ）

### T6.2 動作確認手順の検証可能性 [ ]
- 「stale を意図的に作る → invoke → refreshed: true 確認」の具体的コマンド例を記載
- AWS CLI の `aws secretsmanager get-secret-value` / `put-secret-value` / `lambda invoke` の引数を明記

---

## T7. 退役・廃止対応 [ ]

### T7.1 `.devcontainer/sync_claude_token.sh` の削除 [ ]
```bash
git rm .devcontainer/sync_claude_token.sh
```

### T7.2 `.devcontainer/watch_claude_token.sh` の削除 [ ]
```bash
git rm .devcontainer/watch_claude_token.sh
```

### T7.3 旧 procedure.md に廃止注記 [ ]
- `.steering/20260419-cloudshell-iphone-auth/procedure.md` の冒頭に design.md `3.5.2` の Markdown ブロックを追加

### T7.4 DevContainer 関連ドキュメントから sync 言及を削除 [ ]
- `grep -rn "sync_claude_token\|watch_claude_token" .` で参照箇所を全て発見
- 該当する README / docs から記述削除

---

## T8. 永続ドキュメント更新 [ ]

### T8.1 `docs/architecture.md` の認証セクション更新 [ ]
- 「Claude OAuth トークン管理」関連の記述を新方式に書き換え
- 「自動リフレッシュ Lambda」アーキテクチャを記述
- ECS 書き戻しの説明追加

### T8.2 `docs/functional-design.md` の Token Monitor 説明更新 [ ]
- 「失効監視のみ」→「監視 + 自動延命」に変更
- 通知トリガーの変更を明記（stale → refresh 失敗時のみ）

### T8.3 `CLAUDE.md` の認証関連記述確認 [ ]
- `grep -n "sync_claude_token\|claude.*credentials\|OAuth\|refresh" CLAUDE.md` で要更新箇所を探す
- 該当箇所があれば更新

---

## T9. テスト実行 [ ]

### T9.1 token_monitor テストの単体実行 [ ]
```bash
pytest tests/unit/token_monitor/ -v
```
全件パスすることを確認（新規追加分も含む）

### T9.2 agent テストの単体実行 [ ]
```bash
pytest tests/unit/agent/ -v
```
全件パスすることを確認

### T9.3 全テスト実行 [ ]
```bash
pytest tests/
```
全件パスすることを確認（既存テスト数 + 新規追加分が一致）

### T9.4 SAM ビルド再確認 [ ]
```bash
sam build
```
T4.4 後に変更があった場合に備えて再確認

---

## T10. コミット & push [ ]

### T10.1 差分確認 [ ]
```bash
git status
git diff --stat
```

### T10.2 ステージング & コミット [ ]
- 対象ファイルを明示的に `git add`（`-A` / `.` は使わない）
- 対象:
  - `src/token_monitor/handler.py`
  - `src/agent/main.py`
  - `template.yaml`
  - `tests/unit/token_monitor/test_app.py`
  - `tests/unit/agent/test_main.py`
  - `.steering/20260425-auth-redesign-aipapers/{requirements,design,tasklist,initial-setup}.md`
  - `.steering/20260419-cloudshell-iphone-auth/procedure.md`（廃止注記）
  - `docs/architecture.md`
  - `docs/functional-design.md`
  - 削除: `.devcontainer/sync_claude_token.sh` / `watch_claude_token.sh`
- コミットメッセージ例:
  ```
  feat: adopt ai-papers-digest auth scheme with auto refresh

  Replace manual CloudShell-based OAuth re-auth with Token Refresher
  Lambda that calls Anthropic OAuth endpoint directly. Add ECS
  credentials writeback for in-task refresh propagation. Retire
  sync_claude_token.sh / watch_claude_token.sh and CloudShell procedure.
  ```

### T10.3 main への push [ ]
```bash
git push origin main
```

---

## T11. 本スコープ外（ユーザー判断で実施） [ ]

### T11.1 SAM デプロイ [ ]
```bash
sam build
sam deploy
```

### T11.2 ローカル PC で初回認証 + Secrets Manager 投入 [ ]
- `.steering/20260425-auth-redesign-aipapers/initial-setup.md` の手順 1-4 を実行

### T11.3 実機検証 1: Lambda の refresh 動作確認 [ ]
- 現状の Secrets Manager の値を一旦バックアップ
- `expiresAt` を意図的に過去に書き換え
- `aws lambda invoke --function-name catch-expander-token-monitor /tmp/out.json`
- CloudWatch Logs で `"refreshed": true` を確認
- Secrets Manager の `expiresAt` が現在より未来になっていることを確認

### T11.4 実機検証 2: ECS タスクの書き戻し動作確認 [ ]
- Slack から既存トピックを投入
- ECS タスクが完了するまで待機
- CloudWatch Logs で `"Credentials writeback succeeded"` または `"Credentials unchanged"` を確認

### T11.5 旧運用との並走監視 [ ]
- 1〜2 週間程度、Token Monitor のログで自動 refresh が正常に走り続けることを観測
- Slack 通知が「refresh 失敗時のみ」発火することを確認

---

## メモ

### 設計選択の確認

- **既存 `TokenMonitorFunction` 名の維持**: SAM template の差分最小化、EventBridge スケジュール温存（design.md `1.1`）
- **`STALE_THRESHOLD_HOURS` 環境変数の温存**: 削除すると CloudFormation 差分が出るため、Lambda 側で参照しないだけにする（design.md `3.3.3` / T4.3）
- **finally での書き戻し**: 例外時もちゃんと走る。Claude CLI が refresh した直後に orchestrator が落ちるケースを救済（design.md `3.2.3`）

### 過去の失敗からの学び（feedback memory に基づく）

- 「コピー成功 = 認証成功」という誤判定を避ける（feedback_auth_procedure_design.md）
- 動作確認手順では **stale を意図的に作って refresh が動くことを実機で見せる** ことを必須化（T6.2 / T11.3）
- PoC = 「1 回通った」ではなく「失効シナリオを意図的に再現できる」まで（initial-setup.md の動作確認パートに反映）

### 注意事項

- `_setup_claude_credentials` の戻り値変更で既存テストが壊れる可能性 → T1.2 で先に影響範囲を把握、T3.3 で同時修正
- `urllib` の `Request` / `urlopen` の使い方は ai-papers-digest からそのまま流用可能（外部依存追加なし）
- Slack 通知は引き続き `slack_sdk` を使うため、`requirements.txt` の変更不要
