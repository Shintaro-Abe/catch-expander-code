#!/bin/bash
# watch_claude_token.sh: ~/.claude/.credentials.json の変化を監視し、
# 変更があるたびに sync_claude_token.sh を呼び出す。
# devcontainer 起動時にバックグラウンドで実行される。

CREDENTIALS_DIR="$HOME/.claude"
CREDENTIALS_FILE=".credentials.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/sync_claude_token.sh"
LOG_FILE="$HOME/.claude/token_sync.log"

mkdir -p "$CREDENTIALS_DIR"

# 起動時に一度同期
"$SYNC_SCRIPT" >> "$LOG_FILE" 2>&1

echo "[watch-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Watching $CREDENTIALS_DIR/$CREDENTIALS_FILE for changes..." >> "$LOG_FILE"

# ファイルの変更を監視（ディレクトリ全体を監視してファイル名でフィルタ）
inotifywait -m -e close_write,moved_to "$CREDENTIALS_DIR" 2>/dev/null |
while read -r _directory _event filename; do
    if [ "$filename" = "$CREDENTIALS_FILE" ]; then
        echo "[watch-claude-token] $(date '+%Y-%m-%d %H:%M:%S') Change detected, syncing..." >> "$LOG_FILE"
        "$SYNC_SCRIPT" >> "$LOG_FILE" 2>&1
    fi
done
