#!/usr/bin/env bash
# 一键构建并推送 python-gdal-web 镜像：
#   1) 前端构建（frontend/ -> gdb2pg/web/static，内含 vue-tsc 类型检查）
#   2) docker buildx build --platform linux/amd64 构建并推送镜像
#   3) 清理本机历史版本镜像（只保留本次构建的版本）
#
# 镜像标签用「日期」区分版本，每次构建同时打两个标签：
#   <registry>/middleware/python-gdal-web:YYYYMMDD   （日期版本，可追溯）
#   <registry>/middleware/python-gdal-web:latest     （最新，部署用）
#
# 用法：
#   bash scripts/build_web.sh                  # 前端构建 + buildx 构建推送 + 清理历史
#   bash scripts/build_web.sh --no-push        # 只构建不推送（本地验证）
#   bash scripts/build_web.sh --no-clean       # 不清理本机历史版本
#   bash scripts/build_web.sh --skip-frontend  # 跳过前端构建（前端没改时加速）
#   bash scripts/build_web.sh --tag 20250911   # 自定义日期版本号
#
# 可用环境变量覆盖：
#   G2P_IMAGE      镜像名（必填，或用本脚本所在仓库的 .env 提供），
#                  例：G2P_IMAGE=registry.example.com/middleware/python-gdal-web
#   G2P_PLATFORM   目标平台（默认 linux/amd64）
#   GDAL_IMAGE     基础镜像（默认官方 ghcr.io/osgeo/gdal:ubuntu-small-3.10.2）
set -euo pipefail
cd "$(dirname "$0")/.."

# 镜像名不含内网 registry 地址，由环境变量提供；也可写在项目根 .env（已 gitignore）
if [[ -z "${G2P_IMAGE:-}" && -f .env ]]; then
  G2P_IMAGE="$(sed -n 's/^[[:space:]]*G2P_IMAGE[[:space:]]*=[[:space:]]*//p' .env \
    | tail -n1 | tr -d '"'"'"' \r')"
fi
IMAGE="${G2P_IMAGE:-}"
if [[ -z "$IMAGE" ]]; then
  cat >&2 <<'USAGE'
未设置镜像名 G2P_IMAGE。

  临时指定：
    G2P_IMAGE=<registry>/middleware/python-gdal-web bash scripts/build_web.sh

  或写入项目根 .env（该文件已 gitignore，不会入库）：
    G2P_IMAGE=<registry>/middleware/python-gdal-web

镜像会打两个标签并推送：<G2P_IMAGE>:YYYYMMDD 与 <G2P_IMAGE>:latest
USAGE
  exit 1
fi

PLATFORM="${G2P_PLATFORM:-linux/amd64}"
TAG="$(date +%Y%m%d)"

DO_PUSH=1
DO_CLEAN=1
DO_FRONTEND=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-push)      DO_PUSH=0 ;;
    --no-clean)     DO_CLEAN=0 ;;
    --skip-frontend) DO_FRONTEND=0 ;;
    --tag)          TAG="${2:?--tag 需要参数}"; shift ;;
    -h|--help)      sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$1（-h 查看用法）" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- 0) 前置检查
log "[0/3] 环境检查"
command -v docker >/dev/null || { echo "未找到 docker" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "未找到 docker buildx" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker 未运行，请先启动 Docker Desktop" >&2; exit 1; }

if [[ "$DO_PUSH" == 1 ]]; then
  echo "    镜像：$IMAGE:$TAG 与 $IMAGE:latest"
  echo "    平台：${PLATFORM}（构建后推送）"
else
  echo "    镜像：$IMAGE:$TAG 与 $IMAGE:latest"
  echo "    平台：${PLATFORM}（仅本地构建，不推送）"
fi

# ---------------------------------------------------------------- 1) 前端构建
if [[ "$DO_FRONTEND" == 1 ]]; then
  log "[1/3] 前端构建（frontend -> gdb2pg/web/static）"
  command -v npm >/dev/null || { echo "未找到 npm，无法构建前端" >&2; exit 1; }
  pushd frontend >/dev/null
  if [[ ! -d node_modules ]]; then
    echo "    node_modules 不存在，先执行 npm ci"
    npm ci
  fi
  npm run build          # vue-tsc --noEmit && vite build
  popd >/dev/null
  [[ -f gdb2pg/web/static/index.html ]] \
    || { echo "前端构建产物缺少 gdb2pg/web/static/index.html" >&2; exit 1; }
  echo "    产物：gdb2pg/web/static/"
else
  log "[1/3] 跳过前端构建（--skip-frontend）"
  [[ -f gdb2pg/web/static/index.html ]] \
    || { echo "gdb2pg/web/static 无产物，不能跳过前端构建" >&2; exit 1; }
fi

# ------------------------------------------------- 2) buildx 构建（+ 可选推送）
log "[2/3] buildx 构建 ${PLATFORM} 镜像"
BUILD_ARGS=(
  buildx build
  --platform "$PLATFORM"
  -t "$IMAGE:$TAG"
  -t "$IMAGE:latest"
)
[[ "$DO_PUSH" == 1 ]] && BUILD_ARGS+=(--push)
# 宿主为 arm64（Apple Silicon）时交叉编译 amd64：走 buildx 容器驱动
[[ "$(uname -m)" != "x86_64" && "$DO_PUSH" == 0 ]] && BUILD_ARGS+=(--load)

docker "${BUILD_ARGS[@]}" .

if [[ "$DO_PUSH" == 1 ]]; then
  echo "    已推送：${IMAGE}:${TAG}（latest 同时更新）"
fi

# ------------------------------------------------------- 3) 清理本机历史版本
if [[ "$DO_CLEAN" == 1 ]]; then
  log "[3/3] 清理本机历史版本镜像"
  # 收集该仓库下「非本次 TAG / 非 latest」的镜像 ID
  # 注意：macOS 自带 bash 3.2 没有 mapfile，这里用 while read 保持可移植
  OLD_IDS=()
  while IFS= read -r id; do
    [[ -n "$id" ]] && OLD_IDS+=("$id")
  done < <(
    docker images --format '{{.ID}} {{.Repository}}:{{.Tag}}' \
      | awk -v repo="$IMAGE" -v tag="$TAG" \
        '$2 ~ ("^" repo ":") && $2 != repo ":latest" && $2 != repo ":" tag {print $1}' \
      | sort -u
  )
  if [[ "${#OLD_IDS[@]}" -eq 0 ]]; then
    echo "    无历史版本需要清理"
  elif [[ "$DO_PUSH" == 0 ]]; then
    echo "    跳过清理：本次未推送，删除会丢掉本机可用的 latest"
  else
    for id in "${OLD_IDS[@]}"; do
      docker image rm -f "$id" >/dev/null 2>&1 \
        && echo "    已删除 ${id}" \
        || echo "    跳过 ${id}（被容器占用或仍被其他标签引用）"
    done
  fi
  # 说明：只删历史镜像，不动构建缓存（缓存保留可加速后续构建）
  echo "    当前本机镜像："
  docker images --format '    {{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}' \
    | grep -F "$IMAGE" || echo "    （无）"
else
  log "[3/3] 跳过清理（--no-clean）"
fi

log "完成：$IMAGE:$TAG"
