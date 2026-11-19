#!/usr/bin/env bash
# 一键启动：安装依赖 → 建库 + 种子数据 → 启动服务
# 幂等：重复执行不会重复装包、不会重复写种子数据。
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

echo "==> 1/4 检查 Python"
"$PYTHON" -V

echo "==> 2/4 安装依赖"
if [ ! -d ".venv" ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements-dev.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "    已根据 .env.example 生成 .env"
fi

echo "==> 3/4 初始化数据库与种子数据"
mkdir -p data
"$PYTHON" -m app.db.seed_cli

echo "==> 4/4 启动服务 http://127.0.0.1:8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
