#!/usr/bin/env bash
# 本地 Web 开发/自检：
#  1) 起本地 Postgres（5433，复用 scripts/dev_db.sh 的库初始化）
#  2) 建业务库 g2p_meta（任务元数据）
#  3) 启动 Web 服务（python -m gdb2pg web --config configs/web.dev.json）
# 前端两种用法：
#  - 已构建：打开 http://127.0.0.1:8000
#  - 开发模式：另开终端 cd frontend && npm run dev（访问 http://localhost:5173）
# 用法：bash scripts/dev_web.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export HOME="$PWD/.home"
export MAMBA_ROOT_PREFIX="$PWD/.mamba"
PYBIN="$PWD/.conda/bin"
PORT="${PGPORT:-5433}"
META_DB="${G2P_META_DB:-g2p_meta}"
CFG="${G2P_WEB_CFG:-configs/web.dev.json}"

echo "==> [1/3] 确保本地 Postgres+PostGIS（5433）就绪"
bash scripts/dev_db.sh

echo "==> [2/3] 确保业务库 $META_DB 存在"
"$PYBIN"/psql -h 127.0.0.1 -p "$PORT" -U postgres -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$META_DB'" | grep -q 1 \
  || "$PYBIN"/createdb -h 127.0.0.1 -p "$PORT" -U postgres "$META_DB"
"$PYBIN"/psql -h 127.0.0.1 -p "$PORT" -U postgres -d "$META_DB" -q \
  -c "SELECT 1" >/dev/null

echo "==> [3/3] 启动 Web（$CFG）"
exec "$PYBIN"/python -m gdb2pg web --config "$CFG"