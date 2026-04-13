#!/bin/bash
# post_start.sh: コンテナ起動のたびに実行されるスクリプト。
# Claude OAuth トークンの監視プロセスをバックグラウンドで起動する。

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# inotify-tools が未インストールなら中断
if ! command -v inotifywait &> /dev/null; then
    echo "[post-start] inotifywait not found. Run post_create.sh first." >&2
    exit 1
fi

# 既存の watch プロセスがあれば終了させてから再起動
pkill -f "watch_claude_token.sh" 2>/dev/null || true

nohup bash "$SCRIPT_DIR/watch_claude_token.sh" > /dev/null 2>&1 &
echo "[post-start] Claude token watcher started (PID $!)."
