#!/usr/bin/env bash
# 构建并可选推送多架构镜像（默认 ghcr.io/maotaozi54/nas-app）。
# 用法：
#   scripts/build-image.sh                 # 本机架构，只构建
#   scripts/build-image.sh --push          # amd64 + arm64 推送
#   scripts/build-image.sh --tag 0.19.0 --push
set -euo pipefail
cd "$(dirname "$0")/.."

image="${IMAGE:-ghcr.io/maotaozi54/nas-app}"
tag="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
platforms="linux/amd64,linux/arm64"
push=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) tag="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --push) push="--push"; shift ;;
    --platforms) platforms="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装 Docker Desktop / Docker Engine。" >&2
  exit 1
fi

echo "镜像: $image:$tag  平台: $platforms"
if [[ -n "$push" ]]; then
  docker buildx build --platform "$platforms" -t "$image:$tag" -t "$image:latest" --push .
else
  docker build -t "$image:$tag" -t "$image:latest" .
fi
echo "完成。"
