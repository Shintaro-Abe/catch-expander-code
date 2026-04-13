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
aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ID" \
    --secret-string "$(cat "$CREDENTIALS_FILE")" \
    --region "$REGION" \
    --output text \
    --query 'VersionId' 2>&1

echo "[sync-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Sync complete."
