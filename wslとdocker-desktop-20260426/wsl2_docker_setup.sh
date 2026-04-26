#!/usr/bin/env bash
# PoC品質: このスクリプトは概念実証用のスケルトンです。本番環境での使用前に十分なレビューを行ってください。
#
# WSL2 + Docker Desktop 開発環境セットアップスクリプト
# Ubuntu 22.04 / WSL2 での AWS CDK + Lambda + SAM CLI 環境を構築する
#
# 使い方:
#   chmod +x wsl2_docker_setup.sh
#   ./wsl2_docker_setup.sh
#
# 前提条件（Windows 側）:
#   - Docker Desktop for Windows がインストール済みで WSL2 バックエンドが有効
#   - Settings > Resources > WSL Integration で Ubuntu が ON になっていること
#   - ~/.wslconfig でリソース上限が設定済みであること（下記参照）

set -euo pipefail

LOG_PREFIX="[WSL2-SETUP]"
info()  { echo "${LOG_PREFIX} INFO:  $*"; }
warn()  { echo "${LOG_PREFIX} WARN:  $*" >&2; }
error() { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. OS 確認
# ---------------------------------------------------------------------------
if ! grep -qi microsoft /proc/version 2>/dev/null; then
    warn "WSL2 環境ではない可能性があります。継続しますか? (y/N)"
    read -r ans
    [[ "${ans}" =~ ^[Yy]$ ]] || error "セットアップを中止しました"
fi

# ---------------------------------------------------------------------------
# 1. apt パッケージ更新 + 基本ツール導入
# ---------------------------------------------------------------------------
info "パッケージを更新しています..."
sudo apt-get update -q
sudo apt-get install -y -q \
    curl \
    unzip \
    git \
    python3-pip \
    python3-venv \
    jq \
    ca-certificates

# ---------------------------------------------------------------------------
# 2. Docker グループへの追加（Docker Desktop WSL2 統合が有効な場合に必要）
# ---------------------------------------------------------------------------
if ! getent group docker > /dev/null 2>&1; then
    sudo groupadd docker
fi

if ! id -nG "${USER}" | grep -qw docker; then
    info "Docker グループにユーザーを追加しています..."
    sudo usermod -aG docker "${USER}"
    warn "グループ変更を反映するために新しいターミナルを開いてください (newgrp docker)"
fi

# Docker 動作確認
if docker info > /dev/null 2>&1; then
    info "Docker Desktop との接続を確認しました"
    docker --version
else
    warn "Docker Desktop が起動していないか、WSL2 統合が無効です"
    warn "Docker Desktop > Settings > Resources > WSL Integration を確認してください"
fi

# ---------------------------------------------------------------------------
# 3. Node.js 20 LTS インストール（AWS CDK 用）
# ---------------------------------------------------------------------------
if ! command -v node > /dev/null 2>&1; then
    info "Node.js 20 LTS をインストールしています..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi
info "Node.js: $(node --version), npm: $(npm --version)"

# ---------------------------------------------------------------------------
# 4. AWS CDK CLI + esbuild インストール
# ---------------------------------------------------------------------------
if ! command -v cdk > /dev/null 2>&1; then
    info "AWS CDK CLI をインストールしています..."
    sudo npm install -g aws-cdk
fi
info "CDK: $(cdk --version)"

# esbuild（CDK NodejsFunction の高速ローカルバンドル用）
if ! command -v esbuild > /dev/null 2>&1; then
    info "esbuild をインストールしています..."
    sudo npm install -g esbuild
fi

# ---------------------------------------------------------------------------
# 5. AWS CLI v2 インストール
# ---------------------------------------------------------------------------
if ! command -v aws > /dev/null 2>&1; then
    info "AWS CLI v2 をインストールしています..."
    TMP_DIR=$(mktemp -d)
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "${TMP_DIR}/awscliv2.zip"
    unzip -q "${TMP_DIR}/awscliv2.zip" -d "${TMP_DIR}"
    sudo "${TMP_DIR}/aws/install"
    rm -rf "${TMP_DIR}"
fi
info "AWS CLI: $(aws --version)"

# ---------------------------------------------------------------------------
# 6. AWS SAM CLI インストール
# ---------------------------------------------------------------------------
if ! command -v sam > /dev/null 2>&1; then
    info "AWS SAM CLI をインストールしています..."
    TMP_DIR=$(mktemp -d)
    curl -fsSL "https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-x86_64.zip" \
        -o "${TMP_DIR}/sam.zip"
    unzip -q "${TMP_DIR}/sam.zip" -d "${TMP_DIR}/sam-install"
    sudo "${TMP_DIR}/sam-install/install"
    rm -rf "${TMP_DIR}"
fi
info "SAM CLI: $(sam --version)"

# ---------------------------------------------------------------------------
# 7. Python 仮想環境 + CDK 依存ライブラリ
# ---------------------------------------------------------------------------
VENV_DIR="${HOME}/.venvs/wsl-docker-cdk"
if [[ ! -d "${VENV_DIR}" ]]; then
    info "Python 仮想環境を作成しています: ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
fi

# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"
pip install -q --upgrade pip
pip install -q \
    aws-cdk-lib \
    constructs \
    boto3

info "Python: $(python3 --version)"
info "CDK ライブラリ: $(pip show aws-cdk-lib | grep Version)"

# ---------------------------------------------------------------------------
# 8. .wslconfig の推奨設定を表示（Windows 側で手動適用）
# ---------------------------------------------------------------------------
cat <<'WSLCONFIG'

===========================================================
 推奨 .wslconfig 設定（Windows 側: C:\Users\<USER>\.wslconfig）
===========================================================
[wsl2]
processors=4
memory=8GB
swap=2GB

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true

# 適用方法: PowerShell で wsl --shutdown を実行後、WSL2 を再起動
===========================================================

WSLCONFIG

# ---------------------------------------------------------------------------
# 9. DNS 設定（企業プロキシ環境での SAM CLI ビルド失敗対策）
# ---------------------------------------------------------------------------
WSL_CONF="/etc/wsl.conf"
if ! grep -q "generateResolvConf=false" "${WSL_CONF}" 2>/dev/null; then
    warn "/etc/wsl.conf に generateResolvConf=false が設定されていません"
    warn "企業プロキシ環境では以下を /etc/wsl.conf に追記してください:"
    echo "  [network]"
    echo "  generateResolvConf=false"
    echo ""
    warn "その後 /etc/resolv.conf を手動設定してください:"
    echo "  nameserver 1.1.1.1"
    echo "  nameserver 1.0.0.1"
fi

# ---------------------------------------------------------------------------
# 10. AWS 認証情報の確認
# ---------------------------------------------------------------------------
info "AWS 認証情報を確認しています..."
if aws sts get-caller-identity > /dev/null 2>&1; then
    info "AWS 認証済み:"
    aws sts get-caller-identity --output table
else
    warn "AWS 認証情報が設定されていません"
    warn "以下のいずれかで設定してください:"
    echo "  1) aws configure"
    echo "  2) ln -s /mnt/c/Users/<WindowsUser>/.aws ~/.aws  (Windows と共有)"
    echo "  3) aws sso login --profile <your-sso-profile>"
fi

info "セットアップ完了。新しいターミナルを開いて設定を反映してください。"
