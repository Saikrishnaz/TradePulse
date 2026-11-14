// TradePulse dashboard - no build step, no framework. Data arrives over REST on navigation and over
// Server-Sent Events while open; there is no frontend polling.

/* ============================== utilities ============================== */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { me: null, meta: null, route: "", live: "idle", es: null, cleanup: null, clients: null };

const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7l10-5z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  trending: '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  folder: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  sparkles: '<path d="M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/><path d="M19 3v4M21 5h-4"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  alert: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4M12 17h.01"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<path d="m6 3 14 9-14 9V3z"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  up: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  down: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  bot: '<rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4M8 16h.01M16 16h.01"/>',
};
const icon = (n, s = 16) => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n] || ""}</svg>`;

async function api(method, url, body) {
  const opt = { method, headers: { "X-TradePulse": "1" }, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (r.status === 401 && !url.startsWith("/api/auth/")) { state.me = null; renderLogin(); throw new Error("Signed out"); }
  let data = null;
  const ct = r.headers.get("content-type") || "";
  data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let msg = data && data.detail ? data.detail : `Request failed (${r.status})`;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}
const GET = (u) => api("GET", u);
const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

const nf2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("en-IN");
const n2 = (v) => (v == null || v === "" ? "–" : nf2.format(+v));
const n0 = (v) => (v == null || v === "" ? "–" : nf0.format(+v));
function inr(v, html = true) {
  if (v == null) return '<span class="muted">n/a</span>';
  const f = +v; const s = f > 0 ? "+" : f < 0 ? "−" : "";
  const t = `${s}₹${nf2.format(Math.abs(f))}`;
  if (!html) return t;
  const cls = f > 0 ? "up" : f < 0 ? "down" : "muted";
  const ar = f > 0 ? icon("up", 11) : f < 0 ? icon("down", 11) : "";
  return `<span class="${cls} num">${ar}${t}</span>`;
}
const tm = (iso) => (iso ? String(iso).slice(11, 19) : "–");
const dt = (iso) => (iso ? `${String(iso).slice(0, 10)} ${String(iso).slice(11, 19)}` : "–");
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(s)) return iso;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const BROKERS = { zerodha: "Zerodha", angel: "Angel One", "5paisa": "5paisa", paper: "Paper" };
const broker = (b) => BROKERS[(b || "").toLowerCase()] || b || "–";
const side = (s) => { const u = (s || "").toUpperCase(); return u ? `<span class="${u === "BUY" || u === "LONG" ? "side-buy" : "side-sell"}">${esc(u)}</span>` : "–"; };
const STATUS = {
  COMPLETE: "up", FILLED: "up", EXECUTED: "up", OPENED: "info", OPEN: "info", PARTIALLY_FILLED: "warn", REJECTED: "down", FAILED: "down",
  CANCELLED: "", EXPIRED: "", UNKNOWN: "warn", ROUTED: "violet", SUBMITTING: "violet", CLOSED: "", INCREASED: "info", REDUCED: "warn",
  SENT: "up", QUEUED: "info", DEAD: "down", SKIPPED: "", ACTIVE: "up", PENDING: "warn", ERROR: "down", REMOVED: "", DISABLED: "",
  OK: "up", ACCEPTED: "up", IGNORED: "", RATE_LIMITED: "warn", READY: "up", RUNNING: "up", STOPPED: "",
};
const badge = (s, label) => `<span class="badge ${STATUS[(s || "").toUpperCase()] ?? ""}"><span class="dot"></span>${esc(label ?? (s || "–").replace(/_/g, " "))}</span>`;
const TYPE = {
  ORDER_PLACED: ["Placed", "violet"], ORDER_OPEN: ["Open", "info"], ORDER_MODIFIED: ["Modified", "info"], ORDER_PARTIALLY_FILLED: ["Partial fill", "warn"],
  ORDER_FILLED: ["Filled", "up"], TRADE_EXECUTED: ["Trade", "up"], ORDER_CANCELLED: ["Cancelled", ""], ORDER_REJECTED: ["Rejected", "down"],
  ORDER_UNKNOWN: ["Unknown", "warn"], POSITION_UPDATED: ["Position", "accent"], HOLDING_UPDATED: ["Holding", "accent"],
  RECONCILE_MISMATCH: ["Mismatch", "down"], ACCOUNT_ALERT: ["Alert", "down"],
};
const typeBadge = (t) => { const [l, c] = TYPE[t] || [t, ""]; return `<span class="badge ${c}">${esc(l)}</span>`; };
const modeBadge = (m) => (m && m !== "LIVE" ? `<span class="badge ${m === "PAPER" ? "violet" : ""}">${esc(m)}</span>` : "");
const isAdmin = () => state.me && state.me.role === "ADMIN";
const empty = (title, sub = "", ic = "inbox") => `<div class="empty">${icon(ic, 28)}<b>${esc(title)}</b><span>${sub}</span></div>`;
const skeleton = (n = 4) => `<div class="card-b stack">${Array.from({ length: n }, () => '<div class="skeleton"></div>').join("")}</div>`;

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3500);
}
async function busy(btn, fn) {
  if (btn) { btn.setAttribute("aria-busy", "true"); btn.disabled = true; }
  try { return await fn(); } catch (e) { toast(e.message, "error"); throw e; }
  finally { if (btn) { btn.removeAttribute("aria-busy"); btn.disabled = false; } }
}
function copy(text) { navigator.clipboard?.writeText(text).then(() => toast("Copied", "ok"), () => toast("Copy failed", "error")); }

/* modal & drawer */
function openModal({ title, body, foot = "", wide = false, drawer = false, onClose }) {
  const ov = document.createElement("div");
  ov.className = `overlay${drawer ? " drawer-overlay" : ""}`;
  ov.innerHTML = `<div class="${drawer ? "drawer" : `modal${wide ? " wide" : ""}`}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-h"><h2>${esc(title)}</h2><button class="btn ghost icon x" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="modal-b">${body}</div>${foot ? `<div class="modal-f">${foot}</div>` : ""}</div>`;
  const prev = document.activeElement;
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(); prev && prev.focus && prev.focus(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  ov.addEventListener("click", (e) => { if (e.target === ov || e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(ov);
  (ov.querySelector("input,select,textarea,button:not([data-close])") || ov.querySelector("[data-close]")).focus();
  return { el: ov, close };
}
function confirmDialog(title, text, okLabel = "Confirm", danger = false) {
  return new Promise((res) => {
    const m = openModal({ title, body: `<p style="margin:0">${text}</p>`,
      foot: `<button class="btn" data-close>Cancel</button><button class="btn ${danger ? "danger" : "primary"}" data-ok>${esc(okLabel)}</button>`,
      onClose: () => res(false) });
    m.el.querySelector("[data-ok]").addEventListener("click", () => { res(true); m.el.remove(); });
  });
}

/* tooltip for charts */
let tipEl = null;
function tip(e, html) {
  if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tooltip"; document.body.appendChild(tipEl); }
  tipEl.innerHTML = html; tipEl.style.display = "block";
  const x = Math.min(e.clientX + 14, innerWidth - tipEl.offsetWidth - 8);
  tipEl.style.left = `${x}px`; tipEl.style.top = `${e.clientY + 14}px`;
}
const untip = () => { if (tipEl) tipEl.style.display = "none"; };

/* ============================== theme ============================== */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("tp-theme", t); } catch { /* storage blocked: theme still applies for this view */ }
}
(function initTheme() {
  let t = null;
  try { t = localStorage.getItem("tp-theme"); } catch { /* ignore */ }
  applyTheme(t || "dark");          // dark-first trading desk (req.md); the toggle is remembered per browser
})();

/* ============================== live (SSE) ============================== */
function startLive() {
  if (state.es) return;
  const es = new EventSource("/api/events/stream");
  state.es = es;
  es.addEventListener("hello", (m) => {
    setLive("live");
    // After a deploy the stream reconnects: if the server runs newer dashboard code, this tab is stale.
    let b = null;
    try { b = JSON.parse(m.data).build; } catch { /* ignore */ }
    if (b && state.meta && state.meta.build && b !== state.meta.build) showUpdateBanner();
  });
  es.addEventListener("trade", (m) => {
    let ev; try { ev = JSON.parse(m.data); } catch { return; }
    document.dispatchEvent(new CustomEvent("tp:event", { detail: ev }));
  });
  es.onerror = () => setLive(es.readyState === EventSource.CLOSED ? "down" : "connecting");
  es.onopen = () => setLive("live");
}
function showUpdateBanner() {
  if ($("#updateBanner")) return;
  const el = document.createElement("div");
  el.id = "updateBanner";
  el.setAttribute("role", "alert");
  el.className = "update-banner";
  el.innerHTML = `${icon("refresh", 16)}<span><b>TradePulse was updated.</b> Reload this page before saving anything.</span><button class="btn primary sm" id="updateReload">Reload now</button>`;
  document.body.appendChild(el);
  $("#updateReload").addEventListener("click", () => location.reload());
}
function stopLive() { if (state.es) { state.es.close(); state.es = null; } setLive("idle"); }
function setLive(s) {
  state.live = s;
  const p = $("#livePill");
  if (!p) return;
  p.dataset.state = s;
  $("span.lbl", p).textContent = s === "live" ? "Live" : s === "connecting" ? "Reconnecting…" : s === "down" ? "Offline" : "Idle";
}
function onLive(fn) {
  const h = (e) => fn(e.detail);
  document.addEventListener("tp:event", h);
  return () => document.removeEventListener("tp:event", h);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ============================== login ============================== */
function renderLogin(err = "") {
  stopLive();
  $("#app").innerHTML = `<div class="login-wrap"><div class="card login">
    <div class="brand" style="padding:0"><div class="brand-mark">${icon("activity", 18)}</div><div><div class="brand-name">TradePulse</div><div class="brand-sub">SkyMatrix OMS monitoring</div></div></div>
    <h1>Sign in</h1><p class="muted" style="margin:0">Use your TradePulse login (same username and password as the OMS).</p>
    <form id="loginForm" novalidate>
      <div class="field"><label for="u">Username</label><input class="input" id="u" name="username" autocomplete="username" required></div>
      <div class="field"><label for="p">Password</label><input class="input" id="p" name="password" type="password" autocomplete="current-password" required></div>
      <div class="error-text" id="loginErr" role="alert">${esc(err)}</div>
      <button class="btn primary" type="submit">Sign in</button>
    </form></div></div>`;
  $("#u").focus();
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (!f.get("username") || !f.get("password")) { $("#loginErr").textContent = "Enter your username and password."; return; }
    const btn = e.target.querySelector("button");
    try {
      await busy(btn, () => api("POST", "/api/auth/login", { username: f.get("username"), password: f.get("password") }));
      boot();
    } catch (ex) { $("#loginErr").textContent = ex.message; }
  });
}

/* ============================== shell & router ============================== */
const NAV = [
  ["Monitor", [["overview", "Overview", "dashboard"], ["feed", "Live activity", "activity"], ["orders", "Orders", "list"],
    ["positions", "Positions", "layers"], ["holdings", "Holdings", "inbox"], ["reconcile", "Reconcile", "refresh"],
    ["performance", "Performance", "trending"]]],
  ["Manage", [["accounts", "Accounts", "users"], ["groups", "Groups", "folder"], ["telegram", "Telegram", "send"],
    ["notifications", "Notifications", "bell"], ["users", "Users", "shield"]]],
  ["System", [["system", "Health & settings", "server"]]],
];
const TITLES = Object.fromEntries(NAV.flatMap(([, items]) => items.map(([k, l]) => [k, l])));

function renderShell() {
  $("#app").innerHTML = `<div class="shell" id="shell">
    <aside class="sidebar" aria-label="Main navigation">
      <div class="brand"><div class="brand-mark">${icon("activity", 18)}</div><div><div class="brand-name">TradePulse</div><div class="brand-sub">OMS monitor</div></div></div>
      <nav class="nav">${NAV.map(([sec, items]) => `<div class="nav-label">${sec}</div>${items.filter(([k]) => k !== "users" || isAdmin()).map(([k, l, ic]) =>
        `<a href="#/${k}" data-route="${k}">${icon(ic)}<span>${l}</span>${k === "notifications" ? '<span class="badge down" id="deadBadge" hidden></span>' : ""}</a>`).join("")}`).join("")}</nav>
      <div class="sidebar-foot"><div><b class="text-2">${esc(state.me.name)}</b> · ${esc(state.me.role)}</div><div>Read-only monitor · v${esc(state.meta?.version || "")}</div></div>
    </aside>
    <div class="main">
      <header class="topbar">
        <button class="btn ghost icon menu-btn" id="menuBtn" aria-label="Open navigation">${icon("menu")}</button>
        <h1 id="pageTitle">TradePulse</h1><div class="spacer"></div>
        <span class="live-pill" id="livePill" data-state="idle" title="Real-time stream"><span class="live-dot"></span><span class="lbl">Idle</span></span>
        <span class="muted num hide-sm" id="clock"></span>
        <button class="btn ghost icon" id="themeBtn" aria-label="Toggle light/dark theme">${icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon")}</button>
        <button class="btn ghost icon" id="logoutBtn" aria-label="Sign out" title="Sign out">${icon("logout")}</button>
      </header>
      <main class="content" id="content" tabindex="-1"></main>
    </div></div>`;
  $("#menuBtn").addEventListener("click", () => $("#shell").classList.toggle("nav-open"));
  $(".sidebar").addEventListener("click", (e) => { if (e.target.closest("a")) $("#shell").classList.remove("nav-open"); });
  $("#shell").addEventListener("click", (e) => { if (e.target.id === "shell") $("#shell").classList.remove("nav-open"); });
  $("#themeBtn").addEventListener("click", () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(t); $("#themeBtn").innerHTML = icon(t === "dark" ? "sun" : "moon");
  });
  $("#logoutBtn").addEventListener("click", async () => { await api("POST", "/api/auth/logout").catch(() => {}); state.me = null; renderLogin(); });
  const tick = () => { const c = $("#clock"); if (c) c.textContent = new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) + " IST"; };
  tick(); clearInterval(state.clockTimer); state.clockTimer = setInterval(tick, 1000);
  setLive(state.live);
}

const PAGES = {};
async function route() {
  const name = (location.hash.replace(/^#\/?/, "").split("?")[0] || "overview");
  const page = PAGES[name] ? name : "overview";
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
  state.route = page;
  $$(".nav a").forEach((a) => (a.dataset.route === page ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#pageTitle").textContent = TITLES[page];
  document.title = `${TITLES[page]} · TradePulse`;
  // A fresh container per navigation: listeners a page attached to it can never leak into the next visit.
  const old = $("#content");
  const content = old.cloneNode(false);
  old.replaceWith(content);
  content.innerHTML = skeleton(6);
  try { state.cleanup = (await PAGES[page](content)) || null; }
  catch (e) { if (e.message !== "Signed out") content.innerHTML = `<div class="card">${empty("Couldn't load this page", esc(e.message), "alert")}</div>`; }
}
window.addEventListener("hashchange", route);

async function boot() {
  try { state.me = await GET("/api/auth/me"); } catch { return renderLogin(); }
  state.meta = await GET("/api/meta").catch(() => ({ event_types: [], brokers: [] }));
  state.clients = null;
  renderShell();
  startLive();
  route();
  refreshDeadBadge();
}
async function refreshDeadBadge() {
  try {
    const o = await GET("/api/overview");
    const b = $("#deadBadge"); if (!b) return;
    const d = o.health?.dead || 0; b.hidden = !d; b.textContent = d;
  } catch { /* badge is optional */ }
}
async function clients() {
  if (!state.clients) state.clients = await GET("/api/clients");
  return state.clients;
}
const allAccounts = async () => (await clients()).flatMap((c) => c.accounts);
function accountOptions(accts, sel = "", allLabel = "All accounts") {
  return `<option value="">${esc(allLabel)}</option>` + accts.map((a) =>
    `<option value="${esc(a.account_id)}" ${a.account_id === sel ? "selected" : ""}>${esc(a.client_name)} · ${esc(broker(a.broker))} ${esc(a.masked_account)}</option>`).join("");
}

/* ============================== charts ============================== */
function hourChart(byHour) {
  const hours = Array.from({ length: 10 }, (_, i) => String(i + 8).padStart(2, "0"));
  Object.keys(byHour || {}).forEach((h) => { if (!hours.includes(h)) hours.push(h); });
  hours.sort();
  const vals = hours.map((h) => byHour?.[h] || 0);
  const max = Math.max(4, ...vals);
  const W = 520, H = 160, pl = 28, pb = 20, pt = 8, bw = (W - pl) / hours.length;
  const y = (v) => pt + (H - pt - pb) * (1 - v / max);
  const ticks = [0, Math.round(max / 2), max];
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Events per hour today"><g class="grid">${ticks.map((t) => `<line x1="${pl}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}</g>
    <g class="axis">${ticks.map((t) => `<text x="${pl - 6}" y="${y(t) + 3}" text-anchor="end">${t}</text>`).join("")}
    ${hours.map((h, i) => `<text x="${pl + i * bw + bw / 2}" y="${H - 4}" text-anchor="middle">${h}</text>`).join("")}</g>`;
  hours.forEach((h, i) => {
    const v = vals[i], x = pl + i * bw + 3, w = Math.max(4, bw - 6), top = y(v), bh = H - pb - top;
    if (v) s += `<path class="bar" d="M${x},${H - pb} V${top + 4} q0,-4 4,-4 h${w - 8} q4,0 4,4 V${H - pb} Z"/>`;
    s += `<rect class="hit" x="${pl + i * bw}" y="${pt}" width="${bw}" height="${H - pt - pb}" data-tip="${h}:00–${h}:59 IST · <b>${v}</b> event${v === 1 ? "" : "s"}"/>`;
    void bh;
  });
  return `<div class="chart" data-chart>${s}</svg></div>`;
}
function wireTips(root) {
  $$("[data-tip]", root).forEach((el) => {
    el.addEventListener("mousemove", (e) => tip(e, el.dataset.tip));
    el.addEventListener("mouseleave", untip);
  });
}
function pnlBars(rows) {
  rows = rows.filter((r) => r.net || r.open_legs || r.closed_legs);
  if (!rows.length) return empty("No P&L yet", "Positions opened or closed through the OMS appear here.", "trending");
  const max = Math.max(...rows.map((r) => Math.abs(r.net)), 1);
  return `<div class="pnl-bars">${rows.sort((a, b) => b.net - a.net).map((r) => {
    const w = (Math.abs(r.net) / max) * 50;
    return `<div class="pnl-row" data-tip="<b>${esc(r.client_name)}</b> ${esc(r.masked_account)}<br>Realised ${esc(inr(r.realized, false))} · Unrealised ${esc(inr(r.unrealized, false))}">
      <span class="nm">${esc(r.client_name)} <span class="muted">${esc(r.masked_account)}</span></span>
      <div class="pnl-track"><span class="mid"></span>${r.net ? `<span class="fill ${r.net > 0 ? "pos" : "neg"}" style="width:${w}%"></span>` : ""}</div>
      <span class="v">${inr(r.net)}</span></div>`;
  }).join("")}</div>`;
}

/* ============================== event rendering ============================== */
function feedItem(e, isNew = false) {
  const qty = e.type === "TRADE_EXECUTED" ? `${n0(e.quantity)} @ ${n2(e.price)}` :
    e.type === "POSITION_UPDATED" ? `open ${n0(e.quantity)}` : `${n0(e.filled_quantity)}/${n0(e.quantity)}${e.average_price ? ` @ ${n2(e.average_price)}` : e.price ? ` @ ${n2(e.price)}` : ""}`;
  return `<div class="feed-item${isNew ? " new" : ""}" data-event="${esc(e.event_id)}">
    <span class="time">${tm(e.occurred_at)}</span>
    <div class="what"><div class="line1">${typeBadge(e.type)} <b>${esc(e.symbol || (e.payload && e.payload.kind) || "")}</b> ${side(e.side)} <span class="num text-2">${e.type === "ACCOUNT_ALERT" ? "" : qty}</span> ${modeBadge(e.trading_mode)}</div>
      <div class="line2">${esc(e.client_name || "–")} · ${esc(broker(e.broker))} ${esc(e.masked_account || "")}${e.message ? ` · ${esc(e.message)}` : ""}</div></div>
    <span class="end">${e.status ? badge(e.status) : ""}</span></div>`;
}
function eventRow(e, isNew = false) {
  return `<tr class="clickable${isNew ? " new" : ""}" data-event="${esc(e.event_id)}">
    <td class="num">${tm(e.occurred_at)}<div class="muted" style="font-size:11px">${esc(String(e.occurred_at || "").slice(0, 10))}</div></td>
    <td>${typeBadge(e.type)}</td><td>${esc(e.client_name)}</td><td>${esc(broker(e.broker))}</td><td class="mono">${esc(e.masked_account)}</td>
    <td><b>${esc(e.symbol)}</b></td><td>${esc(e.exchange || "–")}</td><td>${side(e.side)}</td><td>${esc(e.order_type || "–")}</td>
    <td class="num">${n0(e.quantity)}</td><td class="num">${n0(e.filled_quantity)}</td><td class="num">${n2(e.average_price || e.price)}</td>
    <td>${e.status ? badge(e.status) : "–"} ${modeBadge(e.trading_mode)}</td><td class="mono">${esc(e.order_id || "–")}</td><td class="mono">${esc(e.trade_id || "–")}</td></tr>`;
}
const EVENT_HEAD = `<tr><th>Time</th><th>Event</th><th>Client</th><th>Broker</th><th>Account</th><th>Symbol</th><th>Exch</th><th>Side</th><th>Type</th><th class="num">Qty</th><th class="num">Exec</th><th class="num">Price</th><th>Status</th><th>Order ID</th><th>Trade ID</th></tr>`;

async function openEvent(id) {
  const e = await GET(`/api/events/${encodeURIComponent(id)}`);
  openModal({ title: `${(TYPE[e.type] || [e.type])[0]} · ${e.symbol || e.client_name}`, drawer: true, body: `
    <div class="card"><div class="card-b"><dl class="kv">
      <dt>Event</dt><dd>${typeBadge(e.type)} ${e.status ? badge(e.status) : ""} ${modeBadge(e.trading_mode)}</dd>
      <dt>Client</dt><dd>${esc(e.client_name)} · ${esc(broker(e.broker))} <span class="mono">${esc(e.masked_account || "")}</span></dd>
      <dt>Instrument</dt><dd><b>${esc(e.symbol)}</b> ${esc(e.exchange)} ${side(e.side)} ${esc(e.order_type)} ${esc(e.product)}</dd>
      <dt>Quantity</dt><dd class="num">${n0(e.filled_quantity)} / ${n0(e.quantity)}</dd>
      <dt>Price / avg</dt><dd class="num">${n2(e.price)} / ${n2(e.average_price)}</dd>
      <dt>Order ID</dt><dd class="mono">${esc(e.order_id || "–")} <span class="muted">(OMS ${esc(e.internal_order_id || "–")})</span></dd>
      <dt>Trade ID</dt><dd class="mono">${esc(e.trade_id || "–")}</dd>
      <dt>Message</dt><dd>${esc(e.message || "–")}</dd>
      <dt>Source</dt><dd>${esc(e.source)}</dd>
      <dt>Occurred</dt><dd>${dt(e.occurred_at)} IST</dd><dt>Received</dt><dd>${dt(e.received_at)} IST</dd>
    </dl></div></div>
    <div class="card"><div class="card-h"><h3>Notifications</h3></div><div class="card-b flush">${e.notifications.length ? `<div class="table-wrap"><table class="t">
      <thead><tr><th>Chat</th><th>Status</th><th class="num">Attempts</th><th>Sent</th><th>Error</th></tr></thead><tbody>${e.notifications.map((n) =>
      `<tr><td>${esc(n.title || n.kind)}</td><td>${badge(n.status)}</td><td class="num">${n.attempts}</td><td>${tm(n.sent_at)}</td><td class="wrap">${esc(n.last_error || "")}</td></tr>`).join("")}
      </tbody></table></div>` : empty("Not sent anywhere", "The account isn't monitored, no group covers it, or rules turned this event type off.", "bell")}</div></div>
    ${e.internal_order_id ? `<button class="btn" data-order="${esc(e.internal_order_id)}">${icon("list")} Open order lifecycle</button>` : ""}` });
  const b = $(".overlay [data-order]");
  if (b) b.addEventListener("click", () => openOrder(b.dataset.order));
}

async function openOrder(ref) {
  const o = await GET(`/api/orders/${encodeURIComponent(ref)}`);
  openModal({ title: `Order ${o.broker_order_id || o.internal_order_id}`, drawer: true, body: `
    <div class="card"><div class="card-b"><dl class="kv">
      <dt>Status</dt><dd>${badge(o.status)} ${modeBadge(o.trading_mode)}</dd>
      <dt>Client</dt><dd>${esc(o.client_name)} · ${esc(broker(o.broker))} <span class="mono">${esc(o.masked_account)}</span></dd>
      <dt>Instrument</dt><dd><b>${esc(o.symbol)}</b> ${esc(o.exchange)} · ${side(o.side)} · ${esc(o.order_type)} ${esc(o.product)}</dd>
      <dt>Filled</dt><dd class="num">${n0(o.filled_quantity)} / ${n0(o.ordered_quantity)} @ avg ${n2(o.average_price)} (limit ${n2(o.price)})</dd>
      <dt>Modifications</dt><dd class="num">${n0(o.modification_count)}</dd>
      <dt>Broker order</dt><dd class="mono">${esc(o.broker_order_id || "–")}</dd><dt>OMS order</dt><dd class="mono">${esc(o.internal_order_id)}</dd>
      <dt>Strategy</dt><dd>${esc(o.strategy_id || "–")}</dd><dt>Created</dt><dd>${dt(o.created_at)}</dd><dt>Updated</dt><dd>${dt(o.updated_at)}</dd>
      ${o.status_message ? `<dt>Message</dt><dd>${esc(o.status_message)}</dd>` : ""}
    </dl></div></div>
    <div class="card"><div class="card-h"><h3>Lifecycle</h3><span class="sub">${o.lifecycle.length} transitions</span></div><div class="card-b">
      <ol class="timeline">${o.lifecycle.map((l) => `<li><div><b>${esc(l.from_status || "∅")} → ${esc(l.to_status)}</b> <span class="badge">${esc(l.source)}</span>
        ${l.filled_quantity ? `<span class="num text-2"> filled ${n0(l.filled_quantity)} @ ${n2(l.average_price)}</span>` : ""}</div>
        <div class="tl-t">${dt(l.at)}</div>${l.message ? `<div class="text-2">${esc(l.message)}</div>` : ""}</li>`).join("")}</ol></div></div>
    <div class="card"><div class="card-h"><h3>Fills</h3></div><div class="card-b flush">${o.fills.length ? `<div class="table-wrap"><table class="t">
      <thead><tr><th>Time</th><th class="num">Qty</th><th class="num">Price</th><th>Exchange trade</th><th>Source</th></tr></thead><tbody>${o.fills.map((f) =>
      `<tr><td>${tm(f.filled_at)}</td><td class="num">${n0(f.quantity)}</td><td class="num">${n2(f.price)}</td><td class="mono">${esc(f.exchange_trade_id || "–")}</td><td>${esc(f.source)}</td></tr>`).join("")}</tbody></table></div>`
      : empty("No fills", "Nothing executed for this order.")}</div></div>` });
}

/* ============================== Overview ============================== */
PAGES.overview = async (root) => {
  const load = async () => {
    const [o, ev, perf] = await Promise.all([GET("/api/overview"), GET("/api/events?limit=25"), GET("/api/performance?period=today")]);
    const accts = await GET("/api/monitoring/accounts");
    const hl = o.health;
    const H = (ok, label, value, ic) => `<div class="hcell"><span class="status-ic ${ok === true ? "ok" : ok === false ? "bad" : ok === "warn" ? "warn" : "off"}">${icon(ic, 14)}</span><div style="min-width:0"><div class="hl">${label}</div><div class="hv">${value}</div></div></div>`;
    const lw = o.last_webhook;
    root.innerHTML = `
      <div class="kpis" id="kpis">
        <div class="kpi accent"><div class="k-label">${icon("users", 12)} Clients</div><div class="k-value">${o.monitored_clients}<span class="muted" style="font-size:15px"> / ${o.clients}</span></div><div class="k-sub">monitored · ${o.monitored_accounts}/${o.accounts} accts</div></div>
        <div class="kpi"><div class="k-label">${icon("list", 12)} Orders</div><div class="k-value">${n0(o.orders.total)}</div><div class="k-sub">${o.orders.filled} filled · ${o.orders.pending} working</div></div>
        <div class="kpi"><div class="k-label">${icon("zap", 12)} Trades</div><div class="k-value">${n0(o.trades_today)}</div><div class="k-sub">${o.open_positions} open position${o.open_positions === 1 ? "" : "s"}</div></div>
        <div class="kpi"><div class="k-label">${icon("alert", 12)} Rejected</div><div class="k-value ${o.orders.rejected + o.orders.failed ? "down" : ""}">${n0(o.orders.rejected + o.orders.failed)}</div><div class="k-sub">${o.orders.cancelled} cancelled</div></div>
        <div class="kpi"><div class="k-label">${icon("layers", 12)} Partial fills</div><div class="k-value">${n0(o.orders.partial)}</div><div class="k-sub">still working</div></div>
        <div class="kpi"><div class="k-label">${icon("trending", 12)} Net P&amp;L</div><div class="k-value" style="font-size:19px">${inr(o.pnl.total.net)}</div><div class="k-sub">realised ${inr(o.pnl.total.realized, false)}</div></div>
        <div class="kpi"><div class="k-label">${icon("clock", 12)} Last event</div><div class="k-value" style="font-size:18px">${o.last_event_at ? tm(o.last_event_at) : "–"}</div><div class="k-sub">${o.events_today} today · webhook ${lw ? `${esc(broker(lw.broker))} ${ago(lw.at)}` : "none yet"}</div></div>
        <div class="kpi"><div class="k-label">${icon("send", 12)} Sent</div><div class="k-value">${n0(o.notifications_today.SENT || 0)}</div><div class="k-sub">${hl.queue} queued · <span class="${hl.dead ? "down" : ""}">${hl.dead} failed</span></div></div>
      </div>
      <div class="card"><div class="card-b"><div class="health">
        ${H(hl.redis ? true : "warn", "Redis", hl.redis ? "Connected" : "In-process (dev)", "zap")}
        ${H(hl.oms_stream ? true : false, "OMS stream", hl.oms_last_message ? `last message ${ago(hl.oms_last_message)}` : hl.oms_stream ? "connected, quiet" : "not reading", "activity")}
        ${H(hl.reconcile ? (hl.reconcile.status === "DONE" && !hl.reconcile.mismatches ? true : "warn") : null, "Reconcile",
          hl.reconcile ? `${esc(hl.reconcile.trigger.replace("SCHEDULED ", ""))} · ${esc(hl.reconcile.status)}${hl.reconcile.mismatches ? ` · ${hl.reconcile.mismatches} mismatch` : ""}` : "not yet today", "refresh")}
        ${H(hl.telegram ? (hl.bot_ok === false ? false : true) : null, "Telegram bot", hl.telegram ? (hl.bot_ok === false ? "Error" : "Configured") : "Not configured", "send")}
        ${H(hl.dead ? "warn" : true, "Delivery queue", `${hl.queue} queued · ${hl.dead} dead`, "bell")}
      </div></div></div>
      <div class="grid g-main">
        <div class="card"><div class="card-h"><h2>Live activity</h2><span class="sub">latest first</span><div class="actions"><a class="btn sm" href="#/feed">Open feed ${icon("chevron", 12)}</a></div></div>
          <div class="card-b flush"><div class="feed capped" id="ovFeed">${ev.length ? ev.map((e) => feedItem(e)).join("") : empty("No activity yet today", "Order events from the OMS will stream in here as they happen.", "activity")}</div></div></div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Net P&amp;L by account</h2><span class="sub">today</span></div><div class="card-b" id="ovPnl">${pnlBars(perf.accounts)}</div></div>
          <div class="card"><div class="card-h"><h2>Events per hour</h2><span class="sub">today, IST</span></div><div class="card-b">${hourChart(o.stats.by_hour)}</div></div>
        </div>
      </div>
      <div class="card"><div class="card-h"><h2>Accounts</h2><span class="sub">${accts.length} visible</span><div class="actions"><a class="btn sm" href="#/accounts">Manage ${icon("chevron", 12)}</a></div></div>
        <div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Client</th><th>Broker</th><th>Account</th><th>Monitoring</th><th>Session</th><th>Worker</th><th>Mode</th><th class="num">Orders</th><th class="num">Rejected</th><th class="num">Net P&amp;L</th></tr></thead><tbody>
        ${accts.map((a) => { const c = o.orders_by_account[a.account_id] || {}; const p = (o.pnl.accounts[a.account_id] || {});
          return `<tr class="clickable" data-account="${esc(a.account_id)}"><td><b>${esc(a.client_name)}</b></td><td>${esc(broker(a.broker))}</td><td class="mono">${esc(a.masked_account)}</td>
          <td>${a.monitored ? (a.muted ? badge("PENDING", "Muted") : badge("ACTIVE", "Monitored")) : badge("", "Off")}</td><td>${badge(a.session_status)}</td><td>${a.worker_status ? badge(a.worker_status) : "–"}</td>
          <td>${esc(a.trading_mode || "–")}</td><td class="num">${n0(c.total || 0)}</td><td class="num ${c.rejected ? "down" : ""}">${n0(c.rejected || 0)}</td><td class="num">${inr(p.net ?? 0)}</td></tr>`; }).join("")}
        </tbody></table></div></div></div>`;
    wireTips(root);
    $$("[data-account]", root).forEach((r) => r.addEventListener("click", () => openAccount(r.dataset.account)));
    $$("#ovFeed [data-event]", root).forEach((r) => r.addEventListener("click", () => openEvent(r.dataset.event)));
  };
  await load();
  const refresh = debounce(() => { if (state.route === "overview") load().catch(() => {}); }, 2500);
  return onLive((e) => {
    const f = $("#ovFeed");
    if (f) { $(".empty", f)?.remove(); f.insertAdjacentHTML("afterbegin", feedItem(e, true)); while (f.children.length > 25) f.lastElementChild.remove(); }
    refresh();
  });
};

/* ============================== Live feed ============================== */
PAGES.feed = async (root) => {
  const accts = await allAccounts();
  const cl = await clients();
  const today = state.meta.today;
  const f = { search: "", client: "", account_id: "", broker: "", types: "", day: today };
  let paused = false, rows = [];
  root.innerHTML = `<div class="card">
    <div class="card-h"><div class="row" style="flex:1">
      <div class="search">${icon("search", 14)}<input class="input" id="fSearch" placeholder="Symbol, order or trade ID" aria-label="Search"></div>
      <select class="select" id="fClient" aria-label="Client"><option value="">All clients</option>${cl.map((c) => `<option>${esc(c.name)}</option>`).join("")}</select>
      <select class="select" id="fAccount" aria-label="Account">${accountOptions(accts)}</select>
      <select class="select" id="fBroker" aria-label="Broker"><option value="">All brokers</option>${state.meta.brokers.map((b) => `<option value="${esc(b)}">${esc(broker(b))}</option>`).join("")}</select>
      <select class="select" id="fType" aria-label="Event type"><option value="">All events</option>${state.meta.event_types.map((t) => `<option value="${t.type}">${esc(t.label)}</option>`).join("")}</select>
      <input class="input" type="date" id="fDay" value="${today}" aria-label="Date">
    </div><div class="actions">
      <button class="btn" id="pauseBtn" aria-pressed="false">${icon("pause", 14)} Pause</button>
      <button class="btn" id="exportBtn">${icon("download", 14)} Export CSV</button></div></div>
    <div class="card-b flush"><div class="table-wrap tall"><table class="t"><thead>${EVENT_HEAD}</thead><tbody id="feedBody"></tbody></table><div id="feedEmpty"></div></div>
    <div class="card-h" style="border-top:1px solid var(--border);border-bottom:0"><span class="sub" id="feedCount"></span><div class="actions"><button class="btn sm" id="moreBtn" hidden>Load older</button></div></div></div></div>`;
  const body = $("#feedBody");
  const params = (extra = {}) => qs({ ...f, limit: 300, ...extra });
  const draw = () => {
    body.innerHTML = rows.map((e) => eventRow(e)).join("");
    $("#feedEmpty").innerHTML = rows.length ? "" : empty("No events match", "Try a different date or clear the filters.", "search");
    $("#feedCount").textContent = `${rows.length} event${rows.length === 1 ? "" : "s"} shown${paused ? " · paused" : ""}`;
    $("#moreBtn").hidden = rows.length < 300;
  };
  const load = async () => { rows = await GET(`/api/events?${params()}`); draw(); };
  const deb = debounce(load, 250);
  $("#fSearch").addEventListener("input", (e) => { f.search = e.target.value.trim(); deb(); });
  [["fClient", "client"], ["fAccount", "account_id"], ["fBroker", "broker"], ["fType", "types"], ["fDay", "day"]].forEach(([id, k]) =>
    $(`#${id}`).addEventListener("change", (e) => { f[k] = e.target.value; load(); }));
  $("#pauseBtn").addEventListener("click", (e) => {
    paused = !paused; e.currentTarget.setAttribute("aria-pressed", paused);
    e.currentTarget.innerHTML = paused ? `${icon("play", 14)} Resume` : `${icon("pause", 14)} Pause`;
    if (!paused) load(); else draw();
  });
  $("#exportBtn").addEventListener("click", () => { location.href = `/api/events/export.csv?${qs(f)}`; });
  $("#moreBtn").addEventListener("click", async (e) => {
    const older = await busy(e.currentTarget, () => GET(`/api/events?${params({ before_id: rows[rows.length - 1].id })}`));
    rows = rows.concat(older); draw();
  });
  body.addEventListener("click", (e) => { const tr = e.target.closest("[data-event]"); if (tr) openEvent(tr.dataset.event); });
  await load();
  const matches = (e) => (!f.account_id || e.account_id === f.account_id) && (!f.broker || e.broker === f.broker) && (!f.types || e.type === f.types)
    && (!f.client || (e.client_name || "").includes(f.client)) && (!f.day || String(e.occurred_at).slice(0, 10) === f.day)
    && (!f.search || [e.symbol, e.order_id, e.internal_order_id, e.trade_id, e.client_name].some((v) => (v || "").toLowerCase().includes(f.search.toLowerCase())));
  return onLive((e) => {
    if (paused || !matches(e)) return;
    rows.unshift(e);
    $("#feedEmpty").innerHTML = "";
    body.insertAdjacentHTML("afterbegin", eventRow(e, true));
    $("#feedCount").textContent = `${rows.length} events shown`;
  });
};

/* ============================== Orders ============================== */
PAGES.orders = async (root) => {
  const accts = await allAccounts();
  const f = { status: "", account_id: "", day: state.meta.today, search: "" };
  const SEG = [["", "All"], ["PENDING", "Working"], ["FILLED", "Filled"], ["PARTIALLY_FILLED", "Partial"], ["REJECTED", "Rejected"], ["CANCELLED", "Cancelled"], ["FAILED", "Failed"]];
  root.innerHTML = `<div class="card"><div class="card-h"><div class="row" style="flex:1">
      <div class="seg" role="group" aria-label="Status">${SEG.map(([v, l]) => `<button data-s="${v}" aria-pressed="${v === ""}">${l}</button>`).join("")}</div>
      <select class="select" id="oAcct" aria-label="Account">${accountOptions(accts)}</select>
      <input class="input" type="date" id="oDay" value="${f.day}" aria-label="Date">
      <div class="search">${icon("search", 14)}<input class="input" id="oSearch" placeholder="Symbol or order ID" aria-label="Search orders"></div>
    </div><div class="actions"><span class="sub" id="oCount"></span></div></div>
    <div class="card-b flush"><div class="table-wrap tall" id="oTable"></div></div></div>`;
  const load = async () => {
    const rows = await GET(`/api/orders?${qs(f)}`);
    $("#oCount").textContent = `${rows.length} order${rows.length === 1 ? "" : "s"}`;
    $("#oTable").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Created</th><th>Client</th><th>Account</th><th>Symbol</th><th>Side</th><th>Type</th><th class="num">Qty</th><th class="num">Filled</th><th class="num">Price</th><th class="num">Avg</th><th>Status</th><th>Broker order</th><th>Message</th></tr></thead><tbody>
      ${rows.map((o) => `<tr class="clickable" data-order="${esc(o.internal_order_id)}"><td class="num">${tm(o.created_at)}</td><td>${esc(o.client_name)}</td><td class="mono">${esc(o.masked_account)} <span class="muted">${esc(broker(o.broker))}</span></td>
        <td><b>${esc(o.symbol)}</b> <span class="muted">${esc(o.exchange)}</span></td><td>${side(o.side)}</td><td>${esc(o.order_type)}</td><td class="num">${n0(o.ordered_quantity)}</td><td class="num">${n0(o.filled_quantity)}</td>
        <td class="num">${n2(o.price)}</td><td class="num">${o.average_price ? n2(o.average_price) : "–"}</td><td>${badge(o.status)} ${modeBadge(o.trading_mode)}</td><td class="mono">${esc(o.broker_order_id || "–")}</td><td class="wrap text-2">${esc(o.status_message || "")}</td></tr>`).join("")}
      </tbody></table>` : empty("No orders", "No orders match these filters for the selected day.", "list");
  };
  $$(".seg button", root).forEach((b) => b.addEventListener("click", () => { $$(".seg button", root).forEach((x) => x.setAttribute("aria-pressed", x === b)); f.status = b.dataset.s; load(); }));
  $("#oAcct").addEventListener("change", (e) => { f.account_id = e.target.value; load(); });
  $("#oDay").addEventListener("change", (e) => { f.day = e.target.value; load(); });
  $("#oSearch").addEventListener("input", debounce((e) => { f.search = e.target.value.trim(); load(); }, 250));
  $("#oTable").addEventListener("click", (e) => { const r = e.target.closest("[data-order]"); if (r) openOrder(r.dataset.order); });
  await load();
  const refresh = debounce(() => load().catch(() => {}), 1500);
  return onLive((e) => { if (e.type.startsWith("ORDER_") && f.day === state.meta.today) refresh(); });
};

/* ============================== Positions ============================== */
/* ---------- broker refresh: ONE request every 3 s across everyone (server-enforced; mirrored here) ---------- */
const slot = { until: 0, timer: null };
function slotLeft() { return Math.max(0, slot.until - Date.now()); }
function startSlotCountdown(ms) {
  slot.until = Date.now() + ms;
  clearInterval(slot.timer);
  const paint = () => {
    const left = slotLeft();
    $$("[data-refresh]").forEach((b) => {
      b.disabled = left > 0;
      b.textContent = left > 0 ? `Wait ${Math.ceil(left / 1000)} s` : b.dataset.label;
    });
    const s = $("#slotNote");
    if (s) s.textContent = left > 0 ? `Next broker request allowed in ${Math.ceil(left / 1000)} s (1 request every 3 s, all users).` : "";
    if (!left) clearInterval(slot.timer);
  };
  paint();
  slot.timer = setInterval(paint, 250);
}
function refreshButton(accountId, kind, label) {
  return `<button class="btn sm" data-refresh data-account="${esc(accountId)}" data-kind="${kind}" data-label="${esc(label)}">${esc(label)}</button>`;
}
function wireRefresh(root, after) {
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-refresh]");
    if (!b || b.disabled) return;
    if (slotLeft()) { toast(`Please wait ${Math.ceil(slotLeft() / 1000)} s – one broker request every 3 seconds.`, "error"); return; }
    b.disabled = true;
    try {
      const r = await fetch("/api/reconcile/request", { method: "POST", credentials: "same-origin",
        headers: { "X-TradePulse": "1", "Content-Type": "application/json" }, body: JSON.stringify({ account_id: b.dataset.account, kind: b.dataset.kind }) });
      const data = await r.json().catch(() => ({}));
      if (r.status === 429) { startSlotCountdown(data.wait_ms || 3000); toast(data.detail || "Please wait a few seconds.", "error"); return; }
      if (!r.ok) { toast(data.detail || `Refresh failed (${r.status})`, "error"); startSlotCountdown(3000); return; }
      startSlotCountdown(data.next_allowed_ms || 3000);
      toast(data.mismatches.length ? `${b.dataset.kind} refreshed – ${data.mismatches.length} difference(s) vs the book` : `${b.dataset.kind} refreshed – book matches the broker`,
        data.mismatches.length ? "error" : "ok");
      after && after();
    } catch (ex) { toast(ex.message, "error"); b.disabled = false; }
  });
}
const srcBadge = (src, at) => (at ? `<span class="badge ${String(src).startsWith("BROKER") || String(src).startsWith("USER") ? "up" : "info"}" title="${esc(src)}">${esc(String(src).replace(/^BROKER /, "").split(" ")[0])} ${tm(at)}</span>` : '<span class="badge warn">no snapshot</span>');

/* ============================== Positions (book) ============================== */
PAGES.positions = async (root) => {
  const accts = await allAccounts();
  const f = { state: "open", account_id: "" };
  root.innerHTML = `<div class="callout">Positions = the broker snapshot (09:00 / 09:05 or your last refresh) + every order update since, applied live. No broker is polled.</div>
    <div class="kpis" id="pKpis"></div><div class="card"><div class="card-h"><div class="row" style="flex:1">
      <div class="seg" role="group" aria-label="Position state">${[["open", "Open"], ["closed", "Flat today"], ["all", "All"]].map(([v, l]) => `<button data-s="${v}" aria-pressed="${v === "open"}">${l}</button>`).join("")}</div>
      <select class="select" id="pAcct" aria-label="Account">${accountOptions(accts)}</select></div>
      <div class="actions" id="pRefresh"></div></div>
    <div class="card-b" style="padding-top:0;padding-bottom:0"><span class="hint" id="slotNote" aria-live="polite"></span></div>
    <div class="card-b flush"><div class="table-wrap tall" id="pTable"></div></div></div>`;
  const load = async () => {
    const rows = await GET(`/api/positions?${qs(f)}`);
    const tr = rows.reduce((s, r) => s + (r.realized_pnl || 0), 0), tu = rows.reduce((s, r) => s + (r.unrealized_pnl || 0), 0);
    const unpriced = rows.filter((r) => r.net_quantity && r.unrealized_pnl == null).length;
    $("#pKpis").innerHTML = `<div class="kpi"><div class="k-label">Positions</div><div class="k-value">${rows.length}</div><div class="k-sub">${rows.filter((r) => r.net_quantity).length} open</div></div>
      <div class="kpi"><div class="k-label">Unrealised</div><div class="k-value">${inr(tu)}</div><div class="k-sub">${unpriced ? `${unpriced} without live price` : "vs average price"}</div></div>
      <div class="kpi"><div class="k-label">Realised</div><div class="k-value">${inr(tr)}</div><div class="k-sub">since snapshot</div></div>
      <div class="kpi accent"><div class="k-label">Net</div><div class="k-value">${inr(tr + tu)}</div><div class="k-sub">shown positions</div></div>`;
    $("#pRefresh").innerHTML = f.account_id ? refreshButton(f.account_id, "positions", "Refresh from broker") : '<span class="sub">Pick an account to refresh it from the broker</span>';
    $("#pTable").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Client</th><th>Account</th><th>Symbol</th><th>Product</th><th>Side</th><th class="num">Net qty</th><th class="num">Avg</th><th class="num">LTP</th><th class="num">Unrealised</th><th class="num">Realised</th><th class="num">Snapshot qty</th><th class="num">Trades since</th><th>Snapshot</th></tr></thead><tbody>
      ${rows.map((p) => `<tr><td>${esc(p.client_name)}</td><td class="mono">****${esc(p.account_id.slice(-4))} <span class="muted">${esc(broker(p.broker))}</span></td><td><b>${esc(p.symbol || p.exchange_token)}</b> <span class="muted">${esc(p.exchange)}</span></td>
        <td>${esc(p.product)}</td><td>${side(p.direction)}</td><td class="num ${p.net_quantity < 0 ? "down" : ""}">${n0(p.net_quantity)}</td><td class="num">${n2(p.average_entry_price)}</td>
        <td class="num">${p.ltp ? n2(p.ltp) : '<span class="muted">–</span>'}</td><td class="num">${inr(p.unrealized_pnl)}</td><td class="num">${inr(p.realized_pnl)}</td>
        <td class="num">${n0(p.baseline_qty)}</td><td class="num">${n0(p.fills_applied)}</td><td>${srcBadge(p.baseline_source, p.baseline_at)}</td></tr>`).join("")}</tbody></table>`
      : empty(f.state === "open" ? "No open positions" : "No positions", "Positions appear after the 09:00 snapshot or as order updates arrive.", "layers");
    startSlotCountdown(slotLeft());
  };
  $$(".seg button", root).forEach((b) => b.addEventListener("click", () => { $$(".seg button", root).forEach((x) => x.setAttribute("aria-pressed", x === b)); f.state = b.dataset.s; load(); }));
  $("#pAcct").addEventListener("change", (e) => { f.account_id = e.target.value; load(); });
  wireRefresh(root, load);
  await load();
  const refresh = debounce(() => load().catch(() => {}), 1200);
  return onLive((e) => { if (["POSITION_UPDATED", "RECONCILE_MISMATCH", "TRADE_EXECUTED"].includes(e.type)) refresh(); });
};

/* ============================== Holdings ============================== */
PAGES.holdings = async (root) => {
  const accts = await allAccounts();
  const f = { account_id: "" };
  root.innerHTML = `<div class="callout">Holdings = the broker snapshot + today's delivery (CNC/MTF) trades. Bought today shows as T1 until settled.</div>
    <div class="kpis" id="hKpis"></div><div class="card"><div class="card-h"><div class="row" style="flex:1">
      <select class="select" id="hAcct" aria-label="Account">${accountOptions(accts)}</select></div><div class="actions" id="hRefresh"></div></div>
    <div class="card-b" style="padding-top:0;padding-bottom:0"><span class="hint" id="slotNote" aria-live="polite"></span></div>
    <div class="card-b flush"><div class="table-wrap tall" id="hTable"></div></div></div>`;
  const load = async () => {
    const rows = await GET(`/api/holdings?${qs(f)}`);
    const val = rows.reduce((s, r) => s + (r.value || 0), 0), pnl = rows.reduce((s, r) => s + (r.pnl || 0), 0);
    $("#hKpis").innerHTML = `<div class="kpi"><div class="k-label">Holdings</div><div class="k-value">${rows.length}</div><div class="k-sub">${new Set(rows.map((r) => r.account_id)).size} account(s)</div></div>
      <div class="kpi"><div class="k-label">Value</div><div class="k-value" style="font-size:20px">₹${n2(val)}</div><div class="k-sub">at LTP</div></div>
      <div class="kpi accent"><div class="k-label">P&amp;L</div><div class="k-value">${inr(pnl)}</div><div class="k-sub">vs average price</div></div>
      <div class="kpi"><div class="k-label">Changed today</div><div class="k-value">${rows.filter((r) => r.change).length}</div><div class="k-sub">by delivery trades</div></div>`;
    $("#hRefresh").innerHTML = f.account_id ? refreshButton(f.account_id, "holdings", "Refresh from broker") : '<span class="sub">Pick an account to refresh it from the broker</span>';
    $("#hTable").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Client</th><th>Account</th><th>Symbol</th><th>Product</th><th class="num">Qty</th><th class="num">Sellable</th><th class="num">T1</th><th class="num">Pledged</th><th class="num">Avg</th><th class="num">LTP</th><th class="num">P&amp;L</th><th class="num">Today</th><th>Snapshot</th></tr></thead><tbody>
      ${rows.map((h) => `<tr><td>${esc(h.client_name)}</td><td class="mono">${esc(h.masked_account)} <span class="muted">${esc(broker(h.broker))}</span></td><td><b>${esc(h.symbol)}</b> <span class="muted">${esc(h.exchange)}</span></td><td>${esc(h.product)}</td>
        <td class="num ${h.quantity < 0 ? "down" : ""}">${n0(h.quantity)}</td><td class="num">${n0(h.sellable)}</td><td class="num">${h.t1_quantity ? n0(h.t1_quantity) : "–"}</td><td class="num">${h.pledged_quantity ? n0(h.pledged_quantity) : "–"}</td>
        <td class="num">${n2(h.average_price)}</td><td class="num">${h.ltp ? n2(h.ltp) : "–"}</td><td class="num">${inr(h.pnl)}</td>
        <td class="num ${h.change > 0 ? "up" : h.change < 0 ? "down" : ""}">${h.change ? (h.change > 0 ? "+" : "") + n0(h.change) : "–"}</td><td>${srcBadge(h.baseline_source, h.baseline_at)}</td></tr>`).join("")}</tbody></table>`
      : empty("No holdings", "Holdings appear after the 09:00 snapshot or a refresh.", "inbox");
    startSlotCountdown(slotLeft());
  };
  $("#hAcct").addEventListener("change", (e) => { f.account_id = e.target.value; load(); });
  wireRefresh(root, load);
  await load();
  const refresh = debounce(() => load().catch(() => {}), 1200);
  return onLive((e) => { if (["HOLDING_UPDATED", "RECONCILE_MISMATCH"].includes(e.type)) refresh(); });
};

/* ============================== Reconcile ============================== */
PAGES.reconcile = async (root) => {
  const admin = isAdmin();
  const load = async () => {
    const r = await GET("/api/reconcile");
    const run = r.running;
    root.innerHTML = `<div class="grid g-2">
      <div class="card"><div class="card-h"><h2>How reconciliation works</h2></div><div class="card-b stack" style="font-size:13px">
        <div class="row">${(r.times || []).map((t) => `<span class="badge accent">${esc(t)} IST</span>`).join("")}<span class="muted">monitored accounts · trading days</span></div>
        <p class="text-2" style="margin:0">At each time TradePulse reads positions and holdings from the broker (read-only) and makes them the baseline. Between snapshots the book follows every order update from the OMS stream – no broker calls. A difference between the book and a snapshot is a <b>mismatch</b> and raises an alert.</p>
        <p class="text-2" style="margin:0">Broker requests are limited to <b>one every ${r.slot_seconds} seconds</b> for everyone together. A refresh while the slot is busy is refused with a “please wait” message.</p></div></div>
      <div class="card"><div class="card-h"><h2>Run</h2></div><div class="card-b stack">
        ${run ? `<div class="callout">Running <b>${esc(run.trigger)}</b>: ${run.done}/${run.total} requests · started ${tm(run.started_at)}. <button class="btn sm" id="rcReload">${icon("refresh", 12)} Update</button></div>`
          : '<div class="text-2">No reconcile running.</div>'}
        ${admin ? `<div class="row"><button class="btn primary" id="rcRun" ${run ? "disabled" : ""}>${icon("refresh", 14)} Reconcile all monitored accounts now</button>
          <span class="hint">${r.accounts.filter((a) => a.monitored).length} account(s) × 2 requests × ${r.slot_seconds} s</span></div>` : ""}
        <span class="hint" id="slotNote" aria-live="polite"></span></div></div></div>
      <div class="card"><div class="card-h"><h2>Accounts</h2><span class="sub">latest baseline per account</span></div><div class="card-b flush"><div class="table-wrap"><table class="t">
        <thead><tr><th>Client</th><th>Account</th><th>Broker</th><th>Monitored</th><th>Session</th><th>Positions snapshot</th><th>Holdings snapshot</th><th class="num">Mismatches</th><th>Last failure</th></tr></thead><tbody>
        ${r.accounts.map((a) => `<tr><td><b>${esc(a.client_name)}</b></td><td class="mono">${esc(a.masked_account)}</td><td>${esc(broker(a.broker))}</td>
          <td>${a.monitored ? badge("ACTIVE", "Yes") : badge("", "No")}</td><td>${badge(a.session_status)}</td>
          <td><span class="row" style="flex-wrap:nowrap;gap:6px">${srcBadge(a.positions_source, a.positions_at)}${refreshButton(a.account_id, "positions", "Refresh")}</span></td>
          <td><span class="row" style="flex-wrap:nowrap;gap:6px">${srcBadge(a.holdings_source, a.holdings_at)}${refreshButton(a.account_id, "holdings", "Refresh")}</span></td>
          <td class="num ${a.mismatches ? "down" : ""}">${a.mismatches}</td><td class="wrap text-2">${a.last_failure ? `${esc(a.last_failure.kind)}: ${esc(a.last_failure.error)} (${tm(a.last_failure.at)})` : "–"}</td></tr>`).join("")}
        </tbody></table></div></div></div>
      <div class="grid g-2">
        <div class="card"><div class="card-h"><h2>Mismatches</h2><span class="sub">book vs broker</span></div><div class="card-b flush"><div class="table-wrap tall">${r.diffs.length ? `<table class="t"><thead><tr><th>When</th><th>Account</th><th>Kind</th><th>Instrument</th><th class="num">Book</th><th class="num">Broker</th><th>Run</th></tr></thead><tbody>
          ${r.diffs.map((d) => `<tr><td>${dt(d.at)}</td><td class="mono">${esc(d.masked_account)}</td><td>${esc(d.kind)}</td><td><b>${esc(d.symbol || d.exchange_token)}</b> <span class="muted">${esc(d.exchange)} ${esc(d.product)}</span></td>
            <td class="num">${n0(d.book_qty)}</td><td class="num">${n0(d.broker_qty)}</td><td class="text-2">${esc(d.trigger || "")}</td></tr>`).join("")}</tbody></table>` : empty("No mismatches", "The book agrees with every snapshot.", "check")}</div></div></div>
        <div class="card"><div class="card-h"><h2>Runs</h2></div><div class="card-b flush"><div class="table-wrap tall">${r.runs.length ? `<table class="t"><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th class="num">Requests</th><th class="num">Snapshots</th><th class="num">Errors</th><th class="num">Mismatches</th></tr></thead><tbody>
          ${r.runs.map((x) => `<tr><td>${dt(x.started_at)}</td><td>${esc(x.trigger)}</td><td>${badge(x.status === "DONE" ? "OK" : x.status === "RUNNING" ? "PENDING" : "ERROR", x.status)}</td>
            <td class="num">${x.requests}</td><td class="num">${x.snapshots}</td><td class="num ${x.errors ? "down" : ""}">${x.errors}</td><td class="num ${x.mismatches ? "down" : ""}">${x.mismatches}</td></tr>`).join("")}</tbody></table>` : empty("No runs yet", "The first scheduled run is at 09:00 IST.", "clock")}</div></div></div></div>`;
    startSlotCountdown(slotLeft());
    $("#rcReload")?.addEventListener("click", load);
    $("#rcRun")?.addEventListener("click", async (e) => {
      const res = await busy(e.currentTarget, () => api("POST", "/api/reconcile/run"));
      toast(`Reconcile started for ${res.accounts} account(s) – about ${res.estimated_seconds} s`, "ok");
      setTimeout(load, 500);
    });
  };
  wireRefresh(root, load);
  await load();
  const refresh = debounce(() => load().catch(() => {}), 1500);
  return onLive((e) => { if (e.type === "RECONCILE_MISMATCH") refresh(); });
};

/* ============================== Users (admin) ============================== */
PAGES.users = async (root) => {
  if (!isAdmin()) { root.innerHTML = `<div class="card">${empty("Administrators only", "Ask an administrator to change logins.", "shield")}</div>`; return; }
  const load = async () => {
    const [users, accts] = await Promise.all([GET("/api/users"), allAccounts()]);
    const managers = users.filter((u) => u.role === "MANAGER");
    root.innerHTML = `<div class="row"><p class="muted" style="margin:0;flex:1">TradePulse keeps its own logins so it works when the OMS is offline. Users were imported once from the OMS (same usernames and passwords); manage them here.</p>
      <button class="btn primary" id="uNew">${icon("plus", 14)} New user</button></div>
      <div class="card"><div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th><th>Manager</th><th>Accounts</th><th>Source</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td><b>${esc(u.name)}</b></td><td class="mono">${esc(u.username)}</td><td><span class="badge ${u.role === "ADMIN" ? "violet" : u.role === "MANAGER" ? "info" : ""}">${esc(u.role)}</span></td>
        <td>${badge(u.status)}</td><td>${esc((users.find((m) => m.id === u.manager_id) || {}).name || "–")}</td>
        <td>${u.accounts.map((a) => `<span class="chip">${esc(broker(a.broker))} ${esc(a.masked_account)}</span>`).join(" ") || '<span class="muted">–</span>'}</td>
        <td class="text-2">${esc(u.source)}</td><td><button class="btn sm" data-uedit="${u.id}">${icon("edit", 12)} Edit</button></td></tr>`).join("")}</tbody></table></div></div></div>`;
    const edit = (u) => {
      const own = new Set((u?.accounts || []).map((a) => a.account_id));
      const m = openModal({ title: u ? `Edit ${u.name}` : "New user", body: `
        <div class="grid g-2"><div class="field"><label for="uName">Name</label><input class="input" id="uName" value="${esc(u?.name || "")}"></div>
          <div class="field"><label for="uUser">Username</label><input class="input" id="uUser" value="${esc(u?.username || "")}" ${u ? "readonly" : ""}></div>
          <div class="field"><label for="uRole">Role</label><select class="select" id="uRole">${["CLIENT", "MANAGER", "ADMIN"].map((r) => `<option ${u?.role === r ? "selected" : ""}>${r}</option>`).join("")}</select></div>
          <div class="field"><label for="uStatus">Status</label><select class="select" id="uStatus">${["ACTIVE", "DISABLED"].map((s) => `<option ${u?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
          <div class="field"><label for="uMgr">Manager</label><select class="select" id="uMgr"><option value="">None</option>${managers.map((x) => `<option value="${x.id}" ${u?.manager_id === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></div>
          <div class="field"><label for="uPw">${u ? "New password (leave blank to keep)" : "Password"}</label><input class="input" id="uPw" type="password" autocomplete="new-password" minlength="8"><span class="hint">At least 8 characters. Changing it signs the user out.</span></div></div>
        <div class="field"><span class="label">Accounts this user owns (client view)</span><div class="stack" style="max-height:220px;overflow:auto;gap:6px">
          ${accts.map((a) => `<label class="row" style="gap:8px;font-size:12px"><input type="checkbox" class="check" data-ua="${esc(a.account_id)}" ${own.has(a.account_id) ? "checked" : ""}> ${esc(a.client_name)} · ${esc(broker(a.broker))} <span class="mono">${esc(a.masked_account)}</span></label>`).join("")}</div></div>
        <span class="error-text" id="uErr"></span>`,
        foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="uSave">${u ? "Save" : "Create user"}</button>` });
      $("#uSave", m.el).addEventListener("click", async (e) => {
        const body = { name: $("#uName", m.el).value.trim(), username: $("#uUser", m.el).value.trim(), role: $("#uRole", m.el).value, status: $("#uStatus", m.el).value,
          manager_id: +$("#uMgr", m.el).value || null, password: $("#uPw", m.el).value, account_ids: $$("[data-ua]", m.el).filter((x) => x.checked).map((x) => x.dataset.ua) };
        try { await busy(e.currentTarget, () => api(u ? "PUT" : "POST", u ? `/api/users/${u.id}` : "/api/users", body)); m.close(); toast("Saved", "ok"); state.clients = null; load(); }
        catch (ex) { $("#uErr", m.el).textContent = ex.message; }
      });
    };
    $("#uNew").addEventListener("click", () => edit(null));
    $$("[data-uedit]", root).forEach((b) => b.addEventListener("click", () => edit(users.find((u) => u.id == b.dataset.uedit))));
  };
  await load();
};

/* ============================== Performance ============================== */
PAGES.performance = async (root) => {
  let period = "today";
  root.innerHTML = `<div class="row"><div class="seg" role="group" aria-label="Period">${[["today", "Today"], ["week", "This week"], ["month", "This month"], ["all", "All time"]].map(([v, l]) => `<button data-p="${v}" aria-pressed="${v === "today"}">${l}</button>`).join("")}</div>
    <span class="muted" style="font-size:12px">P&amp;L of OMS positions from their fills; unrealised needs a live price and is never estimated.</span></div><div id="perf"></div>`;
  const load = async () => {
    const p = await GET(`/api/performance?period=${period}`);
    const t = p.total;
    const totals = p.accounts.reduce((s, a) => ({ total: s.total + a.orders.total, filled: s.filled + a.orders.filled, rejected: s.rejected + a.orders.rejected }), { total: 0, filled: 0, rejected: 0 });
    $("#perf").innerHTML = `<div class="stack"><div class="kpis">
      <div class="kpi accent"><div class="k-label">Net P&amp;L</div><div class="k-value">${inr(t.net)}</div><div class="k-sub">since ${esc(p.since)}</div></div>
      <div class="kpi"><div class="k-label">Realised</div><div class="k-value">${inr(t.realized)}</div><div class="k-sub">${t.closed_legs} closed legs</div></div>
      <div class="kpi"><div class="k-label">Unrealised</div><div class="k-value">${inr(t.unrealized)}</div><div class="k-sub">${t.open_legs} open legs${t.unpriced_open_legs ? ` · ${t.unpriced_open_legs} unpriced` : ""}</div></div>
      <div class="kpi"><div class="k-label">Win rate</div><div class="k-value">${t.win_rate == null ? "–" : `${t.win_rate}%`}</div><div class="k-sub">${t.wins} wins · ${t.losses} losses</div></div>
      <div class="kpi"><div class="k-label">Orders</div><div class="k-value">${n0(totals.total)}</div><div class="k-sub">fill rate ${totals.total ? Math.round((100 * totals.filled) / totals.total) : 0}% · rejects ${totals.rejected}</div></div></div>
      <div class="grid g-2"><div class="card"><div class="card-h"><h2>Net P&amp;L by account</h2></div><div class="card-b">${pnlBars(p.accounts)}</div></div>
      <div class="card"><div class="card-h"><h2>Execution quality</h2><span class="sub">orders created in period</span></div><div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Account</th><th class="num">Orders</th><th class="num">Fill rate</th><th class="num">Reject rate</th><th class="num">Cancelled</th></tr></thead><tbody>
        ${p.accounts.map((a) => `<tr><td>${esc(a.client_name)} <span class="mono muted">${esc(a.masked_account)}</span></td><td class="num">${n0(a.orders.total)}</td><td class="num">${a.orders.fill_rate == null ? "–" : a.orders.fill_rate + "%"}</td>
          <td class="num ${a.orders.reject_rate ? "down" : ""}">${a.orders.reject_rate == null ? "–" : a.orders.reject_rate + "%"}</td><td class="num">${n0(a.orders.cancelled)}</td></tr>`).join("")}</tbody></table></div></div></div></div>
      <div class="card"><div class="card-h"><h2>By account</h2></div><div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Client</th><th>Broker</th><th>Account</th><th class="num">Realised</th><th class="num">Unrealised</th><th class="num">Net</th><th class="num">Open</th><th class="num">Closed</th><th class="num">Win rate</th></tr></thead><tbody>
        ${p.accounts.map((a) => `<tr class="clickable" data-account="${esc(a.account_id)}"><td><b>${esc(a.client_name)}</b></td><td>${esc(broker(a.broker))}</td><td class="mono">${esc(a.masked_account)}</td><td class="num">${inr(a.realized)}</td><td class="num">${inr(a.unrealized)}</td>
          <td class="num"><b>${inr(a.net)}</b></td><td class="num">${n0(a.open_legs)}</td><td class="num">${n0(a.closed_legs)}</td><td class="num">${a.win_rate == null ? "–" : a.win_rate + "%"}</td></tr>`).join("")}
        <tr><td colspan="3"><b>Total</b></td><td class="num">${inr(t.realized)}</td><td class="num">${inr(t.unrealized)}</td><td class="num"><b>${inr(t.net)}</b></td><td class="num">${n0(t.open_legs)}</td><td class="num">${n0(t.closed_legs)}</td><td class="num">${t.win_rate == null ? "–" : t.win_rate + "%"}</td></tr>
        </tbody></table></div></div></div></div>`;
    wireTips(root);
    $$("[data-account]", root).forEach((r) => r.addEventListener("click", () => openAccount(r.dataset.account)));
  };
  $$(".seg button", root).forEach((b) => b.addEventListener("click", () => { $$(".seg button", root).forEach((x) => x.setAttribute("aria-pressed", x === b)); period = b.dataset.p; load(); }));
  await load();
};

/* ============================== Accounts ============================== */
PAGES.accounts = async (root) => {
  const admin = isAdmin();
  let data = await GET("/api/clients");
  const groups = admin ? await GET("/api/groups") : [];
  const sel = new Set();
  let q = "", br = "";
  root.innerHTML = `<div class="card"><div class="card-h"><div class="row" style="flex:1">
      <div class="search">${icon("search", 14)}<input class="input" id="aSearch" placeholder="Search clients or accounts" aria-label="Search clients"></div>
      <select class="select" id="aBroker" aria-label="Broker"><option value="">All brokers</option>${state.meta.brokers.map((b) => `<option value="${esc(b)}">${esc(broker(b))}</option>`).join("")}</select>
      <label class="row" style="gap:6px;font-size:12px"><input type="checkbox" class="check" id="aAll"> Select all shown</label></div>
      ${admin ? `<div class="actions"><span class="sub" id="aSel">0 selected</span><button class="btn" id="aEnable" disabled>${icon("check", 14)} Monitor</button><button class="btn" id="aDisable" disabled>Stop monitoring</button><button class="btn" id="aGroup" disabled>${icon("folder", 14)} Add to group</button></div>` : ""}</div>
    <div class="card-b"><div class="callout">Accounts are read live from the OMS (no copies). Turning monitoring on makes an account's events eligible for notifications; the feed and dashboards show every account you can see either way.</div></div></div>
    <div class="stack" id="aList"></div>`;
  const shown = () => data.map((c) => ({ ...c, accounts: c.accounts.filter((a) => (!br || a.broker === br) && (!q || [c.name, a.account_id, a.masked_account, a.username].some((v) => (v || "").toLowerCase().includes(q)))) })).filter((c) => c.accounts.length);
  const draw = () => {
    const cs = shown();
    $("#aList").innerHTML = cs.length ? cs.map((c) => `<section class="client" aria-label="${esc(c.name)}">
      <div class="client-h">${admin ? `<input type="checkbox" class="check" data-client="${c.client_id}" aria-label="Select all of ${esc(c.name)}'s accounts" ${c.accounts.every((a) => sel.has(a.account_id)) ? "checked" : ""}>` : ""}
        <span class="avatar">${esc((c.name || "?").split(" ").map((w) => w[0]).slice(0, 2).join(""))}</span>
        <div><div class="nm">${esc(c.name)}</div><div class="muted" style="font-size:12px">${esc(c.role)} · ${c.accounts.length} account${c.accounts.length === 1 ? "" : "s"}${c.manager_name ? ` · manager ${esc(c.manager_name)}` : ""}</div></div>
        <div style="margin-left:auto" class="row">${badge(c.status)}</div></div>
      <div class="table-wrap"><table class="t"><thead><tr>${admin ? "<th></th>" : ""}<th>Account</th><th>Broker</th><th>Account status</th><th>Session</th><th>Worker</th><th>Mode</th><th>Groups</th><th>Monitoring</th><th></th></tr></thead><tbody>
      ${c.accounts.map((a) => `<tr>${admin ? `<td><input type="checkbox" class="check" data-sel="${esc(a.account_id)}" aria-label="Select ${esc(a.masked_account)}" ${sel.has(a.account_id) ? "checked" : ""}></td>` : ""}
        <td class="mono"><b>${esc(a.masked_account)}</b></td><td>${esc(broker(a.broker))}</td><td>${badge(a.account_status)}</td><td>${badge(a.session_status)}</td><td>${a.worker_status ? badge(a.worker_status) : "–"}</td><td>${esc(a.trading_mode || "–")}</td>
        <td>${a.groups.map((g) => `<span class="chip"><span class="sw" style="background:${esc(g.color)}"></span>${esc(g.name)}</span>`).join(" ") || '<span class="muted">–</span>'}</td>
        <td><label class="switch"><input type="checkbox" data-mon="${esc(a.account_id)}" ${a.monitored ? "checked" : ""} ${admin ? "" : "disabled"} aria-label="Monitor ${esc(a.masked_account)}"><span class="track"></span><span>${a.monitored ? (a.muted ? "Muted" : "On") : "Off"}</span></label></td>
        <td><button class="btn sm" data-detail="${esc(a.account_id)}">Details ${icon("chevron", 12)}</button></td></tr>`).join("")}</tbody></table></div></section>`).join("")
      : `<div class="card">${empty("No accounts match", "Clear the search or broker filter.", "users")}</div>`;
    if (admin) {
      $("#aSel").textContent = `${sel.size} selected`;
      ["#aEnable", "#aDisable", "#aGroup"].forEach((s) => ($(s).disabled = !sel.size));
    }
  };
  const reload = async () => { state.clients = null; data = await GET("/api/clients"); draw(); };
  $("#aSearch").addEventListener("input", (e) => { q = e.target.value.trim().toLowerCase(); draw(); });
  $("#aBroker").addEventListener("change", (e) => { br = e.target.value; draw(); });
  $("#aAll").addEventListener("change", (e) => { shown().forEach((c) => c.accounts.forEach((a) => (e.target.checked ? sel.add(a.account_id) : sel.delete(a.account_id)))); draw(); });
  $("#aList").addEventListener("change", async (e) => {
    const t = e.target;
    if (t.dataset.sel) { t.checked ? sel.add(t.dataset.sel) : sel.delete(t.dataset.sel); draw(); }
    else if (t.dataset.client) { const c = data.find((x) => String(x.client_id) === t.dataset.client); c.accounts.forEach((a) => (t.checked ? sel.add(a.account_id) : sel.delete(a.account_id))); draw(); }
    else if (t.dataset.mon) {
      try { await api("POST", `/api/monitoring/accounts/${encodeURIComponent(t.dataset.mon)}/${t.checked ? "enable" : "disable"}`); toast(t.checked ? "Monitoring on" : "Monitoring off", "ok"); await reload(); }
      catch (ex) { t.checked = !t.checked; toast(ex.message, "error"); }
    }
  });
  $("#aList").addEventListener("click", (e) => { const b = e.target.closest("[data-detail]"); if (b) openAccount(b.dataset.detail); });
  if (admin) {
    const bulk = async (enabled, btn) => { await busy(btn, () => api("POST", "/api/monitoring/accounts/select", { account_ids: [...sel], enabled })); toast(`${sel.size} account(s) updated`, "ok"); sel.clear(); await reload(); };
    $("#aEnable").addEventListener("click", (e) => bulk(true, e.currentTarget));
    $("#aDisable").addEventListener("click", (e) => bulk(false, e.currentTarget));
    $("#aGroup").addEventListener("click", () => {
      if (!groups.length) { toast("Create a group first (Groups page).", "error"); return; }
      const m = openModal({ title: `Add ${sel.size} account(s) to a group`, body: `<div class="field"><label for="gPick">Group</label><select class="select" id="gPick">${groups.map((g) => `<option value="${g.id}">${esc(g.name)} (${g.account_ids.length})</option>`).join("")}</select></div>`,
        foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="gAddOk">Add to group</button>` });
      $("#gAddOk", m.el).addEventListener("click", async (e) => {
        const g = groups.find((x) => String(x.id) === $("#gPick", m.el).value);
        const body = groupBody(g, { account_ids: [...new Set([...g.account_ids, ...sel])] });
        await busy(e.currentTarget, () => api("PUT", `/api/groups/${g.id}`, body));
        m.close(); toast(`Added to ${g.name}`, "ok"); sel.clear(); await reload();
      });
    });
  }
  draw();
};
function groupBody(g, over = {}) {
  return { name: g.name, description: g.description, enabled: !!g.enabled, color: g.color, delivery_mode: g.delivery_mode, batch_minutes: g.batch_minutes,
    digest_interval_min: g.digest_interval_min, digest_times: g.digest_times, position_interval_min: g.position_interval_min, eod_summary: !!g.eod_summary,
    allow_queries: !!g.allow_queries, account_ids: g.account_ids, channel_ids: g.channels.map((c) => c.id), rules: g.rules, modes: g.modes || [], ...over };
}

/* account drawer */
async function openAccount(id) {
  const d = await GET(`/api/accounts/${encodeURIComponent(id)}`);
  const a = d.account, c = d.orders_today || {};
  const m = openModal({ title: `${a.client_name} · ${broker(a.broker)} ${a.masked_account}`, drawer: true, body: `
    <div class="kpis"><div class="kpi"><div class="k-label">Orders today</div><div class="k-value">${n0(c.total || 0)}</div><div class="k-sub">${c.filled || 0} filled · ${c.rejected || 0} rejected</div></div>
      <div class="kpi"><div class="k-label">Net P&amp;L today</div><div class="k-value">${inr(d.pnl.net)}</div><div class="k-sub">${d.pnl.open_legs} open legs</div></div>
      <div class="kpi"><div class="k-label">Monitoring</div><div class="k-value" style="font-size:18px">${a.monitored ? (a.muted ? "Muted" : "On") : "Off"}</div><div class="k-sub">${d.groups.map((g) => esc(g.name)).join(", ") || "no groups"}</div></div></div>
    <div class="tabs" role="tablist">${["Overview", "Positions", "Orders", "Activity", "Notifications"].map((t, i) => `<button role="tab" data-tab="${i}" aria-selected="${i === 0}">${t}</button>`).join("")}</div>
    <div id="accTab"></div>` });
  const tabs = [
    () => `<div class="card"><div class="card-b"><dl class="kv"><dt>Client</dt><dd>${esc(a.client_name)} (${esc(a.username)})</dd><dt>Manager</dt><dd>${esc(a.manager_name || "–")}</dd>
      <dt>Broker</dt><dd>${esc(broker(a.broker))} · auth ${esc(a.auth_method)}</dd><dt>Account</dt><dd class="mono">${esc(a.masked_account)}</dd><dt>Account status</dt><dd>${badge(a.account_status)}</dd>
      <dt>Broker session</dt><dd>${badge(a.session_status)} <span class="muted">${a.session_updated ? `updated ${ago(a.session_updated)}` : ""}</span></dd>
      <dt>OMS worker</dt><dd>${a.worker_status ? badge(a.worker_status) : "–"} <span class="muted">${a.worker_heartbeat ? `heartbeat ${ago(a.worker_heartbeat)}` : ""}</span></dd>
      <dt>Trading mode</dt><dd>${esc(a.trading_mode || "–")}</dd><dt>Open orders</dt><dd>${n0(a.open_orders)}</dd></dl>
      <p class="hint" style="margin:12px 0 0">Credentials and tokens are never shown or sent to the browser.</p></div></div>
      ${d.alerts.length ? `<div class="card"><div class="card-h"><h3>OMS alerts</h3></div><div class="card-b flush"><div class="table-wrap"><table class="t"><tbody>${d.alerts.map((x) => `<tr><td>${dt(x.created_at)}</td><td>${badge(x.severity === "CRITICAL" ? "ERROR" : "PENDING", x.kind)}</td><td class="wrap">${esc(x.message)}</td></tr>`).join("")}</tbody></table></div></div></div>` : ""}
      <div class="card"><div class="card-h"><h3>Webhook health</h3></div><div class="card-b flush">${d.webhook_health.length ? `<div class="table-wrap"><table class="t"><tbody>${d.webhook_health.map((w) => `<tr><td>${dt(w.at)}</td><td>${esc(broker(w.broker))}</td><td>${badge(w.outcome)}</td><td class="wrap">${esc(w.detail)}</td></tr>`).join("")}</tbody></table></div>` : empty("No webhooks for this account", "Events arrive through the OMS stream; broker webhooks are optional.", "zap")}</div></div>`,
    () => d.positions.length ? `<div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>Symbol</th><th>Dir</th><th class="num">Open</th><th class="num">Avg</th><th class="num">LTP</th><th class="num">Unrealised</th></tr></thead><tbody>${d.positions.map((p) => `<tr><td><b>${esc(p.symbol)}</b></td><td>${side(p.direction)}</td><td class="num">${n0(p.open_quantity)}</td><td class="num">${n2(p.average_entry_price)}</td><td class="num">${n2(p.ltp)}</td><td class="num">${inr(p.unrealized_pnl)}</td></tr>`).join("")}</tbody></table></div></div>` : `<div class="card">${empty("No open positions")}</div>`,
    () => d.orders.length ? `<div class="card"><div class="table-wrap"><table class="t"><thead><tr><th>Created</th><th>Symbol</th><th>Side</th><th class="num">Filled</th><th>Status</th></tr></thead><tbody>${d.orders.map((o) => `<tr class="clickable" data-order="${esc(o.internal_order_id)}"><td>${dt(o.created_at)}</td><td><b>${esc(o.symbol)}</b></td><td>${side(o.side)}</td><td class="num">${n0(o.filled_quantity)}/${n0(o.ordered_quantity)}</td><td>${badge(o.status)}</td></tr>`).join("")}</tbody></table></div></div>` : `<div class="card">${empty("No orders")}</div>`,
    () => `<div class="card"><div class="feed">${d.events.length ? d.events.map((e) => feedItem(e)).join("") : empty("No activity recorded")}</div></div>`,
    () => `<div class="card"><div class="card-h"><h3>Event rules for this account</h3><span class="sub">Account setting overrides client, group and global</span></div><div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Event</th><th>Effective</th><th>Decided by</th><th>Account override</th></tr></thead><tbody>
      ${Object.entries(d.notification_rules).map(([et, r]) => `<tr><td>${typeBadge(et)}</td><td>${r.enabled ? badge("ACTIVE", "Send") : badge("", "Don't send")}</td><td class="text-2">${esc(r.decided_by)}</td>
        <td>${triState(`acc-${et}`, r.account_override, !isAdmin(), et)}</td></tr>`).join("")}</tbody></table></div></div></div>
      ${isAdmin() ? `<div class="row"><label class="switch"><input type="checkbox" id="muteSw" ${a.muted ? "checked" : ""}><span class="track"></span><span>Mute all notifications for this account</span></label></div>` : ""}`,
  ];
  const show = (i) => {
    $$("[role=tab]", m.el).forEach((t) => t.setAttribute("aria-selected", t.dataset.tab == i));
    $("#accTab", m.el).innerHTML = tabs[i]();
    $$("[data-order]", m.el).forEach((r) => r.addEventListener("click", () => openOrder(r.dataset.order)));
    if (i === 4) wireTri($("#accTab", m.el), async (et, v) => { await api("PUT", "/api/notifications/settings", { scope: "account", scope_id: id, rules: { [et]: v } }); toast("Rule saved", "ok"); });
    const mute = $("#muteSw", m.el);
    if (mute) mute.addEventListener("change", async () => { await api("POST", `/api/monitoring/accounts/${encodeURIComponent(id)}/mute`, { muted: mute.checked }); toast(mute.checked ? "Muted" : "Unmuted", "ok"); });
  };
  $$("[role=tab]", m.el).forEach((t) => t.addEventListener("click", () => show(+t.dataset.tab)));
  show(0);
}
function triState(name, value, disabled, key) {
  const v = value === true ? "on" : value === false ? "off" : "inherit";
  return `<span class="tri" role="group" data-key="${esc(key)}" aria-label="${esc(key)}">${[["inherit", "Inherit"], ["on", "On"], ["off", "Off"]].map(([k, l]) =>
    `<button type="button" data-v="${k}" aria-pressed="${v === k}" ${disabled ? "disabled" : ""}>${l}</button>`).join("")}</span>`;
}
function wireTri(root, onChange) {
  root.addEventListener("click", async (e) => {
    const b = e.target.closest(".tri button"); if (!b || b.disabled) return;
    const grp = b.closest(".tri"); const prev = $("[aria-pressed=true]", grp);
    $$("button", grp).forEach((x) => x.setAttribute("aria-pressed", x === b));
    const v = b.dataset.v === "on" ? true : b.dataset.v === "off" ? false : null;
    try { await onChange(grp.dataset.key, v); } catch (ex) { $$("button", grp).forEach((x) => x.setAttribute("aria-pressed", x === prev)); toast(ex.message, "error"); }
  });
}
const readTri = (root) => Object.fromEntries($$(".tri", root).map((g) => { const v = $("[aria-pressed=true]", g)?.dataset.v; return [g.dataset.key, v === "on" ? true : v === "off" ? false : null]; }));

/* ============================== Groups ============================== */
PAGES.groups = async (root) => {
  const admin = isAdmin();
  const load = async () => {
    const [groups, tg] = await Promise.all([GET("/api/groups"), GET("/api/telegram")]);
    root.innerHTML = `<div class="row"><p class="muted" style="margin:0;flex:1">A group bundles accounts (any brokers) with the Telegram chats that receive their updates, its own event rules and its reporting schedule.</p>
      ${admin ? `<button class="btn primary" id="gNew">${icon("plus", 14)} New group</button>` : ""}</div>
      <div class="grid g-3" id="gList">${groups.length ? groups.map((g) => `<div class="card"><div class="card-h"><span class="chip"><span class="sw" style="background:${esc(g.color)}"></span></span><h3>${esc(g.name)}</h3>
          <div class="actions">${g.enabled ? badge("ACTIVE", "Enabled") : badge("", "Paused")}</div></div>
        <div class="card-b stack">${g.description ? `<div class="text-2">${esc(g.description)}</div>` : ""}
          <dl class="kv"><dt>Accounts</dt><dd>${g.account_ids.length}</dd><dt>Chats</dt><dd>${g.channels.map((c) => `${esc(c.title || c.chat_id)} ${c.status !== "ACTIVE" ? badge(c.status) : ""}`).join(", ") || '<span class="down">none – nothing will be sent</span>'}</dd>
          <dt>Trading modes</dt><dd>${(g.modes && g.modes.length) ? g.modes.map((m) => `<span class="badge ${m === "LIVE" ? "up" : "violet"}">${esc(m)}</span>`).join(" ") : '<span class="muted">global setting</span>'}</dd>
          <dt>Delivery</dt><dd>${g.delivery_mode === "batched" ? `Batched every ${g.batch_minutes} min` : "Instant"}</dd>
          <dt>Reports</dt><dd>${[g.digest_interval_min ? `summary every ${g.digest_interval_min}m` : "", g.digest_times.length ? `at ${g.digest_times.join(", ")}` : "", g.position_interval_min ? `positions every ${g.position_interval_min}m` : "", g.eod_summary ? "EOD summary" : ""].filter(Boolean).join(" · ") || "none"}</dd>
          <dt>Questions</dt><dd>${g.allow_queries ? "Members may ask the bot" : "Off"}</dd><dt>Rule overrides</dt><dd>${Object.keys(g.rules).length}</dd></dl>
          ${admin ? `<div class="row"><button class="btn sm" data-edit="${g.id}">${icon("edit", 12)} Edit</button><button class="btn sm" data-send="${g.id}">${icon("send", 12)} Send summary now</button><button class="btn sm danger" data-del="${g.id}">${icon("trash", 12)} Delete</button></div>` : ""}
        </div></div>`).join("") : `<div class="card" style="grid-column:1/-1">${empty("No groups yet", admin ? "Create a group such as “VIP Clients” or “Mathew Accounts”, add accounts and attach a Telegram chat." : "An administrator hasn't created any groups you can see.", "folder")}</div>`}</div>`;
    if (!admin) return;
    $("#gNew").addEventListener("click", () => editGroup(null, tg.chats || [], load));
    $$("[data-edit]", root).forEach((b) => b.addEventListener("click", () => editGroup(groups.find((g) => g.id == b.dataset.edit), tg.chats || [], load)));
    $$("[data-send]", root).forEach((b) => b.addEventListener("click", async () => { const r = await busy(b, () => api("POST", `/api/groups/${b.dataset.send}/send-now?kind=digest`)); toast(r.queued ? `Summary queued to ${r.queued} chat(s)` : "No active chat attached", r.queued ? "ok" : "error"); }));
    $$("[data-del]", root).forEach((b) => b.addEventListener("click", async () => {
      const g = groups.find((x) => x.id == b.dataset.del);
      if (await confirmDialog("Delete group", `Delete <b>${esc(g.name)}</b>? Its chats stop receiving updates for these accounts. Accounts and chats themselves are kept.`, "Delete group", true)) {
        await api("DELETE", `/api/groups/${g.id}`); toast("Group deleted", "ok"); load();
      }
    }));
  };
  await load();
};
async function editGroup(g, chats, done) {
  const accts = await allAccounts();
  const nm = g ? groupBody(g) : { name: "", description: "", enabled: true, color: "#4c8dff", delivery_mode: "instant", batch_minutes: 5, digest_interval_min: 0, digest_times: [], position_interval_min: 0, eod_summary: true, allow_queries: true, account_ids: [], channel_ids: [], rules: {}, modes: ["LIVE"] };
  const sel = new Set(nm.account_ids), chs = new Set(nm.channel_ids);
  const byClient = {};
  accts.forEach((a) => (byClient[a.client_name] = byClient[a.client_name] || []).push(a));
  const m = openModal({ title: g ? `Edit ${g.name}` : "New monitoring group", wide: true, body: `
    <div class="grid g-2"><div class="field"><label for="gName">Name</label><input class="input" id="gName" maxlength="80" value="${esc(nm.name)}" placeholder="e.g. VIP Clients"><span class="error-text" id="gNameErr"></span></div>
      <div class="field"><label for="gDesc">Description</label><input class="input" id="gDesc" maxlength="500" value="${esc(nm.description)}"></div></div>
    <div class="row"><label class="switch"><input type="checkbox" id="gEnabled" ${nm.enabled ? "checked" : ""}><span class="track"></span><span>Group enabled</span></label>
      <label class="row" style="gap:6px;font-size:12px">Colour <input type="color" id="gColor" value="${esc(nm.color)}" aria-label="Group colour"></label>
      <label class="switch"><input type="checkbox" id="gQueries" ${nm.allow_queries ? "checked" : ""}><span class="track"></span><span>Members can ask the bot about these accounts</span></label></div>
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h3>Accounts</h3><span class="sub" id="gAccCount"></span><div class="actions"><div class="search">${icon("search", 14)}<input class="input" id="gAccQ" placeholder="Filter" aria-label="Filter accounts" style="width:140px"></div></div></div>
        <div class="card-b" style="max-height:300px;overflow:auto" id="gAccList">${Object.entries(byClient).map(([cn, list]) => `<div class="stack" style="gap:6px;margin-bottom:10px" data-cn="${esc(cn.toLowerCase())}">
          <label class="row" style="gap:8px;font-weight:600"><input type="checkbox" class="check" data-gc="${esc(cn)}" ${list.every((a) => sel.has(a.account_id)) ? "checked" : ""}> ${esc(cn)}</label>
          ${list.map((a) => `<label class="row" style="gap:8px;padding-left:24px;font-size:12px"><input type="checkbox" class="check" data-ga="${esc(a.account_id)}" ${sel.has(a.account_id) ? "checked" : ""}> ${esc(broker(a.broker))} <span class="mono">${esc(a.masked_account)}</span> ${a.monitored ? "" : '<span class="badge">not monitored</span>'}</label>`).join("")}</div>`).join("")}</div></div>
      <div class="card"><div class="card-h"><h3>Telegram chats</h3><span class="sub">receive this group's updates</span></div><div class="card-b stack" style="max-height:300px;overflow:auto">
        ${chats.filter((c) => c.status !== "DISABLED").length ? chats.filter((c) => c.status !== "DISABLED").map((c) => `<label class="row" style="gap:8px"><input type="checkbox" class="check" data-ch="${c.id}" ${chs.has(c.id) ? "checked" : ""}> ${esc(c.title || c.chat_id)} <span class="muted mono" style="font-size:11px">${esc(c.chat_id)}</span> ${badge(c.status)}</label>`).join("")
          : `<div class="callout warn">No chats yet. Add the TradePulse bot to a Telegram group, then activate it on the Telegram page.</div>`}</div></div></div>
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h3>Delivery</h3></div><div class="card-b stack">
        <div class="seg" role="group" aria-label="Delivery mode"><button type="button" data-dm="instant" aria-pressed="${nm.delivery_mode === "instant"}">Instant</button><button type="button" data-dm="batched" aria-pressed="${nm.delivery_mode === "batched"}">Batched</button></div>
        <div class="field"><span class="label">Trading modes sent to this group</span><div class="row">${["LIVE", "PAPER", "DRY_RUN"].map((md) => `<label class="row" style="gap:6px"><input type="checkbox" class="check" data-gmode="${md}" ${(nm.modes || []).includes(md) ? "checked" : ""}> ${md === "LIVE" ? "LIVE (real)" : md}</label>`).join("")}</div>
          <span class="hint">None ticked = follow the global setting (Notifications page). A “live trades” group should tick LIVE only.</span></div>
        <div class="field"><label for="gBatch">Batch window (minutes)</label><input class="input" type="number" min="1" max="240" id="gBatch" value="${nm.batch_minutes}"><span class="hint">Batched: updates are combined into one message per window.</span></div></div></div>
      <div class="card"><div class="card-h"><h3>Reporting schedule</h3><span class="sub">IST, trading days</span></div><div class="card-b grid g-2">
        <div class="field"><label for="gDig">Summary every (min)</label><input class="input" type="number" min="0" max="1440" id="gDig" value="${nm.digest_interval_min}"><span class="hint">0 = off · market hours only</span></div>
        <div class="field"><label for="gPos">Positions report every (min)</label><input class="input" type="number" min="0" max="1440" id="gPos" value="${nm.position_interval_min}"><span class="hint">0 = off</span></div>
        <div class="field"><label for="gTimes">Summary at times</label><input class="input" id="gTimes" value="${esc(nm.digest_times.join(", "))}" placeholder="09:30, 12:00"><span class="error-text" id="gTimesErr"></span></div>
        <div class="field"><label class="switch" style="margin-top:24px"><input type="checkbox" id="gEod" ${nm.eod_summary ? "checked" : ""}><span class="track"></span><span>End-of-day summary</span></label></div></div></div></div>
    <div class="card"><div class="card-h"><h3>Event rules for this group</h3><span class="sub">Inherit = global setting. Account/client settings still take precedence.</span></div>
      <div class="card-b flush"><div class="table-wrap"><table class="t"><tbody id="gRules">${state.meta.event_types.map((t) => `<tr><td>${typeBadge(t.type)} <span class="text-2">${esc(t.label)}</span></td><td>${triState("", nm.rules[t.type], false, t.type)}</td></tr>`).join("")}</tbody></table></div></div></div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="gSave">${g ? "Save changes" : "Create group"}</button>` });
  const el = m.el;
  const count = () => ($("#gAccCount", el).textContent = `${sel.size} selected`);
  count();
  let dm = nm.delivery_mode;
  $$("[data-dm]", el).forEach((b) => b.addEventListener("click", () => { dm = b.dataset.dm; $$("[data-dm]", el).forEach((x) => x.setAttribute("aria-pressed", x === b)); }));
  $("#gAccQ", el).addEventListener("input", (e) => { const q = e.target.value.toLowerCase(); $$("[data-cn]", el).forEach((d) => (d.hidden = q && !d.textContent.toLowerCase().includes(q))); });
  $("#gAccList", el).addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.ga) t.checked ? sel.add(t.dataset.ga) : sel.delete(t.dataset.ga);
    if (t.dataset.gc) (byClient[t.dataset.gc] || []).forEach((a) => { t.checked ? sel.add(a.account_id) : sel.delete(a.account_id); const cb = $(`[data-ga="${CSS.escape(a.account_id)}"]`, el); if (cb) cb.checked = t.checked; });
    count();
  });
  el.addEventListener("change", (e) => { if (e.target.dataset.ch) e.target.checked ? chs.add(+e.target.dataset.ch) : chs.delete(+e.target.dataset.ch); });
  wireTri($("#gRules", el), async () => {});
  $("#gSave", el).addEventListener("click", async (e) => {
    const name = $("#gName", el).value.trim();
    const times = $("#gTimes", el).value.split(",").map((s) => s.trim()).filter(Boolean);
    $("#gNameErr", el).textContent = name ? "" : "Give the group a name.";
    const badT = times.find((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t));
    $("#gTimesErr", el).textContent = badT ? `“${badT}” must be HH:MM (24h)` : "";
    $("#gName", el).setAttribute("aria-invalid", !name);
    if (!name || badT) return;
    const body = { name, description: $("#gDesc", el).value, enabled: $("#gEnabled", el).checked, color: $("#gColor", el).value, delivery_mode: dm,
      batch_minutes: +$("#gBatch", el).value || 5, digest_interval_min: +$("#gDig", el).value || 0, digest_times: times, position_interval_min: +$("#gPos", el).value || 0,
      eod_summary: $("#gEod", el).checked, allow_queries: $("#gQueries", el).checked, account_ids: [...sel], channel_ids: [...chs], rules: readTri($("#gRules", el)),
      modes: $$("[data-gmode]", el).filter((x) => x.checked).map((x) => x.dataset.gmode) };
    await busy(e.currentTarget, () => api(g ? "PUT" : "POST", g ? `/api/groups/${g.id}` : "/api/groups", body));
    toast(g ? "Group saved" : "Group created", "ok"); state.clients = null; m.close(); done();
  });
}

/* ============================== Telegram ============================== */
PAGES.telegram = async (root) => {
  const admin = isAdmin();
  const load = async () => {
    const [t, me] = await Promise.all([GET("/api/telegram"), GET("/api/auth/me")]);
    const groups = admin ? await GET("/api/groups") : [];
    const bot = t.username ? `@${t.username}` : "the TradePulse bot";
    root.innerHTML = `<div class="grid g-2">
      <div class="card"><div class="card-h"><h2>${icon("bot", 16)} TradePulse bot</h2><div class="actions">${t.configured ? (t.health && t.health.ok === false ? badge("ERROR", "Error") : badge("ACTIVE", "Configured")) : badge("PENDING", "Not configured")}</div></div>
        <div class="card-b stack"><dl class="kv"><dt>Bot</dt><dd>${t.username ? `<a href="https://t.me/${esc(t.username)}" target="_blank" rel="noopener">@${esc(t.username)}</a>` : "–"}</dd><dt>Token</dt><dd>${t.token.set ? `set ${esc(t.token.hint || "")}` : "not set"}${t.from_env ? " (environment)" : ""}</dd>
          <dt>Updates</dt><dd>${esc(t.mode)}${t.health ? ` · last ${ago(t.health.last_update_at || t.health.at)}` : ""}</dd>${t.health && t.health.last_error ? `<dt>Last error</dt><dd class="down">${esc(t.health.last_error)}</dd>` : ""}</dl>
          ${admin && !t.from_env ? `<form class="row" id="tokForm"><input class="input" id="tok" type="password" autocomplete="off" placeholder="BotFather token 123456789:AA…" aria-label="Bot token" style="flex:1"><button class="btn primary">Save token</button></form>
            <span class="hint">Stored encrypted. Create the bot with @BotFather (/newbot → “TradePulse”) and paste its token. Turn off privacy mode only if you want the bot to read every group message - mentions and commands work either way.</span>` : ""}</div></div>
      <div class="card"><div class="card-h"><h2>${icon("link", 16)} Link my Telegram</h2><div class="actions">${me.telegram ? badge("ACTIVE", "Linked") : badge("", "Not linked")}</div></div>
        <div class="card-b stack">${me.telegram ? `<p style="margin:0">Linked to Telegram ${me.telegram.username ? `@${esc(me.telegram.username)}` : esc(me.telegram.telegram_user_id)} since ${dt(me.telegram.linked_at)}. In a private chat with ${esc(bot)} you can ask about every account you can see here.</p>` :
          `<p style="margin:0">Link your Telegram account to chat with ${esc(bot)} privately about the accounts you can see.</p>`}
          <div class="row"><button class="btn primary" id="codeBtn">${me.telegram ? "Link another device" : "Create link code"}</button></div><div id="codeOut"></div></div></div></div>
      ${admin ? `<div class="card"><div class="card-h"><h2>Chats</h2><span class="sub">groups and people the bot posts to</span><div class="actions"><button class="btn" id="addChat">${icon("plus", 14)} Add chat by ID</button></div></div>
        <div class="card-b"><div class="callout">Add ${esc(bot)} to a Telegram group - it appears here as <b>Pending</b>. Activate it, attach it to a monitoring group, then send a test. A linked admin can also type <code class="inline">/register</code> in the group.</div></div>
        <div class="card-b flush"><div class="table-wrap">${t.chats.length ? `<table class="t"><thead><tr><th>Chat</th><th>Chat ID</th><th>Type</th><th>Status</th><th>Enabled</th><th>Monitoring groups</th><th>Last seen</th><th></th></tr></thead><tbody>
          ${t.chats.map((c) => `<tr><td><b>${esc(c.title || "(untitled)")}</b></td><td class="mono">${esc(c.chat_id)}</td><td>${esc(c.chat_type || c.kind)}</td><td>${badge(c.status)}</td>
            <td><label class="switch"><input type="checkbox" data-chen="${c.id}" ${c.enabled ? "checked" : ""} aria-label="Enable ${esc(c.title)}"><span class="track"></span></label></td>
            <td>${c.groups.map((g) => `<span class="chip">${esc(g.name)}</span>`).join(" ") || '<span class="muted">none</span>'}</td><td>${ago(c.last_seen)}</td>
            <td class="row" style="flex-wrap:nowrap">${c.status !== "ACTIVE" ? `<button class="btn sm primary" data-act="${c.id}">Activate</button>` : `<button class="btn sm" data-test="${c.id}">${icon("send", 12)} Test</button>`}
              <button class="btn sm" data-cgroups="${c.id}">Groups</button><button class="btn sm danger" data-rm="${c.id}" aria-label="Remove chat">${icon("trash", 12)}</button></td></tr>`).join("")}</tbody></table>`
          : empty("No chats yet", "Add the bot to a group or add a chat ID manually.", "message")}</div></div></div>
      <div class="card"><div class="card-h"><h2>Linked Telegram users</h2></div><div class="card-b flush"><div class="table-wrap">${t.users.length ? `<table class="t"><thead><tr><th>Telegram</th><th>OMS user</th><th>Linked</th><th>Last seen</th><th></th></tr></thead><tbody>
          ${t.users.map((u) => `<tr><td>${esc(u.first_name)} ${u.username ? `<span class="muted">@${esc(u.username)}</span>` : ""}</td><td>${u.oms_user ? `${esc(u.oms_user.name)} <span class="badge">${esc(u.oms_user.role)}</span>` : '<span class="muted">not linked</span>'}</td>
            <td>${u.linked_at ? dt(u.linked_at) : "–"}</td><td>${ago(u.last_seen)}</td><td>${u.oms_user ? `<button class="btn sm danger" data-unlink="${esc(u.telegram_user_id)}">Unlink</button>` : ""}</td></tr>`).join("")}</tbody></table>`
          : empty("Nobody has messaged the bot yet")}</div></div></div>` : ""}
      <div class="card"><div class="card-h"><h2>Bot commands</h2></div><div class="card-b"><div class="grid g-3" style="gap:6px 16px;font-size:12px">
        ${[["/status", "account status & health"], ["/today", "orders, trades, P&L today"], ["/positions [account]", "open positions"], ["/orders · /pending · /rejected", "today's orders"], ["/trades", "executed trades"], ["/pnl [week|month]", "P&L"], ["/recent", "recent activity"], ["/order <id>", "one order's lifecycle"], ["/holdings [account]", "holdings"], ["/refresh positions|holdings ACCOUNT", "re-read from broker (1 / 3 s)"], ["/link CODE", "link your OMS login (private chat)"], ["/register", "activate a group (linked admin)"], ["/chatid", "show this chat's ID"]].map(([c, d]) => `<div><code class="inline">${esc(c)}</code> <span class="muted">${esc(d)}</span></div>`).join("")}</div></div></div>`;
    $("#codeBtn").addEventListener("click", async (e) => {
      const r = await busy(e.currentTarget, () => api("POST", "/api/telegram/link-code"));
      $("#codeOut").innerHTML = `<div class="callout">Send this to ${r.bot_username ? `<a href="https://t.me/${esc(r.bot_username)}" target="_blank" rel="noopener">@${esc(r.bot_username)}</a>` : "the bot"} in a private chat within 15 minutes:</div>
        <div class="copyable"><code>/link ${esc(r.code)}</code><button class="btn icon" id="cpCode" aria-label="Copy">${icon("copy", 14)}</button></div>`;
      $("#cpCode").addEventListener("click", () => copy(`/link ${r.code}`));
    });
    if (!admin) return;
    const tf = $("#tokForm");
    if (tf) tf.addEventListener("submit", async (e) => { e.preventDefault(); const r = await busy(e.submitter, () => api("PUT", "/api/telegram/token", { token: $("#tok").value.trim() })); toast(r.bot ? `Connected to @${r.bot.username}` : "Token cleared", "ok"); load(); });
    $("#addChat").addEventListener("click", () => {
      const m = openModal({ title: "Add a chat by ID", body: `<div class="field"><label for="cId">Chat ID</label><input class="input" id="cId" placeholder="-1001234567890"><span class="hint">Group IDs are negative. Send /chatid in the group to see it.</span></div>
        <div class="field"><label for="cTitle">Name</label><input class="input" id="cTitle" placeholder="Desk alerts"></div>`, foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="cOk">Add chat</button>` });
      $("#cOk", m.el).addEventListener("click", async (e) => { await busy(e.currentTarget, () => api("POST", "/api/telegram/chats", { chat_id: $("#cId", m.el).value.trim(), title: $("#cTitle", m.el).value.trim() })); m.close(); toast("Chat added", "ok"); load(); });
    });
    $$("[data-act]", root).forEach((b) => b.addEventListener("click", async () => { await busy(b, () => api("PUT", `/api/telegram/chats/${b.dataset.act}`, { status: "ACTIVE", enabled: true })); toast("Chat activated", "ok"); load(); }));
    $$("[data-test]", root).forEach((b) => b.addEventListener("click", async () => { await busy(b, () => api("POST", "/api/notifications/test", { channel_id: +b.dataset.test })); toast("Test message queued", "ok"); }));
    $$("[data-rm]", root).forEach((b) => b.addEventListener("click", async () => { if (await confirmDialog("Remove chat", "Stop sending to this chat and detach it from all groups? Pending messages to it are dropped.", "Remove chat", true)) { await api("DELETE", `/api/telegram/chats/${b.dataset.rm}`); toast("Chat removed", "ok"); load(); } }));
    $$("[data-unlink]", root).forEach((b) => b.addEventListener("click", async () => { if (await confirmDialog("Unlink user", "This Telegram user will no longer see any account data in private chat.", "Unlink", true)) { await api("DELETE", `/api/telegram/users/${b.dataset.unlink}`); load(); } }));
    $$("[data-cgroups]", root).forEach((b) => b.addEventListener("click", () => {
      const c = t.chats.find((x) => x.id == b.dataset.cgroups);
      const have = new Set(c.groups.map((g) => g.id));
      const m = openModal({ title: `Groups for ${c.title || c.chat_id}`, body: groups.length ? groups.map((g) => `<label class="row" style="gap:8px"><input type="checkbox" class="check" data-g="${g.id}" ${have.has(g.id) ? "checked" : ""}> ${esc(g.name)} <span class="muted">${g.account_ids.length} accounts</span></label>`).join("") : empty("No groups yet", "Create one on the Groups page."),
        foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="cgOk">Save</button>` });
      $("#cgOk", m.el).addEventListener("click", async (e) => { await busy(e.currentTarget, () => api("PUT", `/api/telegram/chats/${c.id}`, { group_ids: $$("[data-g]", m.el).filter((x) => x.checked).map((x) => +x.dataset.g) })); m.close(); toast("Saved", "ok"); load(); });
    }));
  };
  if (admin) root.addEventListener("change", async (e) => {
    const id = e.target.dataset.chen;
    if (!id) return;
    try { await api("PUT", `/api/telegram/chats/${id}`, { enabled: e.target.checked }); toast(e.target.checked ? "Chat enabled" : "Chat disabled", "ok"); }
    catch (ex) { e.target.checked = !e.target.checked; toast(ex.message, "error"); }
  });
  await load();
};

/* ============================== Notifications ============================== */
PAGES.notifications = async (root) => {
  const admin = isAdmin();
  const [s, accts, cl] = await Promise.all([GET("/api/notifications/settings"), allAccounts(), clients()]);
  root.innerHTML = `<div class="grid g-2">
    <div class="card"><div class="card-h"><h2>Global rules</h2><span class="sub">house default for every account</span></div>
      <div class="card-b flush"><div class="table-wrap"><table class="t"><tbody id="glob">${s.event_types.map((t) => `<tr><td>${typeBadge(t.type)} <span class="text-2">${esc(t.label)}</span></td><td class="muted" style="font-size:11px">default ${t.default ? "on" : "off"}</td><td>${triState("", s.global[t.type], !admin, t.type)}</td></tr>`).join("")}</tbody></table></div></div></div>
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Precedence</h2></div><div class="card-b stack" style="font-size:13px">
        <div class="row">${["Account", "Client", "Group", "Global", "Default"].map((l, i) => `<span class="badge ${i === 0 ? "accent" : ""}">${i + 1}. ${l}</span>`).join(`<span class="muted">›</span>`)}</div>
        <p class="text-2" style="margin:0">The first level with an explicit <b>On/Off</b> wins. Account and client settings describe the account and apply to every group it's in. Group settings tailor what one audience receives. An event is only sent when the account is <b>monitored</b>, not muted, its trading mode is enabled below, and the group and chat are active.</p></div></div>
      <div class="card"><div class="card-h"><h2>Channels &amp; modes</h2></div><div class="card-b stack">
        <label class="switch"><input type="checkbox" id="tgOn" ${s.settings.telegram_enabled ? "checked" : ""} ${admin ? "" : "disabled"}><span class="track"></span><span>Telegram notifications</span></label>
        <label class="switch"><input type="checkbox" id="waOn" ${s.settings.whatsapp_enabled ? "checked" : ""} ${admin ? "" : "disabled"}><span class="track"></span><span>WhatsApp notifications (optional provider)</span></label>
        <div class="row"><span class="label">Notify for trading modes:</span>${["LIVE", "PAPER", "DRY_RUN"].map((m) => `<label class="row" style="gap:6px"><input type="checkbox" class="check" data-mode="${m}" ${s.settings.notify_modes.includes(m) ? "checked" : ""} ${admin ? "" : "disabled"}> ${m}</label>`).join("")}</div></div></div>
      <div class="card"><div class="card-h"><h2>Why would it be sent?</h2></div><div class="card-b stack">
        <div class="row"><select class="select" id="exAcct" aria-label="Account">${accountOptions(accts, "", "Choose account")}</select>
          <select class="select" id="exType" aria-label="Event">${s.event_types.map((t) => `<option value="${t.type}">${esc(t.label)}</option>`).join("")}</select><button class="btn" id="exBtn">Explain</button></div><div id="exOut"></div></div></div></div>
    <div class="card"><div class="card-h"><h2>Client &amp; account overrides</h2><div class="actions">
      <select class="select" id="ovScope" aria-label="Override level"><option value="client">Client</option><option value="account">Account</option></select>
      <select class="select" id="ovId" aria-label="Which client or account"></select></div></div>
      <div class="card-b flush"><div class="table-wrap"><table class="t"><tbody id="ovRules"></tbody></table></div></div></div>
    <div class="card"><div class="card-h"><h2>Delivery history</h2><div class="actions">
      <div class="seg" role="group" aria-label="Status filter">${[["", "All"], ["SENT", "Sent"], ["QUEUED", "Queued"], ["FAILED", "Retrying"], ["DEAD", "Failed"], ["SKIPPED", "Skipped"]].map(([v, l]) => `<button data-hs="${v}" aria-pressed="${v === ""}">${l}</button>`).join("")}</div>
      ${admin ? `<button class="btn" id="retryDead">${icon("refresh", 14)} Retry all failed</button>` : ""}</div></div>
      <div class="card-b flush"><div class="table-wrap tall" id="hist"></div></div></div>`;
  const putRules = (scope, scope_id, rules) => api("PUT", "/api/notifications/settings", { scope, scope_id, rules });
  wireTri($("#glob"), async (et, v) => { await putRules("global", "", { [et]: v }); toast("Global rule saved", "ok"); });
  const ovFill = () => {
    const scope = $("#ovScope").value;
    $("#ovId").innerHTML = scope === "client" ? cl.map((c) => `<option value="${c.client_id}">${esc(c.name)}</option>`).join("") : accountOptions(accts).replace('<option value="">All accounts</option>', "");
    ovDraw();
  };
  const ovDraw = () => {
    const scope = $("#ovScope").value, id = $("#ovId").value;
    const cur = (scope === "client" ? s.clients[id] : s.accounts[id]) || {};
    const keys = [...s.event_types.map((t) => [t.type, typeBadge(t.type) + ` <span class="text-2">${esc(t.label)}</span>`]), [s.channel_keys.telegram, "<b>Telegram</b> channel"], [s.channel_keys.whatsapp, "<b>WhatsApp</b> channel"]];
    $("#ovRules").innerHTML = keys.map(([k, l]) => `<tr><td>${l}</td><td>${triState("", cur[k], !admin, k)}</td></tr>`).join("");
  };
  wireTri($("#ovRules"), async (et, v) => {
    const scope = $("#ovScope").value, id = $("#ovId").value;
    const r = await putRules(scope, id, { [et]: v });
    (scope === "client" ? s.clients : s.accounts)[id] = r.rules; toast("Override saved", "ok");
  });
  $("#ovScope").addEventListener("change", ovFill); $("#ovId").addEventListener("change", ovDraw); ovFill();
  const saveSetting = async (patch) => { await api("PUT", "/api/settings", patch); toast("Saved", "ok"); };
  if (admin) {
    $("#tgOn").addEventListener("change", (e) => saveSetting({ telegram_enabled: e.target.checked }));
    $("#waOn").addEventListener("change", (e) => saveSetting({ whatsapp_enabled: e.target.checked }));
    $$("[data-mode]", root).forEach((c) => c.addEventListener("change", () => saveSetting({ notify_modes: $$("[data-mode]", root).filter((x) => x.checked).map((x) => x.dataset.mode) })));
  }
  $("#exBtn").addEventListener("click", async () => {
    const a = $("#exAcct").value; if (!a) { toast("Choose an account", "error"); return; }
    const r = await GET(`/api/notifications/explain?${qs({ account_id: a, event_type: $("#exType").value })}`);
    $("#exOut").innerHTML = !r.monitored ? `<div class="callout warn">Not sent: this account isn't monitored. Turn monitoring on (Accounts page).</div>` : r.muted ? `<div class="callout warn">Not sent: the account is muted.</div>`
      : r.groups.length ? r.groups.map((g) => `<div class="row">${g.enabled ? badge("ACTIVE", "Sent") : badge("", "Not sent")} to <b>${esc(g.group)}</b> <span class="muted">decided by ${esc(g.decided_by)} level</span></div>`).join("")
        : `<div class="callout warn">Not sent: the account isn't in any monitoring group.</div>`;
  });
  let hs = "";
  const hist = async () => {
    const rows = await GET(`/api/notifications/history?${qs({ status: hs, limit: 300 })}`);
    $("#hist").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Created</th><th>Kind</th><th>Event</th><th>Client</th><th>Chat</th><th>Group</th><th>Status</th><th class="num">Attempts</th><th>Sent</th><th>Error</th><th></th></tr></thead><tbody>
      ${rows.map((n) => `<tr><td>${dt(n.created_at)}</td><td>${esc(n.kind)}${n.batched ? ' <span class="badge">batched</span>' : ""}</td><td>${n.event_type ? typeBadge(n.event_type) : "–"} ${esc(n.symbol || "")}</td><td>${esc(n.client_name || "–")} <span class="mono muted">${esc(n.masked_account)}</span></td>
        <td>${esc(n.channel)}</td><td>${esc(n.group_name || "–")}</td><td>${badge(n.status)}</td><td class="num">${n.attempts}</td><td>${tm(n.sent_at)}</td><td class="wrap text-2">${esc(n.last_error || "")}</td>
        <td>${admin && ["DEAD", "FAILED", "SKIPPED"].includes(n.status) ? `<button class="btn sm" data-retry="${n.id}">Retry</button>` : ""}</td></tr>`).join("")}</tbody></table>` : empty("Nothing here", "Notifications appear once monitored accounts in active groups produce events.", "bell");
    $$("[data-retry]", root).forEach((b) => b.addEventListener("click", async () => { await busy(b, () => api("POST", `/api/notifications/${b.dataset.retry}/retry`)); toast("Requeued", "ok"); hist(); }));
  };
  $$("[data-hs]", root).forEach((b) => b.addEventListener("click", () => { $$("[data-hs]", root).forEach((x) => x.setAttribute("aria-pressed", x === b)); hs = b.dataset.hs; hist(); }));
  if (admin) $("#retryDead").addEventListener("click", async (e) => { const r = await busy(e.currentTarget, () => api("POST", "/api/notifications/retry-dead")); toast(`${r.requeued} requeued`, "ok"); hist(); refreshDeadBadge(); });
  await hist();
};

/* ============================== System ============================== */
PAGES.system = async (root) => {
  const admin = isAdmin();
  const [h, st, wh] = await Promise.all([GET("/api/system/health"), GET("/api/settings"), GET("/api/webhooks")]);
  const v = st.values;
  const comp = (ok, title, lines, ic) => `<div class="hcell" style="align-items:flex-start"><span class="status-ic ${ok === true ? "ok" : ok === false ? "bad" : ok === "warn" ? "warn" : "off"}">${icon(ic, 14)}</span><div style="min-width:0"><div class="hv">${title}</div>${lines.map((l) => `<div class="hl">${l}</div>`).join("")}</div></div>`;
  const tail = h.redis_tail || {};
  const br = h.brokers || {};
  const rc = h.reconcile || {};
  root.innerHTML = `
    <div class="card"><div class="card-h"><h2>System health</h2><span class="sub">v${esc(h.version)} · ${dt(h.at)}</span><div class="actions"><span class="badge up">${icon("shield", 12)} Redis only · no broker polling</span><button class="btn sm" id="hRefresh">${icon("refresh", 12)} Refresh</button></div></div>
      <div class="card-b"><div class="health">
        ${comp(h.redis.kind === "redis" ? h.redis.ok : "warn", "Event bus", [h.redis.kind === "redis" ? (h.redis.ok ? "Redis connected" : "Redis down") : "in-process (single box / dev)", `ingest pending ${h.ingest.pending}`], "zap")}
        ${comp(tail.redis_ok === false ? false : tail.last_tick ? true : null, "OMS streams (Redis)", [`last message ${ago(tail.last_message_at)}`, `${n0(tail.messages)} messages · ${tail.streams} command streams`, `${tail.roster_accounts} roster accounts · ${tail.errors} errors`], "activity")}
        ${comp(br.errors ? "warn" : true, "Broker reads", [`${n0(br.requests)} requests · 1 per ${br.slot_seconds} s`, `${n0(br.busy_refusals)} “please wait” refusals`, br.last_error ? esc(br.last_error) : `last ${ago(br.last_request_at)}`], "db")}
        ${comp(rc.running ? "warn" : rc.last ? (rc.last.status === "DONE" ? true : "warn") : null, "Reconcile", [rc.running ? `running ${rc.running.done}/${rc.running.total}` : rc.last ? `${esc(rc.last.trigger)} · ${esc(rc.last.status)}` : "no run yet", `scheduled ${(rc.times || []).join(", ")} IST`], "refresh")}
        ${comp(h.processor.failed ? "warn" : true, "Processor", [`${n0(h.processor.processed)} stored · ${n0(h.processor.duplicates)} duplicates dropped`, `${h.processor.failed} failures`], "layers")}
        ${comp((h.notifications.DEAD || 0) ? "warn" : true, "Notifier", [`${n0(h.notifications.SENT || 0)} sent · ${h.notifications.QUEUED || 0} queued`, `${h.notifications.FAILED || 0} retrying · ${h.notifications.DEAD || 0} dead`], "bell")}
        ${comp(h.telegram.last_error ? "warn" : !!h.telegram.username || null, "Telegram bot", [h.telegram.username ? `@${esc(h.telegram.username)}` : "not connected", `${h.telegram.updates} updates · ${h.telegram.replies} replies`, h.telegram.last_error ? esc(h.telegram.last_error) : ""], "send")}
        ${comp(h.vault.generated_key ? "warn" : true, "Secrets", [h.vault.generated_key ? "using generated key – set TP_FERNET_KEY" : "TP_FERNET_KEY set"], "shield")}
      </div></div></div>
    <div class="grid g-2">
      <div class="card"><div class="card-h"><h2>Monitoring frequency</h2><span class="sub">how often TradePulse reads &amp; reports</span></div><form class="card-b stack" id="setForm">
        <div class="callout">TradePulse follows the OMS only through Redis. Broker reads happen only at the reconcile times below or when a user presses Refresh – one request per slot, never faster than every 3 s.</div>
        <div class="grid g-2">
          <div class="field"><label for="sRec">Reconcile times (IST)</label><input class="input" id="sRec" value="${esc((v.reconcile_times || []).join(", "))}" placeholder="09:00, 09:05"><span class="hint">Positions + holdings snapshot of monitored accounts.</span></div>
          <div class="field"><label class="switch" style="margin-top:22px"><input type="checkbox" id="sRecOn" ${v.reconcile_enabled ? "checked" : ""}><span class="track"></span><span>Scheduled reconcile on</span></label></div>
          <div class="field"><label for="sSlot">Broker request spacing (s)</label><input class="input" type="number" min="3" max="60" id="sSlot" value="${v.broker_slot_seconds}"><span class="hint">One positions/holdings request per this many seconds, all accounts and users. Minimum 3.</span></div>
          <div class="field"><label for="sPoll">Stream read wait (ms)</label><input class="input" type="number" min="250" max="60000" step="250" id="sPoll" value="${v.oms_poll_interval_ms}"><span class="hint">Redis XREAD blocks up to this long; new messages are read immediately.</span></div>
          <div class="field"><label class="switch"><input type="checkbox" id="sPos" ${v.track_positions ? "checked" : ""}><span class="track"></span><span>Track position changes</span></label></div>
          <div class="field"><label class="switch"><input type="checkbox" id="sAlerts" ${v.track_alerts ? "checked" : ""}><span class="track"></span><span>Track OMS alerts</span></label></div>
          <div class="field"><label for="sOpen">Market open (IST)</label><input class="input" id="sOpen" value="${esc(v.market_open)}" pattern="[0-2][0-9]:[0-5][0-9]"></div>
          <div class="field"><label for="sClose">Market close (IST)</label><input class="input" id="sClose" value="${esc(v.market_close)}"></div>
          <div class="field"><label for="sEod">End-of-day summary at</label><input class="input" id="sEod" value="${esc(v.eod_summary_time)}"></div>
          <div class="field"><label>Trading days</label><div class="row">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => `<label class="row" style="gap:4px;font-size:12px"><input type="checkbox" class="check" data-day="${i}" ${v.trading_days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div></div>
          <div class="field"><label for="sRetry">Notification retries</label><input class="input" type="number" min="1" max="50" id="sRetry" value="${v.retry_max_attempts}"></div>
          <div class="field"><label for="sBase">Retry backoff base (s)</label><input class="input" type="number" min="1" max="600" id="sBase" value="${v.retry_base_seconds}"><span class="hint">Doubles each attempt, capped at 1h.</span></div>
          <div class="field"><label for="sRet">Keep events (days)</label><input class="input" type="number" min="1" max="3650" id="sRet" value="${v.event_retention_days}"></div></div>
        ${admin ? `<div class="row"><button class="btn primary" type="submit">Save settings</button><span class="error-text" id="setErr"></span></div>` : ""}</form></div>
      <div class="card"><div class="card-h"><h2>Broker webhooks</h2><span class="sub">optional, event-driven</span></div><div class="card-b flush"><div class="table-wrap"><table class="t"><thead><tr><th>Broker</th><th>Support</th><th>Last</th><th>Today</th><th></th></tr></thead><tbody>
        ${wh.map((w) => `<tr><td><b>${esc(broker(w.broker))}</b><div class="hint" style="white-space:normal;max-width:260px">${esc(w.auth)}</div></td><td>${w.supported ? badge("ACTIVE", "Supported") : badge("", "Not offered")}</td>
          <td>${w.last ? `${badge(w.last.outcome)} <div class="hint">${ago(w.last.at)}</div>` : '<span class="muted">never</span>'}</td><td class="text-2" style="font-size:12px">${Object.entries(w.today).map(([k, n]) => `${esc(k.toLowerCase())} ${n}`).join(" · ") || "–"}</td>
          <td>${w.supported ? `<button class="btn sm" data-wh="${esc(w.broker)}">Setup</button>` : ""}</td></tr>`).join("")}</tbody></table></div>
        <div class="card-b"><p class="hint" style="margin:0">5paisa doesn't push order updates; its accounts are covered by the OMS event stream. A broker app has one postback URL – only point it here if the OMS isn't using it.</p></div></div></div></div>
    ${admin ? `<div class="card"><div class="card-h"><h2>Audit log</h2><span class="sub">configuration changes &amp; sign-ins</span></div><div class="card-b flush"><div class="table-wrap tall" id="audit">${skeleton(3)}</div></div></div>` : ""}`;
  $("#hRefresh").addEventListener("click", () => route());
  if (admin) {
    $("#setForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { oms_poll_interval_ms: +$("#sPoll").value, reconcile_enabled: $("#sRecOn").checked, broker_slot_seconds: +$("#sSlot").value,
        reconcile_times: $("#sRec").value.split(",").map((x) => x.trim()).filter(Boolean), track_positions: $("#sPos").checked, track_alerts: $("#sAlerts").checked,
        market_open: $("#sOpen").value.trim(), market_close: $("#sClose").value.trim(), eod_summary_time: $("#sEod").value.trim(),
        trading_days: $$("[data-day]").filter((x) => x.checked).map((x) => +x.dataset.day), retry_max_attempts: +$("#sRetry").value, retry_base_seconds: +$("#sBase").value, event_retention_days: +$("#sRet").value };
      $("#setErr").textContent = "";
      try { await busy(e.submitter, () => api("PUT", "/api/settings", body)); toast("Settings saved", "ok"); } catch (ex) { $("#setErr").textContent = ex.message; }
    });
    $$("[data-wh]", root).forEach((b) => b.addEventListener("click", () => {
      const w = wh.find((x) => x.broker === b.dataset.wh);
      const m = openModal({ title: `${broker(w.broker)} webhook`, body: `<div class="field"><span class="label">Postback URL</span><div class="copyable"><code>${esc(w.url)}</code><button class="btn icon" id="cpUrl" aria-label="Copy URL">${icon("copy", 14)}</button></div></div>
        <div class="callout">${esc(w.auth)}. Events: ${esc(w.events.join(", "))}.</div>
        ${w.broker === "zerodha" ? `<div class="field"><label for="whAcct">Account (optional)</label><input class="input" id="whAcct" placeholder="blank = all Zerodha accounts"><span class="hint">Leave the secret blank to use the account's API secret from the OMS (needs TP_OMS_FERNET_KEY).</span></div>` : ""}
        <div class="field"><label for="whSec">${w.broker === "angel" ? "URL token" : w.broker === "zerodha" ? "Kite API secret" : "Shared HMAC secret"}</label><input class="input" type="password" autocomplete="off" id="whSec"><span class="hint">Stored encrypted; never displayed again.</span></div>`,
        foot: `<button class="btn" data-close>Cancel</button><button class="btn primary" id="whOk">Save secret</button>` });
      $("#cpUrl", m.el).addEventListener("click", () => copy(w.url));
      $("#whOk", m.el).addEventListener("click", async (e) => { await busy(e.currentTarget, () => api("PUT", `/api/webhooks/${w.broker}/secret`, { secret: $("#whSec", m.el).value, account_id: ($("#whAcct", m.el) || {}).value || "" })); m.close(); toast("Secret saved", "ok"); });
    }));
    const au = await GET("/api/audit?limit=200");
    $("#audit").innerHTML = au.length ? `<table class="t"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead><tbody>${au.map((a) => `<tr><td>${dt(a.at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.target)}</td><td class="wrap text-2">${esc(a.detail)}</td></tr>`).join("")}</tbody></table>` : empty("No audit entries");
  } else {
    $$("#setForm input", root).forEach((i) => (i.disabled = true));
  }
};

boot();
