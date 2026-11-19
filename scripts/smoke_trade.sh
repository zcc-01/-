#!/usr/bin/env bash
# 交易域冒烟：登录 → 检索 → 试算 → 下单 → 重复支付 → 发货 → 收货 → 评价
# 用法：bash scripts/smoke_trade.sh [base_url]
set -euo pipefail

BASE=${1:-http://127.0.0.1:8000}
PY=${PYTHON:-python3}
command -v "$PY" > /dev/null 2>&1 || PY=python

json() { $PY -c "import sys,json;d=json.load(sys.stdin);print($1)"; }

uid() { $PY -c "import uuid;print(uuid.uuid4().hex[:6].upper())"; }

echo "== 1. 登录（消费者 / 农户）"
CT=$(curl -sf -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"consumer01","password":"Passw0rd!"}' | json "d['data']['access_token']")
FT=$(curl -sf -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"farmer01","password":"Passw0rd!"}' | json "d['data']['access_token']")
CAUTH="Authorization: Bearer $CT"
FAUTH="Authorization: Bearer $FT"

echo "== 2. 检索在售商品"
TOTAL=$(curl -sf "$BASE/api/products?in_stock_only=true&sort=price_asc" | json "d['data']['total']")
echo "   可售商品 $TOTAL 件"
[ "$TOTAL" -ge 1 ] || { echo "!! 没有可售商品，请先执行 python -m app.db.seed"; exit 1; }

echo "== 3. 农户上架一个冒烟商品"
FARM=$(curl -sf "$BASE/api/products?mine=true" -H "$FAUTH" | json "d['data']['items'][0]['farm_id']")
PID=$(curl -sf -X POST "$BASE/api/products" -H "$FAUTH" -H 'Content-Type: application/json' \
  -d "{\"farm_id\":\"$FARM\",\"title\":\"冒烟测试商品-$(uid)\",\"crop_name\":\"测试作物\",\"spec\":\"1份\",\"unit\":\"份\",\"price\":50,\"stock\":10}" \
  | json "d['data']['product_id']")
echo "   product_id=$PID"

echo "== 4. 购物车试算（2 件，应满 99 免运费）"
curl -sf -X POST "$BASE/api/cart/quote" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"product_id\":\"$PID\",\"quantity\":2}]}" \
  | json "('goods=%s freight=%s pay=%s' % (d['data']['groups'][0]['goods_amount'], d['data']['groups'][0]['freight'], d['data']['pay_amount']))"

echo "== 5. 下单"
OID=$(curl -sf -X POST "$BASE/api/orders" -H "$CAUTH" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"product_id\":\"$PID\",\"quantity\":2}],\"address\":{\"receiver\":\"李小明\",\"phone\":\"13800000004\",\"detail\":\"浙江省杭州市临安区清凉峰镇 1 号\"}}" \
  | json "d['data']['orders'][0]['order_id']")
echo "   order_id=$OID"

echo "== 6. 支付"
PAY1=$(curl -sf -X POST "$BASE/api/orders/$OID/pay" -H "$CAUTH" -H 'Content-Type: application/json' -d '{"channel":"mockpay"}')
echo "   $(echo "$PAY1" | json "d['data']['status_label'] + ' trade_no=' + d['data']['trade_no']")"

echo "== 7. 重复支付（应幂等，流水号不变）"
PAY2=$(curl -sf -X POST "$BASE/api/orders/$OID/pay" -H "$CAUTH" -H 'Content-Type: application/json' -d '{"channel":"mockpay"}')
PN1=$(echo "$PAY1" | json "d['data']['payment_no']")
PN2=$(echo "$PAY2" | json "d['data']['payment_no']")
[ "$PN1" = "$PN2" ] && echo "   幂等 OK：$PN1" || { echo "!! 重复支付产生了新流水"; exit 1; }

echo "== 8. 农户发货"
curl -sf -X POST "$BASE/api/orders/$OID/ship" -H "$FAUTH" -H 'Content-Type: application/json' \
  -d '{"express_no":"SF7788000001"}' | json "'   ' + d['data']['status_label']"

echo "== 9. 消费者确认收货"
curl -sf -X POST "$BASE/api/orders/$OID/receive" -H "$CAUTH" | json "'   ' + d['data']['status_label']"

echo "== 10. 评价晒单"
curl -sf -X POST "$BASE/api/orders/$OID/reviews" -H "$CAUTH" -H 'Content-Type: application/json' \
  -d "{\"product_id\":\"$PID\",\"rating\":5,\"content\":\"冒烟测试：产地直供，扫码可查批次\"}" \
  | json "'   ' + d['data']['user_display'] + ' ' + '★' * d['data']['rating']"

echo "== 11. 库存核对（下单 2 件，应为 8）"
STOCK=$(curl -sf "$BASE/api/products/$PID" | json "d['data']['stock']")
echo "   stock=$STOCK"
[ "$STOCK" = "8" ] || { echo "!! 库存与预期不符"; exit 1; }

echo "== 交易域冒烟通过；页面地址： $BASE/shop"
