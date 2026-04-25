# 手順書: iPhone から AWS CloudShell 経由で Claude OAuth 再認証

> **⚠️ この手順は 2026-04-25 に廃止されました。**
>
> Claude OAuth の認証は ai-papers-digest 方式（Token Refresher Lambda による自動延命）に置き換わりました。
> 現行の手順は `.steering/20260425-auth-redesign-aipapers/initial-setup.md` を参照してください。
>
> 廃止理由（2026-04-23 判明）:
> - Claude Code 2.1.118 で CloudShell では `~/.claude/.credentials.json` が生成されない
> - `sync-claude.sh` が「ファイル sync 成功」と「OAuth refresh 成功」を区別できない設計欠陥
> - 上記により、ユーザーが手順を完走しても 401 エラーが継続する事故が発生
>
> 本ファイルは履歴として保持されます。

本書は `requirements.md` の受け入れ条件を満たす、iPhone Safari のみで完結する Claude OAuth 再認証手順を記載する。

> **ステータス: 廃止（2026-04-25）**
> - PoC-1: ✅ `claude login` はデバイスフロー（`https://claude.ai/activate?code=...`）で成立。Safari 別タブで OAuth 完了可能
> - PoC-2: ✅ CloudShell 既定 Node.js で `@anthropic-ai/claude-code` が導入可能
> - PoC-3: ✅ iPhone Safari で §2.1〜2.6 を完走
> - 最終確認: ❌ 2026-04-23 22:48 JST、Lambda 経由の実機検証で stale 状態が解消されないことが判明し廃止

---

## 0. 前提

- 日常の AWS 操作用とは **別に、本再認証専用の IAM ユーザー** `catch-expander-reauth` を新規作成する（§1.1）
  - 既存 `AdministratorAccess` ユーザーを iPhone から使わないことで、iPhone compromise 時の爆発半径を対象シークレット 1 本 + Lambda 1 本に限定する
- 対象シークレット `catch-expander/claude-oauth` が Secrets Manager（`ap-northeast-1`）に既に存在する
- Claude Max プランの OAuth ログインができる（claude.ai で認証済み）
- iPhone 単体で完結すること（PC 不要）

---

## 1. 初回セットアップ（PC もしくは既存 DevContainer から 1 回だけ）

再認証専用の IAM ユーザーを新規作成し、最小権限ポリシーを付与する。
**このセクションは `template.yaml`（SAM）には含めない**。個人ユーザー権限として手動で管理する。

### 1.1 専用 IAM ユーザーの作成

**admin 権限を持つ既存ユーザーで** AWS Console にログインし、以下を実施する（PC からの作業推奨）。

1. AWS Console → **IAM** → ユーザー → 「ユーザーの作成」
2. ユーザー名: `catch-expander-reauth`
3. 「AWS Management Console へのユーザーアクセスを提供する」にチェック
   - 「IAM ユーザーを作成したい」を選択
   - コンソールパスワード: 「**カスタムパスワード**」で強度の高いものを設定（iCloud Keychain で管理）
   - 「ユーザーは次回のサインイン時に新しいパスワードを作成する必要があります」: **OFF**（MFA 設定までを同一セッションで完結させるため）
4. アクセス許可: 「**ポリシーを直接アタッチする**」→ 何も選ばず「次へ」
   - この時点では権限ゼロ。§1.3 で最小権限を付ける
5. 「ユーザーの作成」→ サインイン URL を控える
   ```
   https://<AWS_ACCOUNT_ID_OR_ALIAS>.signin.aws.amazon.com/console
   ```

> **重要**: 「**アクセスキーを作成**」は**絶対に実施しない**。
> 本ユーザーはコンソールログイン専用。CLI 用の静的クレデンシャルを発行しない方針（`requirements.md` の制約）。

### 1.2 passkey MFA の設定

作成直後、同じブラウザで一度 `catch-expander-reauth` にログインし直して MFA を設定する（admin が代行するより、ユーザー自身のセッションで登録した方が安全）。

1. 一度サインアウト → §1.1 で控えたサインイン URL にアクセス
2. ユーザー名 `catch-expander-reauth` + 設定したパスワードでログイン
3. 右上ユーザー名 → 「セキュリティ認証情報」
4. 「マルチファクタ認証（MFA）」→ 「**MFA デバイスを割り当てる**」
5. デバイス名: `iphone-passkey`（任意）
6. MFA デバイスの種類: 「**Passkey またはセキュリティキー**」を選択
7. ブラウザの WebAuthn プロンプトで登録
   - iPhone Safari なら Face ID でその場登録（iCloud Keychain にパスキー保存）
   - PC ブラウザなら「別のデバイス」→ QR コードを iPhone で読み取り iCloud Keychain に保存
8. 登録完了後、一度サインアウト → 再ログインで passkey が要求されることを確認

> TOTP / SMS のみは phishing 耐性が無いため本手順では使用しない。パスキー登録ができない環境なら YubiKey 等の FIDO2 セキュリティキーを使う。

### 1.3 IAM ポリシーのアタッチ（インライン）

admin ユーザーに戻って、`catch-expander-reauth` に最小権限ポリシーを付与する。

1. AWS Console → IAM → ユーザー → `catch-expander-reauth` を選択
2. 「アクセス許可」タブ → 「許可を追加」→ 「**インラインポリシーを作成**」
3. JSON エディタに以下を貼付け
4. ポリシー名: `CatchExpanderClaudeOAuthReauth`
5. 「ポリシーの作成」

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudShellAccess",
      "Effect": "Allow",
      "Action": [
        "cloudshell:CreateEnvironment",
        "cloudshell:StartEnvironment",
        "cloudshell:CreateSession",
        "cloudshell:GetEnvironmentStatus",
        "cloudshell:PutCredentials"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ManageClaudeOAuthSecret",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:PutSecretValue",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:ap-northeast-1:*:secret:catch-expander/claude-oauth-*",
      "Condition": {
        "StringEquals": { "aws:RequestedRegion": "ap-northeast-1" }
      }
    },
    {
      "Sid": "InvokeTokenMonitor",
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:ap-northeast-1:*:function:catch-expander-token-monitor"
    },
    {
      "Sid": "ReadTokenMonitorLogs",
      "Effect": "Allow",
      "Action": [
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:ap-northeast-1:*:log-group:/aws/lambda/catch-expander-token-monitor:*",
      "Condition": {
        "StringEquals": { "aws:RequestedRegion": "ap-northeast-1" }
      }
    }
  ]
}
```

> - リソース ARN 末尾の `-*` は Secrets Manager が自動付与するランダムサフィックスに合わせるため必須
> - 他のシークレット（`slack-bot-token` 等）にはアクセスできない
> - `aws:RequestedRegion` Condition で ap-northeast-1 以外への誤適用を構造的に防止

### 1.4 セットアップ検証（推奨）

admin をサインアウトし、`catch-expander-reauth` で一度 CloudShell を起動して以下を確認する:

```bash
# 想定どおりの権限スコープかチェック
aws sts get-caller-identity
# => UserId: AIDAXXXXXXXX, Arn: arn:aws:iam::<ACCOUNT>:user/catch-expander-reauth

aws secretsmanager describe-secret \
  --region ap-northeast-1 \
  --secret-id catch-expander/claude-oauth
# => 成功

aws secretsmanager describe-secret \
  --region ap-northeast-1 \
  --secret-id catch-expander/slack-bot-token
# => AccessDeniedException が返る（想定どおり最小権限）
```

### 1.5 リージョン固定

- CloudShell は **ap-northeast-1（東京）** で起動する
- 理由: Secrets Manager のシークレットが東京リージョンにあるため、同一リージョンで完結させる（クロスリージョン呼び出しと、CloudShell のホームディレクトリをリージョンごとに重複させない）

---

## 2. iPhone からの再認証フロー

### 2.1 AWS Console ログイン

**必ず専用ユーザー `catch-expander-reauth` でログインする**（admin ユーザーは使わない）。

1. iPhone Safari で **アカウント専用サインイン URL** を開く（ブックマーク推奨）
   ```
   https://<AWS_ACCOUNT_ID_OR_ALIAS>.signin.aws.amazon.com/console
   ```
   - 標準の `https://signin.aws.amazon.com/` からでも可だが、アカウント ID 入力の手間 + フィッシング誘導リスクが増す
2. ユーザー名: `catch-expander-reauth` + パスワードを入力（iCloud Keychain 自動入力可）
3. MFA プロンプトで **passkey（Face ID）** で認証
   - パスキーは登録ドメインにしか署名しないため、偽サイトでは原理的に認証不能（phishing-resistant）

### 2.2 CloudShell 起動

1. AWS Console 右上のリージョンセレクタで **Asia Pacific (Tokyo) ap-northeast-1** を選択
2. 上部ツールバーの CloudShell アイコン（ `>_` ）をタップ
3. 初回起動時は 30〜60 秒程度でプロンプトが表示される

> iPhone Safari ではターミナルの長押し → 「ペースト」メニューが使える。ハードウェアキーボード利用を推奨するが、ソフトキーボードでも実施可能。

### 2.3 Node.js / Claude CLI のセットアップ

CloudShell のホームディレクトリ（`$HOME`）は **1GB 上限で 120 日永続**する。一度入れた CLI は次回も再利用できる。

```bash
# 既にインストール済みかチェック
command -v claude && claude --version

# 未導入なら Node.js LTS を $HOME/.local に配置
# ※ PoC-2 で CloudShell の既定 Node バージョンが 18+ なら `npm i -g` が使える
node --version  # v18 以上であることを確認
# 18 未満の場合は nvm で LTS を導入する
if ! command -v nvm &>/dev/null; then
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
fi
nvm install --lts

# Claude CLI を $HOME 配下にインストール（sudo 不要）
npm config set prefix "$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
npm install -g @anthropic-ai/claude-code

# 永続化: .bashrc に PATH を追加（初回のみ）
if ! grep -Fq '.local/bin' ~/.bashrc; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

claude --version
```

### 2.4 Claude OAuth ログイン

```bash
claude login
```

> **挙動（PoC で確定）**: CloudShell では **デバイスフロー** が起動し、`https://claude.ai/activate?code=...` 形式の URL が表示される。
> - 表示された URL を長押しで別タブで開き、iPhone 上で OAuth を完了させる
> - 認証成功後、Claude Code CLI はそのまま対話モードへ遷移するので、**`/exit` で CLI を終了する**（終了しないと §2.5 以降に進めない）

ログイン後、認証ファイルが生成されることを確認:

```bash
ls -la ~/.claude/.credentials.json
jq -r '.claudeAiOauth.expiresAt' ~/.claude/.credentials.json
# => ミリ秒 UNIX 時刻（例: 1745000000000）が返れば OK
```

### 2.5 Secrets Manager への反映

`sync_claude_token.sh` と同じパターン（`--cli-input-json file://...`）でプロセス一覧へのトークン露出を防ぐ。

```bash
SECRET_ID="catch-expander/claude-oauth"
REGION="ap-northeast-1"
CREDENTIALS_FILE="$HOME/.claude/.credentials.json"

TMPFILE="$(mktemp)"
chmod 0600 "$TMPFILE"
trap 'rm -f "$TMPFILE"' EXIT

jq -n \
  --arg id "$SECRET_ID" \
  --rawfile s "$CREDENTIALS_FILE" \
  '{SecretId: $id, SecretString: $s}' > "$TMPFILE"

aws secretsmanager put-secret-value \
  --region "$REGION" \
  --output text \
  --query 'VersionId' \
  --cli-input-json "file://$TMPFILE"
```

出力された VersionId を控える（検証用）。

### 2.6 反映確認

Claude Code CLI が出力する認証ファイルの構造は以下（`src/token_monitor/handler.py:28` 参照）:

```json
{
  "claudeAiOauth": {
    "accessToken": "...",
    "refreshToken": "...",
    "expiresAt": 1745000000000
  }
}
```

`expiresAt` は **ミリ秒 UNIX 時刻**、かつ **`claudeAiOauth` にネスト**されている。jq パスを誤ると `null` が返るので注意。

```bash
# 両者の expiresAt を比較
LOCAL_MS=$(jq -r '.claudeAiOauth.expiresAt' ~/.claude/.credentials.json)
REMOTE_MS=$(aws secretsmanager get-secret-value \
  --region ap-northeast-1 \
  --secret-id catch-expander/claude-oauth \
  --query 'SecretString' --output text \
  | jq -r '.claudeAiOauth.expiresAt')

echo "local : $LOCAL_MS"
echo "remote: $REMOTE_MS"
date -d "@$((LOCAL_MS/1000))"  +'local_date : %Y-%m-%d %H:%M:%S %Z'
date -d "@$((REMOTE_MS/1000))" +'remote_date: %Y-%m-%d %H:%M:%S %Z'
[ "$LOCAL_MS" = "$REMOTE_MS" ] && echo "OK: synced" || echo "NG: mismatch"
```

`OK: synced` が出れば、ECS タスクから新しいトークンが参照できる状態。

判定:

| 結果 | 意味 | 次アクション |
|------|------|------------|
| `OK: synced` | ✅ 完全成功。ECS タスクは次回起動時に新トークンを利用 | 完了。§4 でサインアウト |
| `NG: mismatch` | Secrets Manager に古い値が残っている | §2.5 の `put-secret-value` を再実行 |
| `REMOTE_MS` が空 | Secrets Manager への書込が未実施 | §2.5 を実行 |

### 2.7 次回以降の簡略フロー

初回セットアップ（§1）と CLI インストール（§2.3）、wrapper スクリプト配置（§2.7 下記）が完了していれば、次回以降は:

```
iPhone Safari ログイン → CloudShell 起動 → claude login → /exit → ~/sync-claude.sh
```

の 2 コマンドで完結する。`claude login` 単独では Secrets Manager に反映されないため、**必ず `~/sync-claude.sh` をセットで実行する**。

CloudShell ホームは 120 日永続のため、**前回利用から 120 日以内なら §2.3 の再インストールは不要**。

#### wrapper スクリプト（sync 専用・推奨）

`claude login` は認証完了後に **Claude Code CLI の対話モードへ遷移する** ため、同一スクリプト内で `claude login` の直後に sync コマンドを書いても、ユーザーが CLI を明示的に終了するまで後続処理は走らない。
このため wrapper は **sync + 検証部分だけ** を担い、`claude login` 自体はユーザーが手動で実行する 2 ステップ構成とする。

初回セットアップ時に以下を CloudShell ホームへ配置（1 度きり）:

```bash
cat > ~/sync-claude.sh <<'EOF'
#!/bin/bash
# Claude OAuth ローカル認証ファイルを Secrets Manager に同期し、一致を検証する。
# 事前に `claude login` を完了させ、CLI から /exit で戻ってから実行する。
set -euo pipefail

SECRET_ID="catch-expander/claude-oauth"
REGION="ap-northeast-1"
CREDENTIALS_FILE="$HOME/.claude/.credentials.json"

if [ ! -s "$CREDENTIALS_FILE" ]; then
  echo "ERROR: $CREDENTIALS_FILE が存在しないか空です。先に 'claude login' を実行してください。" >&2
  exit 1
fi

echo "=== Step 1: sync to Secrets Manager ==="
TMPFILE="$(mktemp)"
chmod 0600 "$TMPFILE"
trap 'rm -f "$TMPFILE"' EXIT

jq -n \
  --arg id "$SECRET_ID" \
  --rawfile s "$CREDENTIALS_FILE" \
  '{SecretId: $id, SecretString: $s}' > "$TMPFILE"

VERSION_ID=$(aws secretsmanager put-secret-value \
  --region "$REGION" \
  --output text \
  --query 'VersionId' \
  --cli-input-json "file://$TMPFILE")
echo "synced: VersionId=$VERSION_ID"

echo "=== Step 2: verify ==="
LOCAL_MS=$(jq -r '.claudeAiOauth.expiresAt' "$CREDENTIALS_FILE")
REMOTE_MS=$(aws secretsmanager get-secret-value \
  --region "$REGION" --secret-id "$SECRET_ID" \
  --query 'SecretString' --output text \
  | jq -r '.claudeAiOauth.expiresAt')

date -d "@$((LOCAL_MS/1000))"  +'local_date : %Y-%m-%d %H:%M:%S %Z'
date -d "@$((REMOTE_MS/1000))" +'remote_date: %Y-%m-%d %H:%M:%S %Z'
[ "$LOCAL_MS" = "$REMOTE_MS" ] && echo "OK: synced" || { echo "NG: mismatch"; exit 1; }
EOF
chmod +x ~/sync-claude.sh
```

#### 役割分担の原則

| プロセス | 役割 | `expiresAt` の更新 |
|---------|------|------------------|
| `claude login` | OAuth 再認証で `.credentials.json` を新規生成 | ✅ 更新される |
| `claude`（対話モード利用） | refresh token で access token を自動更新 | ✅ 更新される |
| **`~/sync-claude.sh`** | `.credentials.json` を Secrets Manager へミラーするのみ | ❌ **更新されない** |

**`~/sync-claude.sh` 単体ではトークンを再発行しない**。トークン切れに対応するときは、必ず先に `claude login` を打鍵してから `~/sync-claude.sh` を実行する。

#### 次回以降の実行（2 ステップ）

iPhone Safari でログイン → CloudShell 起動 → 以下:

```bash
# Step 1: 認証（対話モードに入る）
claude login

# → 表示された URL を Safari 別タブで開き OAuth を完了
# → Claude Code CLI プロンプトに戻ったら、必ず /exit で CLI を終了する
#   （/exit で終わらないと Step 2 へ進めない）

# Step 2: 同期 + 検証
~/sync-claude.sh
```

出力末尾に `OK: synced` が出ていれば完了。`§3 動作確認` を実行するかはお好みで（Token Monitor は EventBridge Scheduler から 12 時間ごとに自動実行される）。

#### 成功判定の基準

```bash
~/sync-claude.sh
echo "exit_code=$?"
```

| 出力パターン | 判定 | 対処 |
|------------|------|------|
| 末尾に `OK: synced` + `exit_code=0` | ✅ 成功 | 完了 |
| 末尾に `NG: mismatch` + `exit_code=1` | ❌ 不整合 | `~/sync-claude.sh` を再実行（稀に PutSecret と GetSecret の間に書込競合） |
| `ERROR: ... は存在しないか空です` | ❌ 前提不足 | `claude login` が未完了、または CLI を `/exit` せずに実行した |

#### 独立した追加検証（スクリプト出力が流れた場合）

Secrets Manager 側の最新バージョンの `expiresAt` を人間可読で表示:

```bash
aws secretsmanager get-secret-value \
  --region ap-northeast-1 \
  --secret-id catch-expander/claude-oauth \
  --query 'SecretString' --output text \
  | jq -r '.claudeAiOauth.expiresAt' \
  | awk '{printf "%s (= ", $1; system("date -d @" int($1/1000) " +\"%Y-%m-%d %H:%M:%S %Z\"")}'
```

バージョン履歴（最新が `AWSCURRENT` にマーキングされる）:

```bash
aws secretsmanager list-secret-version-ids \
  --region ap-northeast-1 \
  --secret-id catch-expander/claude-oauth \
  --query 'Versions[?VersionStages != `null`].[VersionId,CreatedDate,VersionStages]' \
  --output table
```

---

## 3. 動作確認（任意）

Token Monitor Lambda を手動起動して、Slack 通知が止まる（= 失効検知されなくなる）ことを確認する。

```bash
aws lambda invoke \
  --function-name catch-expander-token-monitor \
  --region ap-northeast-1 \
  /tmp/out.json
cat /tmp/out.json
```

**期待される出力**:
- `"StatusCode": 200`（関数実行成功）
- `/tmp/out.json` の中身は `null`（`handler.py` はトークン有効時に値を返さず Python の暗黙 `None` = Lambda payload `null` になる仕様）
- `"FunctionError"` キーが **無い**こと（あれば内部例外）

詳細ログは CloudWatch Logs `/aws/lambda/catch-expander-token-monitor` で確認:

```bash
aws logs tail /aws/lambda/catch-expander-token-monitor \
  --region ap-northeast-1 --since 5m
# => "Claude OAuth token is valid. Expires at: 2026-04-20T..." が出れば更新反映 OK
# => "Claude OAuth token is STALE" が出るなら Secrets Manager への反映が失敗している
```

ログ行数が多い場合は `grep` で必要行だけ抽出:

```bash
aws logs tail /aws/lambda/catch-expander-token-monitor \
  --region ap-northeast-1 --since 5m \
  | grep -E "valid|STALE"
```

最新行が `Claude OAuth token is valid. Expires at: ...` なら iPhone 経路が本番まで到達している証跡。

---

## 4. セッション終了

- CloudShell ブラウザタブを閉じるだけで可。ホームディレクトリは保存されるため、次回以降 2.3 のインストール手順は不要
- 20 分以上アイドル放置で自動切断されるが、再起動時にホームは復元される

---

## 5. トラブルシューティング

| 症状 | 原因候補 | 対処 |
|------|---------|------|
| `claude login` 後に sync-claude.sh を実行しても `expiresAt` が変わらない | `claude login` 後 `/exit` せず別セッションで sync 実行した / もしくはそもそも `claude login` を走らせていない | 必ず `claude login` → `/exit` → `~/sync-claude.sh` の順で実行する（§2.7 役割分担の原則） |
| `jq` で `null` が返る | パス誤り（`.expiresAt` ではなく `.claudeAiOauth.expiresAt`） | `.claudeAiOauth.expiresAt` を使用 |
| `npm install -g` が Permission denied | `npm config prefix` 未設定 | §2.3 の `npm config set prefix "$HOME/.local"` を先に実行 |
| CloudShell ホームが 1GB に達した | `node_modules` 肥大化 | `rm -rf ~/.npm` で npm キャッシュを削除。それでも不足なら `npx` へ切替 |
| `put-secret-value` が AccessDenied | §1.3 IAM ポリシー未適用 / リージョン不一致 | `catch-expander-reauth` に紐づくポリシーと、CloudShell 起動リージョンを確認 |
| `aws logs tail` が `logs:FilterLogEvents` で AccessDenied | §1.3 ポリシー更新前の古い状態 | §1.3 の最新ポリシーで `ReadTokenMonitorLogs` Sid を追加、インラインポリシーを上書き保存 |
| ECS タスク側で依然として「期限切れ」| トークンが Secrets Manager に届いたが ECS タスクが古いキャッシュを掴んでいる | 次回タスク起動時に自動反映される。緊急時は対象 ECS タスクを停止してリスケジュール |

---

## 6. DevContainer 経路との併存

- 本手順は DevContainer 経由の自動同期（`watch_claude_token.sh`）と**独立**に動作する
- 競合条件: CloudShell と DevContainer が同時に `claude login` を実行するとラストライト・ウィンの上書きが発生するが、いずれの書き込みも有効な OAuth トークンであるため実害はない
- 通常運用では「PC が使えるときは DevContainer」「外出時のみ CloudShell」と使い分ける

---

## 7. 月額コスト試算

本認証方式の運用に伴う追加コストは **実質 $0/月**（既存リソース流用のため純増なし）。

### 7.1 サービス別内訳

| サービス | 課金有無 | 単価 | 月間利用量（想定） | 月額 |
|---------|---------|------|------------------|------|
| AWS CloudShell | 無料 | $0 | セッション数無制限 / 1GB 永続ホーム込み | $0 |
| IAM Identity Center (SSO) | 無料 | $0 | 認証回数無制限 | $0 |
| Secrets Manager（保管） | 既存 | $0.40/シークレット | `catch-expander/claude-oauth` 1 個（既に課金中） | $0（純増なし） |
| Secrets Manager（API） | 従量 | $0.05 / 10,000 calls | 30 回再認証 × 2 call ≒ 60 calls | $0.0003 |
| Lambda 手動 invoke（§3、任意） | 従量 | $0.20 / 1M requests | 30 invokes | $0.00001 |
| 同一リージョン内データ転送 | 無料 | $0 | CloudShell ↔ Secrets Manager (ap-northeast-1) | $0 |
| CloudShell インターネット egress（npm/nvm DL） | 無料 | $0 | 初回のみ数百 MB | $0 |
| CloudTrail 管理イベント | 無料 | $0 | 全 API コールが記録される | $0 |
| KMS（`aws/secretsmanager`） | 無料 | $0 | AWS-managed key 前提 | $0 |

**合計: 月 $0.0004 ≒ ¥0.06**（実質切り捨て $0）

### 7.2 前提と仮定

- 再認証頻度: 月 30 回（毎日 1 回、CloudShell のみ運用の最大ケース）。DevContainer 併用なら更に少ない
- `catch-expander/claude-oauth` シークレットは既存のため $0.40/月は本方式の純増ではない
- KMS は AWS-managed key 使用。customer-managed key へ変更すると **+$1/月** の固定費が発生
- CloudShell は 1 リージョンにつき 1 環境まで無料。ap-northeast-1 固定を守る

### 7.3 コストが跳ねる可能性のあるケース

| シナリオ | 追加コスト | 備考 |
|---------|----------|------|
| customer-managed KMS 採用 | +$1/月 | CMK 1 本の固定費 |
| CloudShell ホームが 1GB 超過 | N/A | 有料拡張なし、ハードリミットで CLI 動作不能に |
| CloudTrail データイベントを Secrets Manager に有効化 | +$0.10 / 100,000 events | 既定では無効 |
| 別リージョンで CloudShell 起動 → クロスリージョン API | データ転送 $0.02/GB | リージョン固定を守れば不要 |
| 誤って Secrets Manager のシークレットを増やす | +$0.40/本/月 | IAM で抑止 |

### 7.4 他方式との月額比較

| 認証方式 | 月額純増 | 備考 |
|---------|---------|------|
| CloudShell（本方式） | $0 | 完全従量 / 最低課金なし |
| Codespaces + OIDC | $0〜 | 無料枠（120 core hours/月）内なら $0、超過 $0.18/core-hour |
| EC2 t4g.nano 常設 | 約 $3.00/月 | On-Demand ap-northeast-1、EBS 込み |
| 既存 DevContainer のみ | $0 | PC 常時利用前提 |

---

## 8. PoC 実施結果（2026-04-20）

初回実行時の観測結果。冒頭のステータスバナー根拠。

- [x] **PoC-1**: `claude login` はデバイスフローで成立。表示された `https://claude.ai/activate?code=...` を Safari 別タブで開き OAuth 完了、完了後 CLI は対話モードへ遷移するため `/exit` で終了する運用に
- [x] **PoC-2**: CloudShell 既定 Node.js で `@anthropic-ai/claude-code` が導入可能（nvm 経由のフォールバックは未発動）
- [x] **PoC-3**: iPhone Safari で §2.1〜2.6 を完走（所要時間 約 5 分、操作上の引っ掛かりなし）
- [x] **最終確認**: Token Monitor Lambda が `null` payload + CloudWatch Logs に `Claude OAuth token is valid. Expires at: 2026-04-20T15:54:48.987000+00:00` を記録

実運用に移行済み。`docs/credential-setup.md` 4.4 節への統合は任意。
