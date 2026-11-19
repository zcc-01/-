#!/usr/bin/env bash
# 一键启动：装依赖 → 建库 → 灌种子 → 跑测试（可选）→ 起服务
# 用法：
#   bash scripts/bootstrap.sh                 # 完整流程并启动服务
#   bash scripts/bootstrap.sh --install-only  # 只装依赖（devcontainer postCreate 用）
#   bash scripts/bootstrap.sh --no-test       # 跳过测试
set -euo pipefail

cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
if ! command -v "$PY" > /dev/null 2>&1; then
  PY=python
fi

echo "==> Python: $($PY --version)"

echo "==> 安装依赖"
$PY -m pip install --upgrade pip > /dev/null
$PY -m pip install -r requirements.txt
if [ "${1:-}" = "--install-only" ]; then
  $PY -m pip install -r requirements-dev.txt
  echo "==> 依赖安装完成（--install-only，跳过启动）"
  exit 0
fi
$PY -m pip install -r requirements-dev.txt

echo "==> 初始化数据库与种子数据"
$PY -m app.db.seed

if [ "${1:-}" != "--no-test" ]; then
  echo "==> 静态检查"
  $PY -m ruff check .
  echo "==> 单元测试"
  $PY -m pytest -q
fi

echo "==> 启动服务： http://127.0.0.1:8000 （接口文档 /docs）"
$PY -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
