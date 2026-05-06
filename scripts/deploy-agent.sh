#!/bin/bash
# Catch-Expander エージェントコンテナのビルド & デプロイスクリプト。
#
# git の HEAD コミット SHA をタグとして ECR に push し、その SHA を AgentImageUri
# パラメータに渡して sam deploy する。
#
# ECR は IMMUTABLE 設定のため、同じ SHA を 2 回 push するとエラーになる
# （= ワーキングツリーに変更を入れて新しいコミットを作る、もしくは強制的に
#  --tag-suffix を付ける運用が必要）。
#
# 前提:
# - AWS CLI 認証済み
# - docker daemon 起動中
# - sam CLI インストール済み
# - 環境: linux/arm64（CodeBuild ではなくローカル build を想定。
#   M1/M2 Mac なら native、Intel Mac/Linux x86 なら buildx で arm64 をクロスビルド）

set -euo pipefail

# ---- 設定 ---------------------------------------------------------------
REGION="${AWS_REGION:-ap-northeast-1}"
REPO_NAME="catch-expander-agent"
DOCKERFILE_DIR="src/agent"

# ---- 派生値 -------------------------------------------------------------
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_HOST="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ECR_URI="${ECR_HOST}/${REPO_NAME}"
SHA=$(git rev-parse HEAD)
SHORT_SHA=$(git rev-parse --short HEAD)

# ワーキングツリーがダーティなら警告（強制終了はしない、急ぎの hotfix 想定）
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "⚠️  WARNING: working tree has uncommitted changes."
  echo "   Building from current files; the resulting image may not match"
  echo "   commit ${SHORT_SHA} exactly."
  echo
fi

echo "==> SHA: ${SHA}"
echo "==> ECR: ${ECR_URI}:${SHA}"
echo

# ---- ECR login ----------------------------------------------------------
echo "==> ECR login"
aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${ECR_HOST}"

# ---- build (linux/arm64) -----------------------------------------------
# Dockerfile は repo root を build context として参照される箇所があるため
# (src/observability/ を COPY するなど)、context は repo root にする。
echo "==> docker build (linux/arm64)"
docker buildx build \
  --platform linux/arm64 \
  --file "${DOCKERFILE_DIR}/Dockerfile" \
  --tag "${ECR_URI}:${SHA}" \
  --push \
  .

echo
echo "==> Image pushed: ${ECR_URI}:${SHA}"
echo

# ---- sam deploy ---------------------------------------------------------
# samconfig.toml の parameter_overrides は文字列連結なので、AgentImageUri
# だけ追加で渡す。CodexAuthSecretArn / FrontendDomain 等は samconfig 側で解決。
echo "==> sam deploy"
sam deploy \
  --parameter-overrides "AgentImageUri=${ECR_URI}:${SHA}" \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset

echo
echo "✅ Deploy complete."
echo "   ECR image: ${ECR_URI}:${SHA}"
echo "   Short SHA: ${SHORT_SHA}"
