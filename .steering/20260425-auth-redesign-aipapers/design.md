# 設計書: 認証方式の再設計（ai-papers-digest 方式の採用）

作成日: 2026-04-25
ステータス: ドラフト（承認待ち）
対応 requirements: `.steering/20260425-auth-redesign-aipapers/requirements.md`

---

## 1. 設計方針

### 1.1 基本ルール

- **既存リソース名を維持**: `TokenMonitorFunction` / `catch-expander-token-monitor` の Lambda 名と EventBridge ルールは変更しない。CloudFormation の差分が「コード更新と IAM 追加のみ」になるようにする
- **既存 Secrets 格納形式を維持**: `claudeAiOauth.{accessToken, refreshToken, expiresAt, scopes}` 構造は変更しない。Lambda 内で読み書きする際もこの形式を保つ
- **Slack 通知は最終手段**: refresh が成功するうちは通知しない。refresh が失敗した場合のみ通知（=ノイズ削減）
- **ベストエフォート書き戻し**: ECS の書き戻しは失敗してもタスク本体の成功/失敗に影響させない
- **ロガーにトークンを出さない**: ログ出力は `accessToken[0:10] + "..."` 等の固定マスクすら使わず、**完全に出力しない**（NF1.3）

### 1.2 採用する OAuth 仕様（ai-papers-digest からの移植）

```
POST https://platform.claude.com/v1/oauth/token
Content-Type: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "<existing refresh_token>",
  "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
  "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
}
```

レスポンス:

```json
{
  "access_token": "<new access token>",
  "refresh_token": "<new refresh token (may be same)>",
  "expires_in": 28800,
  "scope": "user:profile ..."
}
```

`expires_in` は秒単位のため、Secrets Manager 保存時はミリ秒に変換: `now_ms + expires_in * 1000`。

---

## 2. 全体アーキテクチャ

### 2.1 移行前後の比較

```
=== 移行前（現状） ===

[ 失効通知 → 人手で iPhone OAuth → CloudShell sync ループ ]

  EventBridge(12h) ──→ TokenMonitorFunction
                        ├─ stale なら Slack 通知
                        └─ refresh しない（人手再認証必須）

  CloudShell ──→ sync_claude_token.sh ──→ Secrets Manager 上書き
                                            （but, Claude 2.1.118 で broken）

  ECS Task ──→ Secrets 読み取り → ~/.claude/.credentials.json 配置
              （書き戻しなし）


=== 移行後（本設計） ===

[ 自動延命ループ ]

  EventBridge(12h) ──→ TokenRefresherFunction (= 既存 TokenMonitorFunction を進化)
                        ├─ 残り > 1h なら no-op
                        ├─ 残り <= 1h or 失効済み なら refresh API call
                        │   └─ 成功 → Secrets Manager 上書き
                        │   └─ 失敗 → Slack 通知（最終手段）
                        └─ refreshToken 自体が無い場合 → Slack 通知

  ECS Task ──→ Secrets 読み取り → ~/.claude/.credentials.json 配置
              （タスク終了時、起動時値と比較 → 差分あれば Secrets 書き戻し）

  CloudShell sync スクリプト群 → 廃止
```

### 2.2 コンポーネント変更マトリクス

| コンポーネント | 変更種別 | 主な変更内容 |
|--------------|--------|--------------|
| `src/token_monitor/handler.py` | **書き換え** | refresh ロジック追加、stale 検知の判定基準変更、Slack 通知のトリガー変更 |
| `src/token_monitor/requirements.txt` | 確認のみ | `urllib` 標準ライブラリ使用なので追加依存なし。slack_sdk は維持 |
| `src/agent/main.py` | **追記** | `_setup_claude_credentials` の戻り値で「起動時 hash」返す、終了時 `_writeback_claude_credentials` 呼び出し追加 |
| `template.yaml` | **追記** | Token Monitor の IAM Policy に `secretsmanager:PutSecretValue` を追加、ECS task role の同様追加 |
| `tests/unit/token_monitor/test_app.py` | **追記** | refresh 関連の新規テストクラス（既存 `TestIsTokenStale` などはそのまま残す） |
| `tests/unit/agent/test_main.py` | **追記** | 書き戻しロジックの単体テスト |
| `.devcontainer/sync_claude_token.sh` | **削除** | 役割が新方式で不要 |
| `.devcontainer/watch_claude_token.sh` | **削除** | 同上 |
| `.steering/20260419-cloudshell-iphone-auth/procedure.md` | **冒頭追記** | 廃止注記 |
| `.steering/20260425-auth-redesign-aipapers/initial-setup.md` | **新規作成** | 初回 bootstrap 手順 |
| `docs/architecture.md` | **更新** | 認証セクションを新方式に書き換え |
| `docs/functional-design.md` | **更新** | 同上 |

---

## 3. 詳細設計

### 3.1 Token Refresher Lambda（`src/token_monitor/handler.py` 書き換え）

#### 3.1.1 モジュール構造

```
src/token_monitor/handler.py
├── 定数
│   ├── TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
│   ├── CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
│   ├── SCOPES = "user:profile user:inference user:sessions:claude_code ..."
│   └── REFRESH_BUFFER_MS = 60 * 60 * 1000  # 1h
├── _get_secret(arn) → str                              （既存維持）
├── _put_secret(arn, value) → None                      （新規）
├── _parse_credentials(json_str) → dict                 （新規。既存 _is_token_stale を分解）
├── _needs_refresh(expires_at_ms, buffer_ms) → bool     （新規）
├── _call_refresh_endpoint(refresh_token) → dict        （新規。urllib で OAuth API 叩く）
├── _build_updated_credentials(old, refresh_response, now_ms) → dict
│                                                        （新規。expiresAt 計算と JSON 構築）
├── _post_slack_failure(slack_token, channel_id, reason, expires_at_ms) → None
│                                                        （既存 _post_slack_notification を改名・文面変更）
└── lambda_handler(event, context) → dict               （既存を全面書き換え）
```

#### 3.1.2 lambda_handler のフロー

```python
def lambda_handler(event, context):
    claude_oauth_arn = os.environ["CLAUDE_OAUTH_SECRET_ARN"]

    # 1. Secrets から読み出し
    raw = _get_secret(claude_oauth_arn)
    creds = _parse_credentials(raw)
    oauth = creds.get("claudeAiOauth", {})
    expires_at_ms = oauth.get("expiresAt")
    refresh_token = oauth.get("refreshToken")

    # 2. refresh 不要なら早期 return
    now_ms = int(time.time() * 1000)
    if expires_at_ms is not None and not _needs_refresh(expires_at_ms, REFRESH_BUFFER_MS, now_ms):
        remaining_min = (expires_at_ms - now_ms) // 60_000
        logger.info("Token still valid", extra={"remaining_min": remaining_min})
        return {"refreshed": False, "reason": "still_valid"}

    # 3. refresh_token が無い → 即 Slack 通知
    if not refresh_token:
        logger.error("No refresh_token in credentials")
        _post_slack_failure(..., reason="no_refresh_token", expires_at_ms=expires_at_ms)
        return {"refreshed": False, "reason": "no_refresh_token"}

    # 4. refresh API 呼び出し
    try:
        result = _call_refresh_endpoint(refresh_token)
    except HTTPError as e:
        logger.error("Refresh HTTP error", extra={"http_code": e.code})
        _post_slack_failure(..., reason=f"http_{e.code}", expires_at_ms=expires_at_ms)
        return {"refreshed": False, "reason": f"http_{e.code}"}
    except URLError as e:
        logger.error("Refresh URL error", extra={"error_class": type(e).__name__})
        _post_slack_failure(..., reason="url_error", expires_at_ms=expires_at_ms)
        return {"refreshed": False, "reason": "url_error"}

    # 5. Secrets 書き戻し
    new_creds = _build_updated_credentials(creds, result, now_ms)
    _put_secret(claude_oauth_arn, json.dumps(new_creds))

    new_expires_at = new_creds["claudeAiOauth"]["expiresAt"]
    logger.info("Token refreshed", extra={
        "refreshed": True,
        "new_expires_in_min": (new_expires_at - now_ms) // 60_000,
    })
    return {"refreshed": True, "new_expires_at_ms": new_expires_at}
```

#### 3.1.3 `_needs_refresh` の判定基準

```python
def _needs_refresh(expires_at_ms: int, buffer_ms: int, now_ms: int) -> bool:
    return now_ms + buffer_ms >= expires_at_ms
```

- 残り 1 時間以下 or 既に失効済み → True
- それ以外 → False
- 12 時間ごとの実行を考えると、トークン寿命 8 時間でも次回までに必ず捕捉できる（最悪のケースで「残り 4 時間」のときに refresh 不要と判定 → 4 時間後に失効 → 次の 12h Lambda 実行で期限切れを捕捉して即 refresh）

#### 3.1.4 `_call_refresh_endpoint` の実装

```python
def _call_refresh_endpoint(refresh_token: str) -> dict:
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": SCOPES,
    }).encode("utf-8")
    req = Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
```

- `boto3` 以外の追加依存なし（標準ライブラリ `urllib`）
- 10 秒タイムアウト
- HTTP 4xx/5xx は `HTTPError` として上位で捕捉
- ネットワーク到達不能は `URLError`

#### 3.1.5 `_build_updated_credentials` の実装

```python
def _build_updated_credentials(old: dict, result: dict, now_ms: int) -> dict:
    old_oauth = old.get("claudeAiOauth", {})
    new_oauth = {
        **old_oauth,
        "accessToken": result["access_token"],
        "refreshToken": result.get("refresh_token", old_oauth.get("refreshToken")),
        "expiresAt": now_ms + result.get("expires_in", 28800) * 1000,
    }
    if "scope" in result:
        new_oauth["scopes"] = result["scope"].split(" ")
    return {**old, "claudeAiOauth": new_oauth}
```

- `expires_in` 不在時のデフォルトは 28800 秒 = 8 時間（Anthropic OAuth の典型値）
- レスポンスに `refresh_token` があれば更新、無ければ既存維持
- `scope` も配列化して保存
- 元 dict のその他のキーは保持（`oauthAccount` 等のメタは温存）

#### 3.1.6 Slack 通知文面（`_post_slack_failure`）

```
🚨 Claude OAuth トークンの自動延命に失敗しました。再認証が必要です。

理由: <reason>
最終 expiresAt: <ISO 形式>

対処:
1. ローカル PC で `claude` を起動して再認証
2. `~/.claude/.credentials.json` の中身を Secrets Manager に投入:
   `aws secretsmanager put-secret-value --secret-id catch-expander/claude-oauth --secret-string file://~/.claude/.credentials.json`

詳細手順: .steering/20260425-auth-redesign-aipapers/initial-setup.md
```

`reason` に入る値:
- `no_refresh_token`: refresh_token が credentials に無い
- `http_400` / `http_401` / `http_403` 等: OAuth エンドポイントが拒否（refresh_token revoked の可能性）
- `http_5xx`: Anthropic 側の一時障害
- `url_error`: ネットワーク到達不能（NAT GW 障害等）

### 3.2 ECS Task の credentials 書き戻し（`src/agent/main.py`）

#### 3.2.1 関数追加

```python
def _setup_claude_credentials(secret_value: str) -> str:
    """既存処理 + 起動時 hash を返す"""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(exist_ok=True)
    creds_path = claude_dir / ".credentials.json"
    creds_path.write_text(secret_value)
    return _hash_text(secret_value)


def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _writeback_claude_credentials(secret_arn: str, initial_hash: str) -> None:
    """タスク終了時に credentials の差分を Secrets Manager に書き戻す（ベストエフォート）"""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        if not creds_path.exists():
            logger.warning("Credentials file not found at task exit; skipping writeback")
            return
        current = creds_path.read_text()
        if _hash_text(current) == initial_hash:
            logger.info("Credentials unchanged at task exit")
            return
        boto3.client("secretsmanager").put_secret_value(
            SecretId=secret_arn,
            SecretString=current,
        )
        logger.info("Credentials writeback succeeded")
    except Exception:
        logger.exception("Credentials writeback failed (non-fatal)")
```

#### 3.2.2 main 関数の改修

```python
def main():
    _setup_logging()
    slack_token = _get_secret(os.environ["SLACK_BOT_TOKEN_SECRET_ARN"])
    claude_secret_arn = os.environ["CLAUDE_OAUTH_SECRET_ARN"]
    initial_hash: str | None = None
    try:
        notion_token = _get_secret(os.environ["NOTION_TOKEN_SECRET_ARN"])
        github_token = _get_secret(os.environ["GITHUB_TOKEN_SECRET_ARN"])
        claude_oauth = _get_secret(claude_secret_arn)
        initial_hash = _setup_claude_credentials(claude_oauth)

        # ... existing logic ...
    except Exception as exc:
        logger.exception("Task failed")
        _notify_task_failure(slack_token, exc)
        raise
    finally:
        if initial_hash is not None:
            _writeback_claude_credentials(claude_secret_arn, initial_hash)
```

#### 3.2.3 設計判断

- **ハッシュで比較する理由**: トークン値そのものをログに出さないため。ハッシュなら差分検知は可能だがログに残しても無害
- **ベストエフォート**: 書き戻し失敗時もタスクの結果コードに影響させない。書き戻しが失敗するケースは IAM 不足や Secrets Manager 一時障害で、いずれもタスク自体の成否とは独立
- **finally で実行**: 例外時もちゃんと書き戻す。`claude` CLI が refresh した直後にタスクが落ちるケースがあるため
- **既存 `_setup_claude_credentials` のシグネチャ変更**: 戻り値を `None` → `str` に変える。既存テストへの影響を要確認（`test_main.py` を読み直す）

### 3.3 SAM template の変更

#### 3.3.1 TokenMonitorFunction の IAM Policy 拡張

```yaml
TokenMonitorFunction:
  ...
  Policies:
    - Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Action:
            - secretsmanager:GetSecretValue
            - secretsmanager:PutSecretValue          # 新規追加
          Resource:
            - !Ref ClaudeOAuthSecretArn               # PutSecretValue は claude-oauth のみ
        - Effect: Allow
          Action:
            - secretsmanager:GetSecretValue
          Resource:
            - !Ref SlackBotTokenSecretArn             # Slack token は Get のみ
```

PutSecretValue の Resource を `ClaudeOAuthSecretArn` のみに限定（最小権限）。

#### 3.3.2 AgentTaskRole の IAM Policy 拡張

template.yaml `:296-300` 付近の Agent task role に `secretsmanager:PutSecretValue` を追加（書き戻し用）。Resource は `ClaudeOAuthSecretArn` のみ。

#### 3.3.3 環境変数

- `STALE_THRESHOLD_HOURS` は不要になる（refresh 判定が `REFRESH_BUFFER_MS` 内蔵）。template.yaml から削除する代わりに、後方互換のため Lambda 側で読み捨て（参照しない）
- `SLACK_NOTIFICATION_CHANNEL_ID` は引き続き使用

### 3.4 初回 bootstrap 手順書（`initial-setup.md`）

#### 3.4.1 構成

```
# 初回認証セットアップ手順

## 前提
- ローカル PC（macOS / Windows）
- Claude Max プラン加入済み
- AWS CLI 設定済み（catch-expander の Secrets Manager に書ける IAM）

## 手順
1. Claude Code CLI のインストール
   `npm install -g @anthropic-ai/claude-code`

2. 認証
   `claude` を起動 → `/login` → ブラウザ認証

3. credentials.json の確認
   `ls -la ~/.claude/.credentials.json`
   存在しない場合: `find ~ -name "*.credentials*" -o -name ".claude.json" 2>/dev/null`
   バージョンによって `~/.claude.json` 内に格納される場合は手順 3-bis

4. Secrets Manager への投入
   `aws secretsmanager put-secret-value \
      --secret-id catch-expander/claude-oauth \
      --secret-string file://$HOME/.claude/.credentials.json \
      --region ap-northeast-1`

5. 動作確認
   - Token Refresher Lambda を手動 invoke
     `aws lambda invoke --function-name catch-expander-token-monitor /tmp/out.json`
   - CloudWatch Logs で `"refreshed": false, "reason": "still_valid"` を確認
   - 古いトークンを意図的にセット → 再 invoke → `"refreshed": true` を確認

6. クリーンアップ
   - ローカル `~/.claude/.credentials.json` の削除（任意、セキュリティ強化）
   - シェル履歴のサニタイズ（必要に応じて）

## トラブルシューティング
- credentials.json が見つからない → 手順 3-bis（Claude Code 2.x 環境別の保存先確認）
- Lambda invoke が失敗 → IAM / Secrets Manager 権限確認
```

#### 3.4.2 設計判断

- **CloudShell は使わない**: requirements C2 の通りローカル PC 前提
- **動作確認に「stale を意図的に作る」を含める**: 実機検証で「refresh が本当に動く」ことをユーザー自身が確認できる手順を必須化（過去の PoC 失敗の教訓を反映）

### 3.5 退役・廃止

#### 3.5.1 削除ファイル

- `.devcontainer/sync_claude_token.sh`
- `.devcontainer/watch_claude_token.sh`

ファイル削除のみ。これらを参照していたドキュメントは F4.3 / F4.4 で更新。

#### 3.5.2 旧 procedure.md への廃止注記

`.steering/20260419-cloudshell-iphone-auth/procedure.md` の冒頭に以下を追加:

```markdown
> **⚠️ この手順は 2026-04-25 に廃止されました。**
>
> Claude OAuth の認証は ai-papers-digest 方式（Token Refresher Lambda による自動延命）に置き換わりました。
> 現行の手順は `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を参照してください。
>
> 本ファイルは履歴として保持されます。
```

---

## 4. データ構造

### 4.1 Secrets Manager `catch-expander/claude-oauth` の格納形式

**変更なし**。既存の構造を維持。

```json
{
  "claudeAiOauth": {
    "accessToken": "<JWT>",
    "refreshToken": "<opaque>",
    "expiresAt": 1729999999999,
    "scopes": ["user:profile", "user:inference", "..."]
  },
  "oauthAccount": {  /* メタデータ。touch しない */ }
}
```

Lambda は `claudeAiOauth.expiresAt` / `claudeAiOauth.refreshToken` のみ参照・書き換え。それ以外のキー（`oauthAccount` 等）は保全。

### 4.2 Lambda レスポンス JSON

```json
{ "refreshed": false, "reason": "still_valid" }
{ "refreshed": false, "reason": "no_refresh_token" }
{ "refreshed": false, "reason": "http_401" }
{ "refreshed": false, "reason": "url_error" }
{ "refreshed": true, "new_expires_at_ms": 1729999999999 }
```

可観測性向上のため、Lambda の戻り値で常に `refreshed` フラグと `reason` / `new_expires_at_ms` を返す。CloudWatch Insights でクエリしやすい形式。

---

## 5. テスト戦略

### 5.1 Token Refresher Lambda 単体テスト

`tests/unit/token_monitor/test_app.py` に新規テストクラス追加:

| テストクラス | 検証内容 |
|------------|---------|
| `TestNeedsRefresh` | 残り > 1h で False、残り <= 1h で True、失効済みで True、`expires_at_ms=None` の境界 |
| `TestCallRefreshEndpoint` | `urlopen` を mock して 200/401/500/URLError 各ケースを検証 |
| `TestBuildUpdatedCredentials` | `refresh_token` 無いレスポンス時に旧値維持、`scope` の split、`expires_in` から `expiresAt` 計算 |
| `TestLambdaHandler` | 統合フロー: still valid / no refresh_token / http_401 / success の 4 シナリオ |

既存 `TestIsTokenStale` / `TestPostSlackNotification` クラスは削除または改名（旧関数を削除するため）。

#### 5.1.1 mock 戦略

- `secretsmanager` クライアントは `unittest.mock.patch` で `secrets_client` をモック
- `urlopen` は `unittest.mock.patch("urllib.request.urlopen")` で偽レスポンス返却
- `WebClient` は既存テストと同様に mock

### 5.2 ECS 書き戻しの単体テスト

`tests/unit/agent/test_main.py` に追加:

| テスト | 検証内容 |
|------|---------|
| `test_writeback_skips_when_unchanged` | 起動時 hash と現在 hash が同じなら put_secret_value を呼ばない |
| `test_writeback_calls_put_when_changed` | hash が異なれば put_secret_value 呼ぶ |
| `test_writeback_handles_missing_file` | credentials ファイル消失時に WARN ログだけ出して例外を出さない |
| `test_writeback_swallows_put_exception` | put_secret_value が失敗しても例外を上に伝播しない |

### 5.3 統合テスト（実機）

requirements 受け入れ条件の最後 2 項目をカバー:

1. Secrets Manager 上のトークンを意図的に stale にする（`expiresAt` を過去にする）→ Lambda 手動 invoke → 延命成功 → CloudWatch Logs に `refreshed: true` が出る → Secrets Manager の expiresAt が更新される
2. ECS タスクを起動 → orchestrator が正常完了 → CloudWatch Logs に `Credentials writeback succeeded` または `Credentials unchanged` が出る

---

## 6. 影響範囲分析

### 6.1 既存機能への影響

| 機能 | 影響 | 対応 |
|------|-----|------|
| Slack 投入 → ECS 起動 → orchestrator 実行 | **間接的に改善**（refresh 自動化でトークン切れ起因の失敗が減る） | 機能変更なし、テスト不要 |
| Token Monitor の Slack 通知 | **トリガー変更**（stale → refresh 失敗時のみ） | 通知文面更新、頻度低下を運用文書化 |
| ECS task の startup シーケンス | **`_setup_claude_credentials` の戻り値変更**（None → str） | 既存テスト (`test_main.py`) を読み直して影響範囲確認、必要なら修正 |
| DevContainer 起動時の sync ループ | **完全削除**（sync_claude_token.sh / watch_claude_token.sh） | DevContainer ドキュメントから関連記述を削除 |

### 6.2 デプロイ手順への影響

- SAM の差分は「Lambda コード更新 + IAM Policy に PutSecretValue 追加」のみ
- EventBridge ルール / Lambda 関数名は維持されるため、既存スケジュールが温存される
- ECS task definition も IAM の追加のみで再デプロイされる（コンテナイメージは別途 GitHub Actions で更新）

### 6.3 ドキュメント更新範囲

- `docs/architecture.md`: 「認証」セクションを ai-papers-digest 方式の自動延命説明に書き換え
- `docs/functional-design.md`: Token Monitor の機能説明を「監視のみ」→「監視＋自動延命」に更新
- `CLAUDE.md`: 認証関連の言及があれば更新（要確認）

---

## 7. リスク・トレードオフ

### 7.1 公開 OAuth client_id の依存

- リスク: Anthropic 側仕様変更で client_id / scope / TOKEN_URL が変わると壊れる
- 緩和: ai-papers-digest が運用実績あり、ハードコード値を `# AS OF 2026-04-25` コメントで明示、将来仕様変更時のための単一の更新箇所にする

### 7.2 refresh_token のローテーション挙動が不明

- リスク: Anthropic がレスポンスで新 refresh_token を返した場合、それを保存しないと次回 refresh が失敗する
- 緩和: `_build_updated_credentials` で `result.get("refresh_token", old)` の優先順位で必ず最新を保存

### 7.3 並行 ECS タスクによる書き戻し競合

- リスク: 2 つのタスクが同時に動いた場合、後勝ちで refresh_token が古いものに上書きされる可能性
- 緩和: ECS タスクは Slack 投入駆動で並行頻度が低い。実害は次回 Lambda 実行時の refresh で復旧可能。本タスクでは緩和策を実装せず、実機運用で問題が出れば別 steering で対応

### 7.4 ローカル PC で `~/.claude/.credentials.json` が生成されない場合

- リスク: Claude Code 2.1.118 の挙動次第で、ローカル macOS でもファイルが作られない可能性（CloudShell と同じ）
- 緩和: F3.4 / `initial-setup.md` でファイル探索手順を明記し、見つからない場合は `~/.claude.json` 等の代替パスから OAuth フィールドを抽出する手順を補助的に記載

---

## 8. 完了の定義

requirements.md `8. 受け入れ条件（マスタ）` を満たすこと。具体的には:

- [ ] `src/token_monitor/handler.py` が refresh 機能込みに書き換えられ、pytest 全件パス
- [ ] `src/agent/main.py` に書き戻しロジックが追加され、pytest 全件パス
- [ ] `template.yaml` に PutSecretValue が追加され、`sam build` が通る
- [ ] `.steering/20260425-auth-redesign-aipapers/initial-setup.md` が作成済み
- [ ] `.devcontainer/sync_claude_token.sh` / `watch_claude_token.sh` が削除済み
- [ ] `.steering/20260419-cloudshell-iphone-auth/procedure.md` に廃止注記
- [ ] `docs/architecture.md` / `docs/functional-design.md` 更新
- [ ] 実機検証 2 件成功
- [ ] main へマージ済み
