# クレデンシャル取得手順書

本書は `architecture.md` 7. セキュリティ設計で定義されたクレデンシャルの取得手順を記載する。

## 対象クレデンシャル一覧

| # | クレデンシャル | 保管先 |
|---|--------------|--------|
| 1 | Slack Bot Token（`xoxb-`） | Secrets Manager |
| 2 | Slack Signing Secret | Secrets Manager |
| 3 | Notion Integration Token | Secrets Manager |
| 4 | GitHub Fine-grained PAT | Secrets Manager |
| 5 | Claude Code MaxプランOAuth | コンテナ内Claude Code設定 |

---

## 1. Slack Bot Token + Signing Secret

### 1.1 Slack Appの作成

1. https://api.slack.com/apps にアクセス
2. 「Create New App」→「From scratch」を選択
3. App Name: `Catch Expander`、Workspace: 対象ワークスペースを選択
4. 「Create App」をクリック

### 1.2 Bot Token Scopesの設定

「OAuth & Permissions」ページで以下のBot Token Scopesを追加する。

| スコープ | 用途 |
|---------|------|
| `app_mentions:read` | チャンネルでのメンション検知 |
| `chat:write` | 進捗通知・完了通知の投稿 |
| `im:history` | DM履歴の読み取り（プロファイル登録の対話用） |
| `im:read` | DMチャンネルの情報取得 |
| `im:write` | DMでのメッセージ送信 |

### 1.3 Event Subscriptionsの設定

「Event Subscriptions」ページで以下を設定する。

1. Enable Events: ON
2. Request URL: API GatewayのエンドポイントURL（デプロイ後に設定）
   ```
   https://{api-id}.execute-api.ap-northeast-1.amazonaws.com/Prod/slack/events
   ```
3. Subscribe to bot events:
   - `app_mention` — チャンネルでのメンション
   - `message.im` — Bot宛てDM

### 1.4 ワークスペースへのインストール

1. 「Install App」ページで「Install to Workspace」をクリック
2. 権限を確認し「Allow」をクリック

### 1.5 クレデンシャルの取得

| クレデンシャル | 取得場所 |
|--------------|---------|
| Bot Token（`xoxb-`） | 「OAuth & Permissions」→ Bot User OAuth Token |
| Signing Secret | 「Basic Information」→ App Credentials → Signing Secret |

### 1.6 Secrets Managerへの登録

```bash
# Slack Bot Token
aws secretsmanager create-secret \
  --name "catch-expander/slack-bot-token" \
  --secret-string "xoxb-xxxxxxxxxxxx"

# Slack Signing Secret
aws secretsmanager create-secret \
  --name "catch-expander/slack-signing-secret" \
  --secret-string "xxxxxxxxxxxxxxxx"
```

---

## 2. Notion Integration Token

### 2.1 Internal Integrationの作成

1. https://www.notion.so/profile/integrations にアクセス
2. 「New integration」をクリック
3. 以下を設定:
   - Name: `Catch Expander`
   - Associated workspace: 対象ワークスペース
   - Type: Internal

### 2.2 Capabilitiesの設定

| カテゴリ | 権限 | 設定 |
|---------|------|:---:|
| Content Capabilities | Read content | o |
| Content Capabilities | Update content | o |
| Content Capabilities | Insert content | o |
| Comment Capabilities | Read comments | x |
| Comment Capabilities | Insert comments | x |
| User Capabilities | Read user information without email | x |

ページ削除権限は付与しない（functional-design.md 4.2 の安全設計に準拠）。

### 2.3 トークンの取得

Integration作成後に表示される Internal Integration Secret をコピーする。
`ntn_` で始まる文字列。

### 2.4 成果物DBへの共有設定

1. Notionで成果物Database（`Catch Expander 成果物`）を作成
2. Databaseページ右上「...」→「Connections」→「Catch Expander」を追加
3. この操作により、IntegrationはこのDatabase配下のページのみにアクセス可能になる

他のページには一切アクセスできない。

### 2.5 Secrets Managerへの登録

```bash
aws secretsmanager create-secret \
  --name "catch-expander/notion-token" \
  --secret-string "ntn_xxxxxxxxxxxx"
```

---

## 3. GitHub Fine-grained PAT

### 3.1 コード成果物リポジトリの作成

1. GitHubで `catch-expander-code` リポジトリを作成（Private）
2. READMEを追加して初期化

### 3.2 Fine-grained PATの作成

1. https://github.com/settings/tokens?type=beta にアクセス
2. 「Generate new token」をクリック
3. 以下を設定:

| 項目 | 設定 |
|------|------|
| Token name | `catch-expander` |
| Expiration | 90 days（期限切れ前に再発行） |
| Repository access | Only select repositories → `catch-expander-code` |

4. Repository permissionsで以下のみ付与:

| 権限 | レベル | 用途 |
|------|--------|------|
| Contents | Read and write | コード成果物のpush |

他の権限（Issues, Pull requests, Actions等）はすべてNo accessのまま。

### 3.3 Secrets Managerへの登録

```bash
aws secretsmanager create-secret \
  --name "catch-expander/github-token" \
  --secret-string "github_pat_xxxxxxxxxxxx"
```

### 3.4 トークンの有効期限管理

Fine-grained PATには有効期限がある。期限切れ前に再発行し、Secrets Managerを更新する。

```bash
# トークン更新
aws secretsmanager update-secret \
  --secret-id "catch-expander/github-token" \
  --secret-string "github_pat_new_xxxxxxxxxxxx"
```

---

## 4. Claude Code MaxプランOAuth

### 4.1 Maxプランの契約

1. https://claude.ai にアクセス
2. Maxプランに加入（月額固定）

### 4.2 Claude Code CLIの認証

ECSコンテナ内でClaude Code CLIのOAuth認証を行う。

```bash
# 初回認証（対話式）
claude login

# 認証状態の確認
claude --version
```

### 4.3 ECSコンテナでの認証永続化

Claude CodeのOAuth認証情報はホームディレクトリ配下（`~/.claude/`）に保存される。ECS Fargateのファイルシステムはエフェメラル（タスク終了で消滅）のため、タスク起動のたびに認証情報を復元する必要がある。

#### 方式: 起動時にSecrets Managerから復元（推奨）

認証情報をSecrets Managerに保存し、タスク起動時にコンテナ内へ配置する。

```
ECSタスク起動
  → 起動スクリプトがSecrets Managerから認証情報を取得
  → ~/.claude/ に配置
  → Claude Code CLI実行
  → タスク終了（ファイルシステムごと消滅、認証情報も消滅）
```

この方式により:
- Dockerイメージにクレデンシャルが含まれない（ECRからイメージを取得しても認証情報は漏洩しない）
- 永続ストレージ（EFS等）にクレデンシャルが残らない
- 認証情報が存在するのはタスク実行中のコンテナ内のみ

#### 認証情報の初回取得と登録

ローカル環境でClaude Code CLIにログインし、生成された認証ファイルをSecrets Managerに登録する。

```bash
# 1. ローカルでClaude Code CLIにログイン（対話式）
claude login

# 2. 認証ファイルの内容をSecrets Managerに登録
aws secretsmanager create-secret \
  --name "catch-expander/claude-oauth" \
  --secret-string file://~/.claude/.credentials.json
```

実際の認証ファイルパスは `~/.claude/.credentials.json`（先頭ドット）。

### 4.4 OAuth トークンの自動延命と失敗時通知

#### 自動延命（Token Refresher Lambda）

Token Refresher Lambda（`src/token_monitor/handler.py`、EventBridge Scheduler が 12 時間ごとに起動）が、
Secrets Manager 上の `claudeAiOauth.refreshToken` を使って Anthropic OAuth エンドポイント
（`https://platform.claude.com/v1/oauth/token`）に直接リクエストし、新 access_token を取得して
Secrets Manager を上書きする。

判定基準: 残り < 1 時間または失効済みのとき refresh をトリガー。残り十分なら no-op。

#### 失敗時通知

refresh が HTTP 4xx/5xx で失敗、または `refreshToken` が credentials に存在しない場合のみ、
Slack 通知チャンネルへ「再認証が必要」案内を投稿する。通知文には失敗理由（`http_401` /
`no_refresh_token` 等）と最終 expiresAt を含む。

通知を受信したら、ローカル PC で 1 回 `claude` 認証して `~/.claude/.credentials.json` を
Secrets Manager に再投入する。詳細は `.steering/20260425-auth-redesign-aipapers/initial-setup.md`
を参照。

#### ECS タスク内での書き戻し

ECS Fargate タスクは起動時に Secrets Manager から credentials を取得して `~/.claude/.credentials.json`
に展開し、タスク終了時に内容に変化があれば Secrets Manager に書き戻す（ベストエフォート）。
これは `claude` CLI がタスク実行中に内部的に refresh した場合の救済。

#### 不採用とした方式

| 方式 | 不採用理由 |
|------|-----------|
| Dockerイメージに焼き込み | ECRアクセス権限を持つ人がイメージからクレデンシャルを抽出できるため、セキュリティリスクが高い |
| EFSマウント | 永続ストレージにクレデンシャルが残留する。EFSのアクセス制御（IAM + セキュリティグループ）も追加で必要になり、管理が複雑化する |

---

## 5. Secrets Manager のシークレット一覧（まとめ）

| シークレット名 | 内容 |
|---------------|------|
| `catch-expander/slack-bot-token` | Slack Bot Token（`xoxb-`） |
| `catch-expander/slack-signing-secret` | Slack Signing Secret |
| `catch-expander/notion-token` | Notion Integration Token（`ntn_`） |
| `catch-expander/github-token` | GitHub Fine-grained PAT（`github_pat_`） |
| `catch-expander/claude-oauth` | Claude Code OAuth認証情報 |

すべてのシークレットは `catch-expander/` プレフィックスで統一する。IAMポリシーでこのプレフィックスに限定したアクセス制御が可能。

```json
{
  "Effect": "Allow",
  "Action": "secretsmanager:GetSecretValue",
  "Resource": "arn:aws:secretsmanager:ap-northeast-1:*:secret:catch-expander/*"
}
```
