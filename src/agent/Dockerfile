FROM node:22-slim

# 非rootユーザー作成
RUN groupadd -r appuser && useradd -r -g appuser -m appuser

# Codex CLI のインストール
# Claude Code CLI は claude-agent-sdk (requirements.txt に pin) が wheel に同梱するものを
# subprocess 起動するため、npm での別途インストールは不要 (T24)。
# 同梱 CLI のバージョンは SDK pin (0.2.110 → CLI 2.1.191) に決定的に追従する。
# ※ 旧記述「SDK は CLI を同梱せず PATH 上の claude を使う」は誤りで、npm pin の 2.1.207 は
#   SDK 経由では一度も使われていなかった (exec-20260711154440-67fed1d5 調査で判明)。
RUN npm install -g @openai/codex@0.125.0 \
    && codex --version

# Python（ラッパースクリプト用）と curl（レビュアープロンプトの出典URL検証用）
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコード
# build context は repo root (T1-2 P1 対応)。
# - src/agent/ を /app/ に直置き (既存の `from notify.* import ...` 等を維持)
# - src/observability/ を /app/src/observability/ に配置して
#   orchestrator.py の `from src.observability import EventEmitter` を
#   コンテナ内でも解決可能にする (Codex P1: 観測 dead code 解消)。
COPY src/agent/ /app/
COPY src/observability/ /app/src/observability/
RUN : > /app/src/__init__.py
WORKDIR /app

# Python依存パッケージ
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# 非rootユーザーで実行
USER appuser

ENTRYPOINT ["python3", "main.py"]
