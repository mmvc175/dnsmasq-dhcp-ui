#!/usr/bin/env bash
# 镜像构建脚本
#
#   ./scripts/build.sh                     # 构建当前架构，标签 dnsmasq-dhcp-ui:latest
#   ./scripts/build.sh --multiarch         # 构建 amd64 + arm64（需要 buildx）
#   ./scripts/build.sh --push user/repo    # 多架构并推送到仓库
#
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-dnsmasq-dhcp-ui}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH=0
MULTI=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --multiarch) MULTI=1; shift ;;
    --push)      PUSH=1; MULTI=1; if [[ $# -gt 1 && "$2" != -* ]]; then IMAGE="$2"; shift; fi; shift ;;
    --platforms) PLATFORMS="$2"; MULTI=1; shift 2 ;;
    --image)     IMAGE="$2"; shift 2 ;;
    --tag)       TAG="$2"; shift 2 ;;
    -h|--help)   sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [[ "$MULTI" -eq 1 ]]; then
  echo "==> 多架构构建: ${IMAGE}:${TAG} (${PLATFORMS})"
  if ! docker buildx inspect dhcpui-builder >/dev/null 2>&1; then
    docker buildx create --name dhcpui-builder --use
  else
    docker buildx use dhcpui-builder
  fi
  if [[ "$PUSH" -eq 1 ]]; then
    docker buildx build --platform "$PLATFORMS" -t "${IMAGE}:${TAG}" --push .
    echo "==> 已推送 ${IMAGE}:${TAG}"
  else
    # 不推送时只能导出单个平台，默认导出当前平台
    docker buildx build --platform "$(docker version -f '{{.Server.Arch}}' 2>/dev/null | sed 's|^|linux/|')" \
      -t "${IMAGE}:${TAG}" --load .
    echo "==> 已本地加载 ${IMAGE}:${TAG}（多架构镜像需加 --push 才会生成 manifest）"
  fi
else
  echo "==> 单架构构建: ${IMAGE}:${TAG}"
  docker build -t "${IMAGE}:${TAG}" .
  echo "==> 完成。运行: docker run -d --network host --cap-add=NET_ADMIN --cap-add=NET_RAW \\
         -v \$(pwd)/data:/data -e WEB_USER=admin -e WEB_PASS=secret ${IMAGE}:${TAG}"
fi
