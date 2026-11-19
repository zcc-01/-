#!/usr/bin/env bash
# 冒烟脚本：用 curl 走一遍「登录 → 建档 → 录入环节 → 赋码 → 扫码 → 校验」
# 用法：bash scripts/smoke.sh [base_url]
set -euo pipefail

BASE=${1:-http://127.0.0.1:8000}
PY=${PYTHON:-python3}
command -v "$PY" > /dev/null 2>&1 || PY=python

json() { $PY -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

echo "== 1. 登录"
TOKEN=$(curl -sf -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"farmer01","password":"Passw0rd!"}' | json "d['data']['access_token']")
AUTH="Authorization: Bearer $TOKEN"
echo "   token 前 16 位：${TOKEN:0:16}..."

echo "== 2. 取产地"
FARM=$(curl -sf "$BASE/api/batches" -H "$AUTH" | json "d['data']['items'][0]['farm_id']")
echo "   farm_id=$FARM"

echo "== 3. 建档"
BID=$(curl -sf -X POST "$BASE/api/batches" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"farm_id\":\"$FARM\",\"crop_name\":\"冒烟测试茶-$RANDOM\",\"plot_no\":\"S-$RANDOM\",\"plant_date\":\"2026-03-01\",\"harvest_date\":\"2026-10-01\",\"expected_yield\":100}" \
  | json "d['data']['batch_id']")
echo "   batch_id=$BID"

echo "== 4. 状态流转 → 已采收"
curl -sf -X PATCH "$BASE/api/batches/$BID/status" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"status":"harvested"}' > /dev/null

echo "== 5. 录入质检环节"
curl -sf -X POST "$BASE/api/batches/$BID/events" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"stage":"quality","location":"县农检中心","detail":{"report_no":"SMOKE-001","conclusion":"合格"}}' > /dev/null

echo "== 6. 赋码 3 个"
CODE=$(curl -sf -X POST "$BASE/api/batches/$BID/codes" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"code_type":"item","count":3}' | json "d['data']['items'][0]['code_value']")
echo "   code=$CODE"

echo "== 7. 匿名扫码"
curl -sf "$BASE/api/trace/$CODE" | json "('chain_ok=%s events=%s' % (d['data']['chain']['ok'], len(d['data']['events'])))"

echo "== 8. 运营校验整链"
OP=$(curl -sf -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"operator01","password":"Passw0rd!"}' | json "d['data']['access_token']")
curl -sf "$BASE/api/batches/$BID/verify" -H "Authorization: Bearer $OP" | json "d['data']"

echo "== 冒烟通过；页面地址： $BASE/trace/$CODE"
