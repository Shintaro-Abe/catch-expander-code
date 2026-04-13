#!/bin/bash
# sync_claude_token.sh: ~/.claude/.credentials.json の内容を
# Secrets Manager (catch-expander/claude-oauth) に同期する。
set -euo pipefail

CREDENTIALS_FILE="$HOME/.claude/.credentials.json"
SECRET_ID="catch-expander/claude-oauth"
REGION="ap-northeast-1"

if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo "[sync-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Credentials file not found, skipping." >&2
    exit 0
fi

echo "[sync-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Syncing to Secrets Manager..."
# --secret-string に直接値を渡すとプロセス一覧 (ps aux / /proc/PID/cmdline) に
# トークンが露出するため、0600 権限の一時ファイル経由で渡す。
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
    --cli-input-json "file://$TMPFILE" 2>&1

echo "[sync-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Sync complete."
