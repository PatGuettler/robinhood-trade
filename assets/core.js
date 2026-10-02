// Shared helpers for the TradeBot paper-trading site (GitHub Pages, no build step).
// Settings (repo + optional GitHub token) live only in this browser's localStorage.

const SETTINGS_KEY = "tradebot.paper.settings";
const DATA_BRANCH = "paper-data";
export const WORKFLOW = "paper-bot.yml";
export const DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file";

// ── Settings ────────────────────────────────────────────────────────────────

function detectRepo() {
  const h = location.hostname;
  if (h.endsWith(".github.io")) {
    const owner = h.split(".")[0];
    const seg = location.pathname.split("/").filter(Boolean)[0];
    const repo = seg && !seg.endsWith(".html") ? seg : `${owner}.github.io`;
    return { owner, repo };
  }
  return { owner: "", repo: "" };
}

// The GitHub token is kept in sessionStorage (cleared when the tab closes) unless the
// user ticks "remember on this device". Note: every <user>.github.io site shares one
// browser origin, so anything in localStorage is readable by the user's other Pages sites.
function readStore(store) {
  try { return JSON.parse(store.getItem(SETTINGS_KEY) || "{}"); } catch { return {}; }
}

export function getSettings() {
  const local = readStore(localStorage), sess = readStore(sessionStorage), d = detectRepo();
  return { owner: local.owner || d.owner, repo: local.repo || d.repo,
           token: sess.token || local.token || "", remember: !!local.token };
}

export function saveSettings({ owner, repo, token, remember }) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ owner, repo, ...(remember && token ? { token } : {}) }));
    sessionStorage.setItem(SETTINGS_KEY, JSON.stringify({ token: token || "" }));
  } catch { /* storage blocked */ }
}

// Dev/offline only: ?data=<url prefix>&config=<url> reads files over plain HTTP.
// Ignored on the public site so a crafted link can't feed the page someone else's data.
const params = new URLSearchParams(location.search);
const IS_LOCAL = ["localhost", "127.0.0.1", ""].includes(location.hostname);
export const LOCAL_DATA = IS_LOCAL ? params.get("data") : null;
export const LOCAL_CONFIG = IS_LOCAL ? params.get("config") : null;
export const DEV_QS = LOCAL_DATA ? location.search : "";

// ── GitHub API ──────────────────────────────────────────────────────────────

export async function gh(path, { method = "GET", body, raw = false, token } = {}) {
  const s = getSettings();
  const t = token ?? s.token;
  const headers = { Accept: raw ? "application/vnd.github.raw+json" : "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28" };
  if (t) headers.Authorization = `Bearer ${t}`;
  if (body) headers["Content-Type"] = "application/json";
  const r = await fetch(`https://api.github.com${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
  if (r.status === 404) return null;
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg += " " + (await r.json()).message; } catch { /* no body */ }
    throw new Error(`GitHub ${method} ${path}: ${msg}`);
  }
  if (r.status === 204) return {};
  return raw ? r.text() : r.json();
}

export const repoPath = () => { const s = getSettings(); return `/repos/${s.owner}/${s.repo}`; };

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = ""; bytes.forEach(b => { bin += String.fromCharCode(b); });
  return btoa(bin);
}

export async function readRepoFile(path, ref) {
  const s = getSettings();
  if (!s.owner || !s.repo) throw new Error("Repository not set — open Setup.");
  if (s.token) return gh(`${repoPath()}/contents/${path}${ref ? `?ref=${ref}` : ""}`, { raw: true });
  const r = await fetch(`https://raw.githubusercontent.com/${s.owner}/${s.repo}/${ref || "HEAD"}/${path}?t=${Date.now()}`, { cache: "no-store" });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`Could not read ${path} (${r.status}). Private repo? Add a token on Setup.`);
  return r.text();
}

export async function writeRepoFile(path, text, message) {
  const existing = await gh(`${repoPath()}/contents/${path}`);
  return gh(`${repoPath()}/contents/${path}`, { method: "PUT", body: {
    message, content: b64encode(text), ...(existing ? { sha: existing.sha } : {}) } });
}

let sodiumPromise = null;
async function sodium() {
  if (!sodiumPromise) {
    sodiumPromise = import("https://cdn.jsdelivr.net/npm/libsodium-wrappers@0.7.15/+esm")
      .then(async m => { const s = m.default || m; await s.ready; return s; });
  }
  return sodiumPromise;
}

// Encrypts with the repo's public key (libsodium sealed box) exactly as GitHub documents.
export async function setSecret(name, value) {
  const key = await gh(`${repoPath()}/actions/secrets/public-key`);
  if (!key) throw new Error("Cannot read the repo's Actions public key — token needs Secrets: read & write.");
  const s = await sodium();
  const sealed = s.crypto_box_seal(s.from_string(value), s.from_base64(key.key, s.base64_variants.ORIGINAL));
  await gh(`${repoPath()}/actions/secrets/${name}`, { method: "PUT", body: {
    encrypted_value: s.to_base64(sealed, s.base64_variants.ORIGINAL), key_id: key.key_id } });
}

export async function listSecretNames() {
  const r = await gh(`${repoPath()}/actions/secrets?per_page=100`);
  return new Set((r?.secrets || []).map(x => x.name));
}

export async function defaultBranch() {
  const r = await gh(repoPath());
  return r?.default_branch || "main";
}

export async function dispatch(inputs) {
  const ref = await defaultBranch();
  await gh(`${repoPath()}/actions/workflows/${WORKFLOW}/dispatches`, { method: "POST", body: { ref, inputs } });
}

export async function recentRuns(n = 10) {
  const r = await gh(`${repoPath()}/actions/workflows/${WORKFLOW}/runs?per_page=${n}`);
  return r?.workflow_runs || [];
}

// ── Config ──────────────────────────────────────────────────────────────────

export async function loadConfig() {
  if (LOCAL_CONFIG) return (await fetch(LOCAL_CONFIG, { cache: "no-store" })).json();
  const text = await readRepoFile("paper_config.json");
  if (!text) throw new Error("paper_config.json not found in the repository.");
  return JSON.parse(text);
}

// ── Google Drive (browser side) ─────────────────────────────────────────────

let driveToken = null;
function loadGis() {
  if (window.google?.accounts?.oauth2) return Promise.resolve();
  return new Promise((res, rej) => {
    const sc = document.createElement("script");
    sc.src = "https://accounts.google.com/gsi/client";
    sc.onload = res; sc.onerror = () => rej(new Error("Could not load Google sign-in"));
    document.head.appendChild(sc);
  });
}

export async function driveAccessToken(clientId, interactive = true) {
  if (driveToken && driveToken.exp > Date.now()) return driveToken.value;
  try {
    const c = JSON.parse(sessionStorage.getItem("tradebot.drive") || "null");
    if (c && c.exp > Date.now()) { driveToken = c; return c.value; }
  } catch { /* ignore */ }
  if (!interactive) return null;
  await loadGis();
  return new Promise((resolve, reject) => {
    const client = google.accounts.oauth2.initTokenClient({
      client_id: clientId, scope: DRIVE_SCOPE,
      callback: (resp) => {
        if (resp.error) return reject(new Error(resp.error_description || resp.error));
        driveToken = { value: resp.access_token, exp: Date.now() + (resp.expires_in - 60) * 1000 };
        try { sessionStorage.setItem("tradebot.drive", JSON.stringify(driveToken)); } catch { /* ignore */ }
        resolve(resp.access_token);
      },
      error_callback: (e) => reject(new Error(e.message || "Google sign-in was closed")),
    });
    client.requestAccessToken();
  });
}

async function driveFiles(cfg, names) {
  const token = await driveAccessToken(cfg.storage.google_client_id);
  const auth = { Authorization: `Bearer ${token}` };
  const q = encodeURIComponent(`'${cfg.storage.drive_folder_id}' in parents and trashed = false`);
  const list = await (await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id,name)&pageSize=200`, { headers: auth })).json();
  if (list.error) throw new Error(`Google Drive: ${list.error.message}`);
  const ids = Object.fromEntries((list.files || []).map(f => [f.name, f.id]));
  const out = {};
  await Promise.all(names.map(async n => {
    out[n] = ids[n] ? await (await fetch(`https://www.googleapis.com/drive/v3/files/${ids[n]}?alt=media`, { headers: auth })).text() : null;
  }));
  return out;
}

// ── Data loading ────────────────────────────────────────────────────────────

export async function loadFiles(cfg, names) {
  if (LOCAL_DATA) {
    const out = {};
    await Promise.all(names.map(async n => {
      const r = await fetch(LOCAL_DATA.replace(/\/?$/, "/") + n, { cache: "no-store" });
      out[n] = r.ok ? await r.text() : null;
    }));
    return out;
  }
  if (cfg.storage?.backend === "drive") return driveFiles(cfg, names);
  const out = {};
  await Promise.all(names.map(async n => { out[n] = await readRepoFile(n, DATA_BRANCH); }));
  return out;
}

export function sourceLabel(cfg) {
  if (LOCAL_DATA) return `local: ${LOCAL_DATA}`;
  return cfg.storage?.backend === "drive" ? "Google Drive" : `GitHub branch “${DATA_BRANCH}”`;
}

// ── CSV ─────────────────────────────────────────────────────────────────────

export function parseCSV(text) {
  if (!text) return [];
  const rows = []; let row = [], field = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') q = false;
      else field += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  const [head, ...body] = rows.filter(r => r.length > 1 || r[0]);
  return body.map(r => Object.fromEntries(head.map((h, i) => [h, r[i] ?? ""])));
}

// ── Formatting ──────────────────────────────────────────────────────────────

export const num = (x) => (x === "" || x == null ? NaN : Number(x));
export const usd = (x, sign = false) => {
  const v = num(x); if (!isFinite(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
  return v < 0 ? `−${s}` : (sign && v > 0 ? `+${s}` : s);
};
export const pct = (x, sign = true) => {
  const v = num(x); if (!isFinite(v)) return "—";
  return `${v < 0 ? "−" : sign && v > 0 ? "+" : ""}${Math.abs(v).toFixed(2)}%`;
};
export const tone = (x) => (num(x) > 0 ? "pos" : num(x) < 0 ? "neg" : "");
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};
export const etDate = (iso) => iso ? iso.slice(0, 10) : "";
export const safeUrl = (u) => (/^https:\/\/github\.com\//.test(String(u)) ? esc(u) : "#");
export const chartUrl = (ticker) => `https://finance.yahoo.com/chart/${encodeURIComponent(ticker)}`;

// ── Trades (independent JS implementation — the Verify page compares it to Python's) ──

export function roundtrips(fills) {
  const byTrade = new Map();
  [...fills].sort((a, b) => num(a.fill_id) - num(b.fill_id)).forEach(f => {
    const t = byTrade.get(f.trade_id) || { trade_id: f.trade_id, ticker: f.ticker };
    if (f.side === "BUY") Object.assign(t, { entry_ts: f.timestamp, entry_price: num(f.price), qty: num(f.qty),
      cost: num(f.amount) + num(f.fee), target: num(f.target), stop: num(f.stop), entry_reason: f.reason, buy: f });
    else Object.assign(t, { exit_ts: f.timestamp, exit_price: num(f.price), exit_reason: f.reason,
      pnl: num(f.amount) - num(f.fee) - (t.cost ?? NaN), recorded_pnl: num(f.realized_pnl), sell: f });
    byTrade.set(f.trade_id, t);
  });
  return [...byTrade.values()].map(t => ({ ...t, closed: t.exit_ts != null, pnl_pct: t.pnl != null ? t.pnl / t.cost * 100 : null }));
}

// ── UI helpers ──────────────────────────────────────────────────────────────

export function nav(active) {
  const links = [["index.html", "Dashboard"], ["setup.html", "Setup"], ["verify.html", "Verify"]];
  const qs = esc(DEV_QS);
  document.getElementById("nav").innerHTML = `
    <a class="brand" href="index.html${qs}">Trade<span>Bot</span> <em>paper</em></a>
    ${links.map(([h, l]) => `<a class="nav-link${active === h ? " active" : ""}" href="${h}${qs}">${l}</a>`).join("")}`;
}

export function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

export function table(cols, rows, empty = "Nothing yet.") {
  if (!rows.length) return `<p class="empty">${esc(empty)}</p>`;
  return `<div class="table-wrap"><table><thead><tr>${cols.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr>${cols.map(c => `<td class="${c.num ? "num" : ""} ${c.cls ? c.cls(r) : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// ── Charts (plain SVG, no library) ──────────────────────────────────────────

const SVGNS = "http://www.w3.org/2000/svg";

function tooltipFor(host) {
  let tip = host.querySelector(".chart-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip"; host.appendChild(tip); }
  return tip;
}

function niceTicks(min, max, n = 4) {
  if (min === max) { min -= 1; max += 1; }
  const step0 = (max - min) / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const out = []; for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}

// points: [{t: iso, v: number}] in time order. X is by index so nights/weekends don't leave gaps.
export function lineChart(host, points, { baseline, fmt = usd, label = "Equity" } = {}) {
  host.innerHTML = "";
  if (points.length < 2) { host.innerHTML = `<p class="empty">Not enough data to chart yet.</p>`; return; }
  const W = host.clientWidth || 600, H = 240, m = { l: 64, r: 12, t: 12, b: 26 };
  const vals = points.map(p => p.v).concat(baseline != null ? [baseline] : []);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const X = i => m.l + (i / (points.length - 1)) * (W - m.l - m.r);
  const Y = v => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `${label} chart`);
  let g = ticks.map(v => `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/>
    <text class="axis" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${esc(fmt(v))}</text>`).join("");
  // day boundaries
  let lastDay = null, lastX = -99;
  points.forEach((p, i) => {
    const d = etDate(p.t);
    if (d !== lastDay) {
      if (lastDay !== null) g += `<line class="daysep" x1="${X(i)}" x2="${X(i)}" y1="${m.t}" y2="${H - m.b}"/>`;
      if (X(i) - lastX > 46) {
        g += `<text class="axis" x="${X(i) + 2}" y="${H - 8}">${new Date(d + "T12:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}</text>`;
        lastX = X(i);
      }
      lastDay = d;
    }
  });
  if (baseline != null) g += `<line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${Y(baseline)}" y2="${Y(baseline)}"/>
    <text class="axis" x="${W - m.r}" y="${Y(baseline) - 5}" text-anchor="end">start ${esc(fmt(baseline))}</text>`;
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
  const up = points[points.length - 1].v >= (baseline ?? points[0].v);
  g += `<path class="line ${up ? "pos" : "neg"}" d="${d}"/>
    <line class="cross" y1="${m.t}" y2="${H - m.b}" visibility="hidden"/>
    <circle class="dot ${up ? "pos" : "neg"}" r="4.5" visibility="hidden"/>
    <rect class="hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}"/>`;
  svg.innerHTML = g;
  host.appendChild(svg);
  const tip = tooltipFor(host), cross = svg.querySelector(".cross"), dot = svg.querySelector(".dot");
  svg.querySelector(".hit").addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect(), sx = (e.clientX - r.left) * (W / r.width);
    const i = Math.max(0, Math.min(points.length - 1, Math.round((sx - m.l) / (W - m.l - m.r) * (points.length - 1))));
    const p = points[i];
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(p.v)); dot.setAttribute("visibility", "visible");
    const chg = baseline != null ? ` <span class="${tone(p.v - baseline)}">${usd(p.v - baseline, true)}</span>` : "";
    tip.innerHTML = `<b>${esc(fmt(p.v))}</b>${chg}<br><span class="muted">${esc(fmtTime(p.t))} ET</span>`;
    tip.style.display = "block";
    const px = X(i) / W * r.width;
    tip.style.left = `${Math.min(px + 12, r.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${Y(p.v) / H * r.height - 44}px`;
  });
  svg.querySelector(".hit").addEventListener("mouseleave", () => {
    tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
  });
}

// bars: [{label, v, note}] — zero baseline, gains up (green) and losses down (red).
export function barChart(host, bars, { fmt = usd } = {}) {
  host.innerHTML = "";
  if (!bars.length) { host.innerHTML = `<p class="empty">No completed days yet.</p>`; return; }
  const W = host.clientWidth || 600, H = 200, m = { l: 64, r: 12, t: 12, b: 26 };
  const ticks = niceTicks(Math.min(0, ...bars.map(b => b.v)), Math.max(0, ...bars.map(b => b.v)));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const Y = v => m.t + (1 - (v - y0) / (y1 - y0)) * (H - m.t - m.b);
  const slot = (W - m.l - m.r) / bars.length, bw = Math.max(4, Math.min(36, slot - 4));
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Daily profit and loss chart");
  let g = ticks.map(v => `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/>
    <text class="axis" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${esc(fmt(v))}</text>`).join("");
  const every = Math.ceil(bars.length / Math.max(1, Math.floor((W - m.l) / 56)));
  bars.forEach((b, i) => {
    const x = m.l + i * slot + (slot - bw) / 2, top = Y(Math.max(b.v, 0)), bot = Y(Math.min(b.v, 0));
    const h = Math.max(1, bot - top), r = Math.min(4, h / 2, bw / 2), cls = b.v >= 0 ? "pos" : "neg";
    // rounded only at the data end, square at the zero baseline
    const path = b.v >= 0
      ? `M${x},${bot} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${bot} Z`
      : `M${x},${top} V${bot - r} Q${x},${bot} ${x + r},${bot} H${x + bw - r} Q${x + bw},${bot} ${x + bw},${bot - r} V${top} Z`;
    g += `<path class="bar ${cls}" d="${path}"/><rect class="hit" data-i="${i}" x="${m.l + i * slot}" y="${m.t}" width="${slot}" height="${H - m.t - m.b}"/>`;
    if (i % every === 0) g += `<text class="axis" x="${x + bw / 2}" y="${H - 8}" text-anchor="middle">${esc(b.label)}</text>`;
  });
  g += `<line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  svg.innerHTML = g;
  host.appendChild(svg);
  const tip = tooltipFor(host);
  svg.querySelectorAll(".hit").forEach(el => {
    el.addEventListener("mousemove", () => {
      const b = bars[+el.dataset.i], r = svg.getBoundingClientRect();
      tip.innerHTML = `<b class="${tone(b.v)}">${esc(fmt(b.v))}</b><br><span class="muted">${esc(b.note || b.label)}</span>`;
      tip.style.display = "block";
      const cx = (+el.getAttribute("x") + slot / 2) / W * r.width;
      tip.style.left = `${Math.max(0, Math.min(cx - tip.offsetWidth / 2, r.width - tip.offsetWidth))}px`;
      tip.style.top = `0px`;
    });
    el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  });
}
