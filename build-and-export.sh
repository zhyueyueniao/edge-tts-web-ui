#!/usr/bin/env bash
# ============================================
#  edge-tts 镜像一键构建并导出（WSL / Linux / macOS）
#  用法： bash build-and-export.sh [pip镜像地址]
#  例：   bash build-and-export.sh https://pypi.tuna.tsinghua.edu.cn/simple
#  产物： edge-tts-web-ui.tar.gz（tag 固定 latest，不含版本号）
#  导入： docker load -i edge-tts-web-ui.tar.gz
# ============================================
set -euo pipefail

IMAGE="edge-tts-web-ui"
TAG="latest"
MIRROR="${1:-}"

ARGS=()
[ -n "$MIRROR" ] && ARGS+=(--build-arg "PIP_INDEX_URL=$MIRROR")

echo "[1/3] 构建镜像 ${IMAGE}:${TAG} ..."
docker build "${ARGS[@]}" -t "${IMAGE}:${TAG}" .

echo "[2/3] 冒烟测试容器 ..."
docker rm -f edge-tts-smoke >/dev/null 2>&1 || true
docker run -d --name edge-tts-smoke -p 15050:5050 "${IMAGE}:${TAG}" >/dev/null
sleep 6
curl -s http://127.0.0.1:15050/health && echo ""
docker rm -f edge-tts-smoke >/dev/null

echo "[3/3] 导出镜像 ..."
docker save "${IMAGE}:${TAG}" | gzip > "${IMAGE}.tar.gz"
ls -lh "${IMAGE}.tar.gz"
echo "完成。导入命令： docker load -i ${IMAGE}.tar.gz"
