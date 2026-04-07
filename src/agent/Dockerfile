FROM node:22-slim

# 非rootユーザー作成
RUN groupadd -r appuser && useradd -r -g appuser -m appuser

# Claude Code CLIのインストール
RUN npm install -g @anthropic-ai/claude-code

# Python（ラッパースクリプト用）
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# アプリケーションコード
COPY . /app/
WORKDIR /app

# Python依存パッケージ
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# 非rootユーザーで実行
USER appuser

ENTRYPOINT ["python3", "main.py"]
