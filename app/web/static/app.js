// 前端公共方法：令牌存取、统一请求、轻提示。
// 只做三件事，不引入框架 —— 课程作业的页面逻辑量不足以支撑一个构建链。

const TOKEN_KEY = "agri_token";
const USER_KEY = "agri_user";

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

function setSession(data) {
  localStorage.setItem(TOKEN_KEY, data.access_token);
  localStorage.setItem(USER_KEY, JSON.stringify(data.user));
}

function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const token = getToken();
  if (token) headers.Authorization = "Bearer " + token;

  const resp = await fetch(path, Object.assign({}, options, { headers }));
  const contentType = resp.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const text = await resp.text();
    throw new Error("接口返回非 JSON：" + text.slice(0, 120));
  }
  const body = await resp.json();
  if (!resp.ok) {
    const err = new Error(body.message || "请求失败");
    err.code = body.code;
    err.data = body.data;
    err.status = resp.status;
    if (resp.status === 401) clearSession();
    throw err;
  }
  return body.data;
}

let toastTimer = null;
function toast(message, isError = false) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 3200);
}

function renderWho() {
  const el = document.getElementById("who");
  if (!el) return;
  const user = getUser();
  if (!user) {
    el.innerHTML = '<a href="/login">登录</a>';
    return;
  }
  el.innerHTML = `${user.display_name}（${user.role_label}）<a href="#" id="logout" style="margin-left:10px">退出</a>`;
  document.getElementById("logout").onclick = (event) => {
    event.preventDefault();
    clearSession();
    location.href = "/login";
  };
}

function guard() {
  if (!getToken()) {
    location.href = "/login";
    return false;
  }
  return true;
}

document.addEventListener("DOMContentLoaded", renderWho);
