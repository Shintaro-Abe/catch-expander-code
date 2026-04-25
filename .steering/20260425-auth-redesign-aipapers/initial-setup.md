# 初回認証セットアップ手順

作成日: 2026-04-25
対応: ai-papers-digest 方式の認証への移行（`design.md` 参照）

---

## このドキュメントの位置づけ

Token Refresher Lambda が自動延命を担うため、**初回 1 回だけ** Claude OAuth クレデンシャルを Secrets Manager に投入する必要がある。本手順書はその 1 回の作業を示す。

以降、refresh が連続失敗（refresh_token revoke、Anthropic 仕様変更など）した時のみ、本手順を再実行する。

---

## 1. 前提

- **ローカル PC（macOS / Windows）** が手元にある
  - CloudShell では Claude Code 2.1.118 で `~/.claude/.credentials.json` が生成されないことを確認済み（2026-04-23）
  - ローカル macOS / Windows ではファイル生成される（ai-papers-digest 運用実績）
- Claude Max プラン（Max 5x または Max 20x）に加入済み
- AWS CLI 設定済み、対象 IAM が `catch-expander/claude-oauth` シークレットへ書き込める権限を持つ
- Node.js 18 以上がローカル PC にインストール済み

---

## 2. 手順

### 2.1 Claude Code CLI のインストール

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

バージョン 2.x 以上が表示されることを確認。

### 2.2 Claude Code で OAuth 認証

```bash
claude
```

対話セッションが開いたら、スラッシュ付きで以下を入力:

```
/login
```

ブラウザが起動して Anthropic アカウントでのログインを促される。許可すると `Login successful` が表示される。`/exit` でセッションを抜ける。

### 2.3 credentials.json の存在確認

```bash
ls -la ~/.claude/.credentials.json
jq -r '.claudeAiOauth | keys[]' ~/.claude/.credentials.json
```

期待される出力:

```
accessToken
expiresAt
refreshToken
scopes
```

#### 2.3.1 ファイルが見つからない場合

Claude Code のバージョンや OS 環境により保存先が変わっている可能性がある。以下で探索:

```bash
find ~ -name ".credentials.json" 2>/dev/null
find ~ -name ".claude.json" 2>/dev/null
grep -rlE 'accessToken|claudeAiOauth' ~/.claude* 2>/dev/null
```

`~/.claude.json`（ホーム直下）に保存されていて `claudeAiOauth` キーを含む場合は、そのファイルを `.credentials.json` 相当として扱う:

```bash
jq '.claudeAiOauth' ~/.claude.json
```

`accessToken` / `refreshToken` / `expiresAt` を含む `claudeAiOauth` オブジェクトが取得できれば、それを Secrets Manager 投入用に整形する:

```bash
jq '{claudeAiOauth: .claudeAiOauth}' ~/.claude.json > /tmp/claude-creds.json
```

### 2.4 Secrets Manager への投入

ファイルパスを `--secret-string file://...` で指定する（`echo` や `cat |` 経由はシェル履歴に残るため避ける）:

```bash
aws secretsmanager put-secret-value \
  --secret-id catch-expander/claude-oauth \
  --secret-string file://$HOME/.claude/.credentials.json \
  --region ap-northeast-1
```

`/tmp/claude-creds.json` を生成した場合はそちらを参照:

```bash
aws secretsmanager put-secret-value \
  --secret-id catch-expander/claude-oauth \
  --secret-string file:///tmp/claude-creds.json \
  --region ap-northeast-1
```

成功すると `VersionId` が表示される。

### 2.5 動作確認 1: Lambda の正常系

```bash
aws lambda invoke \
  --function-name catch-expander-token-monitor \
  --region ap-northeast-1 \
  /tmp/lambda-out.json
cat /tmp/lambda-out.json
```

期待される出力（投入直後のトークンは有効期限が十分あるため refresh は走らない）:

```json
{"refreshed": false, "reason": "still_valid"}
```

CloudWatch Logs（`/aws/lambda/catch-expander-token-monitor`）に `Token still valid` のログが出ていれば OK。

### 2.6 動作確認 2: refresh が実際に走ることを検証する

**ここを必ず実施する。** 「投入したら通った」だけでは「リフレッシュ経路が機能する」ことを確認できないため、過去にこの検証を省略して運用事故を起こした（2026-04-23 の認証失敗）。

**注意**: 以下は **検証用に意図的に stale 状態を作る** 操作であり、本物のトークンを破壊するわけではない。検証後、自動 refresh が成功して新トークンに置き換わる。

#### 2.6.1 現在の expiresAt をバックアップ

```bash
aws secretsmanager get-secret-value \
  --secret-id catch-expander/claude-oauth \
  --region ap-northeast-1 \
  --query SecretString --output text > /tmp/claude-creds-backup.json
jq -r '.claudeAiOauth.expiresAt' /tmp/claude-creds-backup.json
```

#### 2.6.2 expiresAt を意図的に過去にする

```bash
jq '.claudeAiOauth.expiresAt = 1' /tmp/claude-creds-backup.json > /tmp/claude-creds-stale.json
aws secretsmanager put-secret-value \
  --secret-id catch-expander/claude-oauth \
  --secret-string file:///tmp/claude-creds-stale.json \
  --region ap-northeast-1
```

#### 2.6.3 Lambda を手動 invoke

```bash
aws lambda invoke \
  --function-name catch-expander-token-monitor \
  --region ap-northeast-1 \
  /tmp/lambda-refresh.json
cat /tmp/lambda-refresh.json
```

期待される出力:

```json
{"refreshed": true, "new_expires_at_ms": <未来の値>}
```

#### 2.6.4 expiresAt が更新されたことを確認

```bash
aws secretsmanager get-secret-value \
  --secret-id catch-expander/claude-oauth \
  --region ap-northeast-1 \
  --query SecretString --output text \
  | jq -r '.claudeAiOauth.expiresAt'
```

`1` ではなく現在より未来のミリ秒値（例: `1730000000000`）が返れば検証成功。これで **refresh 経路が本当に動く** ことが確認できた。

#### 2.6.5 もし `refreshed: false` が返ったら

- `reason: "no_refresh_token"`: 2.4 で投入した JSON に `refreshToken` が含まれていない。2.3 でファイル内容を再確認
- `reason: "http_401"` or `http_400`: refresh_token が無効（revoke 済み or 投入ミス）。2.2 から再認証
- `reason: "url_error"`: Lambda から Anthropic への egress 不通。VPC 設定確認
- `reason: "no_expires_at"`: JSON 構造が壊れている。2.4 を再実行

### 2.7 動作確認 3: ECS 起動と書き戻し

Slack から既存トピックを投入し ECS タスクを動かす:

1. Slack にメッセージ送信（例: 既存の検証用トピック）
2. CloudWatch Logs `/ecs/catch-expander-agent` を tail
3. タスク終了直前のログに以下のいずれかが出る:
   - `Credentials unchanged at task exit` → タスク中に refresh されなかった（=正常）
   - `Credentials writeback succeeded` → タスク中に refresh が走り Secrets Manager に書き戻し成功

### 2.8 クリーンアップ（任意・推奨）

ローカル PC からトークンを削除してリスクを減らす:

```bash
shred -u ~/.claude/.credentials.json 2>/dev/null || rm -f ~/.claude/.credentials.json
shred -u /tmp/claude-creds-backup.json /tmp/claude-creds-stale.json /tmp/lambda-out.json /tmp/lambda-refresh.json 2>/dev/null
```

シェル履歴のサニタイズ（必要に応じて）:

```bash
history -d $(history | grep -nE 'put-secret-value' | tail -5 | cut -d: -f1)
```

---

## 3. 失敗時の Slack 通知について

Token Refresher Lambda は、**自動延命に失敗した場合のみ** Slack へ通知する。具体的には:

- `no_refresh_token`: credentials に refresh_token が入っていない（投入時のミス、または Anthropic 仕様変更）
- `http_4xx`: refresh_token が拒否された（revoke 済み、または scope/client_id 不一致）
- `http_5xx`: Anthropic 側の一時障害
- `url_error`: ネットワーク到達不能（NAT GW 障害など）

Slack 通知を受信したら、本手順書 2.2 から再実行する。

---

## 4. トラブルシューティング

### Q1. `claude /login` でブラウザが開かない

CLI が表示する URL を手動でブラウザに貼り付ける。CloudShell や Web IDE では `/login` 後に表示されるコードを別端末でブラウザに入力する device-flow 形式になる場合がある。

### Q2. `~/.claude/.credentials.json` が作られない

2.3.1 のファイル探索手順に従う。Claude Code のバージョン依存で `~/.claude.json` 内に統合されている場合は、そこから `claudeAiOauth` フィールドを抽出する。

### Q3. `aws secretsmanager put-secret-value` で AccessDenied

IAM が `secretsmanager:PutSecretValue` 権限を持つか確認。SAM デプロイ後に Token Refresher Lambda の IAM Role と ECS Task Role には自動付与されているが、人間ユーザーの権限は別途必要。

### Q4. Lambda invoke が `expired token` 系のエラーを返し続ける

- AWS CLI の認証情報が期限切れ → `aws configure` or SSO 再認証
- Lambda 自体の権限不足 → CloudWatch Logs で具体エラー確認

### Q5. 検証 2.6 で `refreshed: false` が継続して出る

`reason` 別の対処は 2.6.5 を参照。それでも解消しない場合は CloudWatch Logs `/aws/lambda/catch-expander-token-monitor` で構造化ログを確認し、`http_code` や `error_class` を特定する。
