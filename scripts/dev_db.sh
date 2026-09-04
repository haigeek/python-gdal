#!/usr/bin/env bash
# 本地 PostGIS 开发/验证库：全部资源都在项目内（不污染系统）。
# 用途：没有远程可写库时，跑 gdb2pg 端到端自检。
# 用法：bash scripts/dev_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export HOME="$PWD/.home"
export MAMBA_ROOT_PREFIX="$PWD/.mamba"
PYBIN="$PWD/.conda/bin"
PORT="${PGPORT:-5433}"
DB="${PGDB:-gdb2pg_test}"
PGDATA="$PWD/pgdata"

echo "==> [1/4] 确保环境里有 postgresql + postgis"
if ! "$PYBIN"/postgres --version >/dev/null 2>&1; then
  .tools/bin/micromamba install -p ./.conda -c conda-forge postgresql postgis -y
fi
"$PYBIN"/postgres --version

echo "==> [2/4] initdb（若已存在则跳过）"
if [ ! -d "$PGDATA" ]; then
  "$PYBIN"/initdb -D "$PGDATA" -U postgres --encoding=UTF8 --no-locale -A trust >/dev/null
  echo "port = $PORT" >> "$PGDATA/postgresql.conf"
  echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
fi

echo "==> [3/4] 启动 postgres (port $PORT)"
if ! "$PYBIN"/pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  "$PYBIN"/pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" -o "-p $PORT" start >/dev/null
fi

echo "==> [4/4] 建库 + 启用 PostGIS"
"$PYBIN"/psql -h 127.0.0.1 -p "$PORT" -U postgres -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 \
  || "$PYBIN"/createdb -h 127.0.0.1 -p "$PORT" -U postgres "$DB"
"$PYBIN"/psql -h 127.0.0.1 -p "$PORT" -U postgres -d "$DB" -q \
  -c "CREATE EXTENSION IF NOT EXISTS postgis"

echo "==> 就绪: host=127.0.0.1 port=$PORT db=$DB user=postgres (trust 免密)"
"$PYBIN"/psql -h 127.0.0.1 -p "$PORT" -U postgres -d "$DB" -tAc "SELECT PostGIS_Version();"