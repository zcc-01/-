/* 交易域前端（UC-06～UC-10）
 *
 * 设计取舍：页面由服务端渲染骨架，数据一律走 /api 接口拉取。
 * 这样做的好处是「页面看到的金额」和「接口返回的金额」永远同源，
 * 不存在模板里再算一遍运费导致金额不一致的问题。
 *
 * 购物车放在 localStorage：报告 4.5 的 13 张表里没有 cart 表，
 * 购物车是纯前端状态，下单时才把 {product_id, quantity} 提交给服务端重算金额。
 */

const API = "/api";
const TOKEN_KEY = "agri_token";
const USER_KEY = "agri_user";
const CART_KEY = "agri_cart";

/* ------------------------------------------------------------------ 基础工具 */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const money = (n) => "¥" + Number(n || 0).toFixed(2);

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }
function currentUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); } catch { return null; }
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  if (!el) { alert(message); return; }
  el.textContent = message;
  el.className = "toast on" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

async function api(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && token()) headers.Authorization = "Bearer " + token();
  const resp = await fetch(API + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try { payload = await resp.json(); } catch { /* 空响应 */ }
  if (!resp.ok || !payload || payload.code !== 0) {
    const message = (payload && payload.message) || `请求失败（HTTP ${resp.status}）`;
    const error = new Error(message);
    error.status = resp.status;
    error.data = payload && payload.data;
    if (resp.status === 401) { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); renderWho(); }
    throw error;
  }
  return payload.data;
}

/* ------------------------------------------------------------------ 登录状态 */
function renderWho() {
  const user = currentUser();
  const text = $("#who-text");
  if (!text) return;
  text.textContent = user ? `${user.display_name || user.username}（${roleLabel(user.role)}）` : "未登录";
  $("#btn-login").hidden = !!user;
  $("#btn-logout").hidden = !user;
  const salesTab = $("#tab-sales");
  if (salesTab) salesTab.hidden = !(user && ["farmer", "operator"].includes(user.role));
}

function roleLabel(role) {
  return { farmer: "农户", consumer: "消费者", operator: "平台运营", regulator: "监管" }[role] || role;
}

function bindLogin() {
  const box = $("#login-box");
  const btn = $("#btn-login");
  if (btn) btn.addEventListener("click", () => box.classList.toggle("on"));
  if (box) {
    box.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const data = await api("/auth/login", {
          method: "POST",
          auth: false,
          body: { username: $("#login-username").value.trim(), password: $("#login-password").value },
        });
        localStorage.setItem(TOKEN_KEY, data.access_token);
        localStorage.setItem(USER_KEY, JSON.stringify(data.user));
        box.classList.remove("on");
        toast(`已登录：${data.user.display_name || data.user.username}`);
        renderWho();
        document.dispatchEvent(new CustomEvent("auth:changed"));
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
  const logout = $("#btn-logout");
  if (logout) {
    logout.addEventListener("click", () => {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      renderWho();
      toast("已退出登录");
      document.dispatchEvent(new CustomEvent("auth:changed"));
    });
  }
}

/* ------------------------------------------------------------------ 购物车状态 */
function cart() {
  try { return JSON.parse(localStorage.getItem(CART_KEY) || "[]"); } catch { return []; }
}
function saveCart(items) {
  localStorage.setItem(CART_KEY, JSON.stringify(items));
  renderCartBadge();
}
function cartCount() {
  return cart().reduce((sum, item) => sum + Number(item.quantity || 0), 0);
}
function renderCartBadge() {
  const badge = $("#cart-badge");
  if (badge) badge.textContent = cartCount();
}
function addToCart(product, quantity = 1) {
  const items = cart();
  const found = items.find((item) => item.product_id === product.product_id);
  const stock = Number(product.stock ?? 0);
  if (found) {
    found.quantity = Math.min(found.quantity + quantity, stock);
    found.stock = stock;
    found.price = product.price;
  } else {
    items.push({
      product_id: product.product_id,
      title: product.title,
      spec: product.spec,
      unit: product.unit,
      price: product.price,
      stock,
      quantity,
    });
  }
  saveCart(items);
  toast(`已加入购物车：${product.title}`);
}

/* ------------------------------------------------------------------ 商城列表（UC-07） */
const shopState = { page: 1, size: 12 };

function productCard(item) {
  const tags = [
    item.cert_badge ? `<span class="tag">${esc(item.cert_badge)}</span>` : "",
    item.batch_id ? '<span class="tag grey">可溯源</span>' : '<span class="tag grey">普通农产品</span>',
    item.stock > 0 ? "" : '<span class="tag amber">已售罄</span>',
  ].join("");
  const stars = item.review_count
    ? `<span class="stars">${"★".repeat(Math.round(item.rating_avg))}${"☆".repeat(5 - Math.round(item.rating_avg))}</span>
       <span>${item.rating_avg.toFixed(1)}（${item.review_count}）</span>`
    : '<span>暂无评价</span>';
  return `
    <article class="pcard">
      <div class="pmeta">${tags}</div>
      <h3><a href="/shop/${esc(item.product_id)}">${esc(item.title)}</a></h3>
      <div class="pmeta">
        <span>${esc(item.farm_name)}</span><span>·</span><span>${esc(item.province)}${esc(item.city)}</span>
      </div>
      <div class="pmeta">${stars}</div>
      <div class="price">${money(item.price)} <small>/ ${esc(item.spec || item.unit)}</small></div>
      <div class="pmeta"><span>库存 ${item.stock}</span><span>·</span><span>已售 ${item.sales}</span></div>
      <div class="btn-row">
        <button class="btn sm" data-add="${esc(item.product_id)}" ${item.can_buy ? "" : "disabled"}>
          ${item.can_buy ? "加入购物车" : "暂时无货"}
        </button>
        <a class="btn sm ghost" href="/shop/${esc(item.product_id)}">查看详情</a>
      </div>
    </article>`;
}

async function loadProducts(page = 1) {
  const grid = $("#grid");
  if (!grid) return;
  shopState.page = page;
  const params = new URLSearchParams({ page: String(page), size: String(shopState.size) });
  const keyword = $("#f-keyword").value.trim();
  if (keyword) params.set("keyword", keyword);
  const min = $("#f-min").value;
  const max = $("#f-max").value;
  if (min) params.set("min_price", min);
  if (max) params.set("max_price", max);
  if ($("#f-sort").value) params.set("sort", $("#f-sort").value);
  if ($("#f-cert").checked) params.set("cert_only", "true");
  if ($("#f-stock").checked) params.set("in_stock_only", "true");

  grid.innerHTML = '<p class="muted">加载中…</p>';
  try {
    const data = await api("/products?" + params.toString(), { auth: false });
    $("#result-count").textContent = `共 ${data.total} 件商品`;
    grid.innerHTML = data.items.length
      ? data.items.map(productCard).join("")
      : '<p class="muted">没有匹配的商品，试试调整筛选条件。</p>';
    renderPager(data);
  } catch (err) {
    grid.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function renderPager(data) {
  const pager = $("#pager");
  if (!pager) return;
  const pages = Math.max(1, Math.ceil(data.total / data.size));
  pager.innerHTML = `
    <button class="btn sm ghost" id="prev" ${data.page <= 1 ? "disabled" : ""}>上一页</button>
    <span class="muted">第 ${data.page} / ${pages} 页</span>
    <button class="btn sm ghost" id="next" ${data.page >= pages ? "disabled" : ""}>下一页</button>`;
  $("#prev")?.addEventListener("click", () => loadProducts(data.page - 1));
  $("#next")?.addEventListener("click", () => loadProducts(data.page + 1));
}

function bindShop() {
  $("#filter-form").addEventListener("submit", (event) => { event.preventDefault(); loadProducts(1); });
  $("#f-reset").addEventListener("click", () => {
    ["#f-keyword", "#f-min", "#f-max"].forEach((sel) => { $(sel).value = ""; });
    $("#f-cert").checked = false;
    $("#f-stock").checked = false;
    $("#f-sort").value = "newest";
    loadProducts(1);
  });
  $("#grid").addEventListener("click", async (event) => {
    const btn = event.target.closest("[data-add]");
    if (!btn) return;
    try {
      const product = await api(`/products/${btn.dataset.add}`, { auth: false });
      if (!product.can_buy) { toast("该商品暂时无货", true); return; }
      addToCart(product, 1);
    } catch (err) { toast(err.message, true); }
  });
}

/* ------------------------------------------------------------------ 商品详情（UC-07/UC-10） */
async function loadProduct() {
  const id = document.body.dataset.productId;
  const card = $("#detail-card");
  try {
    const item = await api(`/products/${id}`, { auth: false });
    const batch = item.batch
      ? `<div class="kvrow"><span class="label">生产批次</span><span>${esc(item.batch.batch_no)}</span></div>
         <div class="kvrow"><span class="label">地块 / 采收</span><span>${esc(item.batch.plot_no)} / ${esc(item.batch.harvest_date)}</span></div>
         <div class="kvrow"><span class="label">溯源环节</span><span>${item.batch.event_count} 条已上链</span></div>
         <div class="kvrow"><span class="label">批次状态</span><span>${esc(item.batch.status)}</span></div>`
      : '<div class="kvrow"><span class="label">溯源批次</span><span>未关联批次</span></div>';
    card.innerHTML = `
      <div class="detail">
        <div class="detail-main">
          <div class="pmeta">
            ${item.cert_badge ? `<span class="tag">${esc(item.cert_badge)}</span>` : ""}
            <span class="tag grey">${esc(item.crop_name)}</span>
          </div>
          <h1 style="margin-top:8px">${esc(item.title)}</h1>
          <p class="muted">${esc(item.description || "产地直供，当季采收。")}</p>
          <div class="price" style="font-size:24px">${money(item.price)} <small>/ ${esc(item.spec || item.unit)}</small></div>
          <div class="pmeta" style="margin-top:6px">
            <span>库存 ${item.stock} ${esc(item.unit)}</span><span>·</span><span>已售 ${item.sales}</span>
          </div>
          <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap">
            <div class="qty">
              <button type="button" id="q-minus">−</button>
              <input id="q-value" value="1" readonly />
              <button type="button" id="q-plus">＋</button>
            </div>
            <button class="btn" id="btn-add" ${item.can_buy ? "" : "disabled"} style="margin-top:0">
              ${item.can_buy ? "加入购物车" : "暂时无货"}
            </button>
            <button class="btn ghost" id="btn-buy" ${item.can_buy ? "" : "disabled"} style="margin-top:0">立即购买</button>
          </div>
        </div>
        <aside class="detail-side">
          <h2>产地与溯源</h2>
          <div class="kvrow"><span class="label">供货产地</span><span>${esc(item.farm_name)}</span></div>
          <div class="kvrow"><span class="label">产地位置</span><span>${esc(item.province)}${esc(item.city)}</span></div>
          ${batch}
          <p class="hint" style="margin-top:10px">溯源记录由溯源链域维护，扫码或访问 /trace/&lt;码值&gt; 可查看完整时间轴与链完整性校验结论。</p>
        </aside>
      </div>`;

    const clamp = (n) => Math.max(1, Math.min(n, item.stock || 1));
    $("#q-minus").addEventListener("click", () => { $("#q-value").value = clamp(Number($("#q-value").value) - 1); });
    $("#q-plus").addEventListener("click", () => { $("#q-value").value = clamp(Number($("#q-value").value) + 1); });
    $("#btn-add").addEventListener("click", () => addToCart(item, Number($("#q-value").value)));
    $("#btn-buy").addEventListener("click", () => {
      addToCart(item, Number($("#q-value").value));
      location.href = "/cart";
    });
    loadReviews(id);
  } catch (err) {
    card.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

async function loadReviews(productId) {
  const box = $("#reviews");
  try {
    const data = await api(`/products/${productId}/reviews`, { auth: false });
    box.innerHTML = data.items.length
      ? data.items.map((item) => `
        <div style="border-bottom:1px dashed var(--line);padding:10px 0">
          <div class="pmeta">
            <span class="stars">${"★".repeat(item.rating)}${"☆".repeat(5 - item.rating)}</span>
            <span>${esc(item.user_display)}</span>
            <span>${esc((item.created_at || "").replace("T", " ").slice(0, 16))}</span>
          </div>
          <p style="margin:6px 0">${esc(item.content || "买家未填写文字评价")}</p>
        </div>`).join("")
      : '<p class="muted">还没有评价，确认收货后即可晒单。</p>';
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

/* ------------------------------------------------------------------ 购物车（UC-08） */
function cartLines() {
  return cart().map((item) => ({ product_id: item.product_id, quantity: item.quantity }));
}

async function renderCart() {
  const body = $("#cart-body");
  const items = cart();
  if (!items.length) {
    body.innerHTML = '<p class="muted">购物车是空的，去 <a href="/shop">产地直供</a> 挑点东西吧。</p>';
    $("#checkout-card").hidden = true;
    return;
  }
  $("#checkout-card").hidden = false;
  body.innerHTML = `
    <table class="table">
      <thead><tr><th>商品</th><th>单价</th><th>数量</th><th class="num">小计</th><th></th></tr></thead>
      <tbody>
        ${items.map((item) => `
          <tr>
            <td><a href="/shop/${esc(item.product_id)}" style="color:var(--text)">${esc(item.title)}</a>
              <div class="hint">${esc(item.spec || "")}</div></td>
            <td>${money(item.price)}</td>
            <td>
              <div class="qty">
                <button type="button" data-dec="${esc(item.product_id)}">−</button>
                <input value="${item.quantity}" readonly style="width:40px" />
                <button type="button" data-inc="${esc(item.product_id)}">＋</button>
              </div>
            </td>
            <td class="num">${money(item.price * item.quantity)}</td>
            <td><button class="btn sm ghost" data-del="${esc(item.product_id)}">移除</button></td>
          </tr>`).join("")}
      </tbody>
    </table>
    <div id="quote-box" class="hint" style="margin-top:12px">正在试算金额…</div>`;

  refreshQuote();
}

function handleCartClick(event) {
  const dec = event.target.closest("[data-dec]");
  const inc = event.target.closest("[data-inc]");
  const del = event.target.closest("[data-del]");
  if (!dec && !inc && !del) return;
  const node = dec || inc || del;
  const id = node.dataset.dec || node.dataset.inc || node.dataset.del;
  const next = cart()
    .map((item) => {
      if (item.product_id !== id) return item;
      if (del) return null;
      const limit = dec ? Math.max(0, item.quantity - 1) : Math.min(item.quantity + 1, item.stock || 99);
      return { ...item, quantity: limit };
    })
    .filter((item) => item && item.quantity > 0);
  saveCart(next);
  renderCart();
}

async function refreshQuote() {
  const box = $("#quote-box");
  if (!box) return;
  if (!cart().length) return;
  try {
    const data = await api("/cart/quote", { method: "POST", auth: false, body: { items: cartLines() } });
    box.innerHTML = data.groups.map((group) => `
      <div style="margin-bottom:8px">
        <b>${esc(group.farm_name || "产地")}</b>
        <span class="muted">商品 ${money(group.goods_amount)} + 运费 ${money(group.freight)} = ${money(group.pay_amount)}</span>
        ${group.available ? "" : `<div class="tag amber" style="margin-left:6px">${esc(group.reason)}</div>`}
      </div>`).join("") +
      `<div style="margin-top:6px;font-size:15px">合计应付 <b class="price">${money(data.pay_amount)}</b>
       <span class="hint">（满 ${$("body").dataset.freeFreight} 元包邮）</span></div>`;
  } catch (err) {
    box.innerHTML = `<span class="muted">${esc(err.message)}</span>`;
  }
}

function bindCart() {
  renderCart();
  $("#cart-body").addEventListener("click", handleCartClick);
  $("#checkout-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!token()) { toast("请先登录（consumer01）", true); $("#login-box")?.classList.add("on"); return; }
    const user = currentUser();
    if (user && user.role !== "consumer") { toast("只有消费者账号可以下单，请用 consumer01 登录", true); return; }
    const body = {
      items: cartLines(),
      address: {
        receiver: $("#a-receiver").value.trim(),
        phone: $("#a-phone").value.trim(),
        detail: $("#a-detail").value.trim(),
      },
      remark: $("#a-remark").value.trim(),
    };
    if (!body.items.length) { toast("购物车是空的", true); return; }
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const data = await api("/orders", { method: "POST", body });
      saveCart([]);
      toast(`下单成功，共 ${data.total} 笔订单，请尽快支付`);
      setTimeout(() => { location.href = "/orders"; }, 800);
    } catch (err) {
      toast(err.message, true);
      button.disabled = false;
      refreshQuote();
    }
  });
}

/* ------------------------------------------------------------------ 订单中心（UC-08/09/10） */
const orderState = { scope: "mine", status: "" };

function statusPill(order) {
  return `<span class="status-pill ${esc(order.status)}">${esc(order.status_label)}</span>`;
}

function orderCard(order, scope) {
  const items = order.items.map((item) => `
    <tr>
      <td>${esc(item.product_title)}<div class="hint">${esc(item.spec || "")}</div></td>
      <td>${money(item.unit_price)} × ${item.quantity}</td>
      <td class="num">${money(item.subtotal)}</td>
      <td>${order.can_review ? `<button class="btn sm ghost" data-review="${esc(order.order_id)}|${esc(item.product_id)}">评价</button>` : ""}</td>
    </tr>`).join("");

  const actions = [];
  if (scope === "mine") {
    if (order.can_pay) actions.push(`<button class="btn sm" data-pay="${esc(order.order_id)}">立即支付</button>`);
    if (order.can_cancel) actions.push(`<button class="btn sm ghost" data-cancel="${esc(order.order_id)}">取消订单</button>`);
    if (order.can_receive) actions.push(`<button class="btn sm" data-receive="${esc(order.order_id)}">确认收货</button>`);
  } else {
    if (order.can_ship) actions.push(`<button class="btn sm" data-ship="${esc(order.order_id)}">发货</button>`);
  }

  const meta = [
    `订单号 ${esc(order.order_no)}`,
    esc((order.created_at || "").replace("T", " ").slice(0, 16)),
    order.express_no ? `运单号 ${esc(order.express_no)}` : "",
  ].filter(Boolean).join(" · ");

  return `
    <div class="order-card" data-order="${esc(order.order_id)}">
      <div class="order-head">
        <span>${meta}</span>
        ${statusPill(order)}
      </div>
      <table class="table" style="margin-top:8px">
        <tbody>${items}</tbody>
      </table>
      <div class="order-head" style="margin-top:8px">
        <span>${esc(order.address_snapshot)}</span>
        <span>商品 ${money(order.total_amount)} + 运费 ${money(order.freight)} = <b class="price">${money(order.pay_amount)}</b></span>
      </div>
      ${order.cancel_reason ? `<p class="hint">取消原因：${esc(order.cancel_reason)}</p>` : ""}
      ${actions.length ? `<div class="btn-row">${actions.join("")}</div>` : ""}
      <div data-review-box="${esc(order.order_id)}"></div>
    </div>`;
}

async function loadOrders() {
  const box = $("#order-list");
  box.innerHTML = '<p class="muted">加载中…</p>';
  if (!token()) {
    box.innerHTML = '<p class="muted">请先登录后查看订单。演示账号 consumer01 / Passw0rd!</p>';
    return;
  }
  const params = new URLSearchParams({ scope: orderState.scope, page: "1", size: "20" });
  if (orderState.status) params.set("status", orderState.status);
  try {
    const data = await api("/orders?" + params.toString());
    box.innerHTML = data.items.length
      ? data.items.map((order) => orderCard(order, orderState.scope)).join("")
      : '<p class="muted">还没有订单。</p>';
  } catch (err) {
    box.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function reviewForm(orderId, productId) {
  const box = $(`[data-review-box="${orderId}"]`);
  box.innerHTML = `
    <form class="toolbar" style="margin-top:10px;background:var(--green-soft);padding:10px;border-radius:10px">
      <label class="field">评分
        <select data-rating>
          <option value="5">★★★★★ 非常满意</option>
          <option value="4">★★★★ 满意</option>
          <option value="3">★★★ 一般</option>
          <option value="2">★★ 不满意</option>
          <option value="1">★ 很不满意</option>
        </select>
      </label>
      <label class="field grow">评价内容
        <input data-content placeholder="产地直供的感受、包装、口感…" maxlength="500" />
      </label>
      <button class="btn sm" type="submit" data-submit="${orderId}|${productId}">提交评价</button>
    </form>`;
  box.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/orders/${orderId}/reviews`, {
        method: "POST",
        body: {
          product_id: productId,
          rating: Number(box.querySelector("[data-rating]").value),
          content: box.querySelector("[data-content]").value.trim(),
        },
      });
      toast("评价已提交，感谢晒单");
      loadOrders();
    } catch (err) { toast(err.message, true); }
  });
}

function bindOrders() {
  renderWho();
  loadOrders();

  $("#scope-tabs").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-scope]");
    if (!btn) return;
    $$("#scope-tabs button").forEach((item) => item.classList.toggle("on", item === btn));
    orderState.scope = btn.dataset.scope;
    loadOrders();
  });

  $("#status-tabs").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-status]");
    if (!btn) return;
    $$("#status-tabs button").forEach((item) => item.classList.toggle("on", item === btn));
    orderState.status = btn.dataset.status;
    loadOrders();
  });

  $("#order-list").addEventListener("click", async (event) => {
    const target = event.target.closest("button");
    if (!target) return;
    const { pay, cancel, receive, ship, review } = target.dataset;

    try {
      if (pay) {
        target.disabled = true;
        const data = await api(`/orders/${pay}/pay`, { method: "POST", body: { channel: "mockpay" } });
        toast(data.message || "支付成功");
        loadOrders();
      } else if (cancel) {
        if (!confirm("确认取消该订单？库存会立即释放。")) return;
        await api(`/orders/${cancel}/cancel`, { method: "POST", body: { reason: "买家主动取消" } });
        toast("订单已取消");
        loadOrders();
      } else if (receive) {
        await api(`/orders/${receive}/receive`, { method: "POST" });
        toast("已确认收货，可以评价了");
        loadOrders();
      } else if (ship) {
        const expressNo = prompt("请输入物流单号（如 SF7788123456）", "SF" + Date.now());
        if (!expressNo) return;
        await api(`/orders/${ship}/ship`, { method: "POST", body: { express_no: expressNo } });
        toast("已发货");
        loadOrders();
      } else if (review) {
        const [orderId, productId] = review.split("|");
        reviewForm(orderId, productId);
      }
    } catch (err) {
      toast(err.message, true);
      if (pay) target.disabled = false;
    }
  });

  document.addEventListener("auth:changed", loadOrders);
}

/* ------------------------------------------------------------------ 启动 */
document.addEventListener("DOMContentLoaded", () => {
  renderCartBadge();
  renderWho();
  bindLogin();
  const page = document.body.dataset.page;
  if (page === "shop") bindShop(), loadProducts(1);
  if (page === "product") loadProduct();
  if (page === "cart") bindCart();
  if (page === "orders") bindOrders();
});
