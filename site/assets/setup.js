// OAuth popup return: hand the code back to the opener and close.
{
  const q = new URLSearchParams(location.search);
  if ((q.get("code") || q.get("error")) && q.get("state") && window.opener) {
    window.opener.postMessage({ type: "tradebot-google", code: q.get("code"), error: q.get("error"), state: q.get("state") }, location.origin);
    window.close();
  }
}

import {
  nav, getSettings, saveSettings, gh, repoPath, loadConfig, writeRepoFile, readRepoFile, setSecret,
  listSecretNames, dispatch, recentRuns, driveAccessToken, toast, esc, safeUrl, fmtTime, DRIVE_SCOPE, WORKFLOW,
} from "./core.js";

nav("setup.html");
const $ = (id) => document.getElementById(id);
const form = $("cfg-form");
let cfg = null;
let secrets = new Set();

// ── form <-> config ─────────────────────────────────────────────────────────

const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
function setPath(o, p, v) { const ks = p.split("."); ks.slice(0, -1).reduce((a, k) => (a[k] ||= {}), o)[ks.at(-1)] = v; }

function fillForm() {
  form.querySelectorAll("[data-path]").forEach(el => {
    const v = getPath(cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.type === "radio") el.checked = (v || "github") === el.value;
    else if (el.dataset.type === "list") el.value = (v || []).join(", ");
    else if (el.dataset.scale) el.value = v == null ? "" : Number(v) / Number(el.dataset.scale);
    else el.value = v ?? "";
  });
  $("acct-id").textContent = `live account #${cfg.account.id}`;
  $("bt-cash").value = cfg.account.starting_cash;
  folderLink();
  driveVisibility();
  universeVisibility();
}

function universeVisibility() {
  const scan = $("universe").value === "market";
  $("scan-fields").style.display = scan ? "" : "none";
  $("watchlist-label").textContent = scan ? "Always include (optional, comma-separated tickers)" : "Watchlist (comma-separated tickers)";
}

function readForm() {
  const out = structuredClone(cfg);
  form.querySelectorAll("[data-path]").forEach(el => {
    let v;
    if (el.type === "checkbox") v = el.checked;
    else if (el.type === "radio") { if (!el.checked) return; v = el.value; }
    else if (el.dataset.type === "list") v = el.value.split(/[\s,]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
    else if (el.dataset.scale) v = Number(el.value) * Number(el.dataset.scale);
    else if (el.type === "number" || el.dataset.type === "number") v = Number(el.value);
    else v = el.value.trim();
    setPath(out, el.dataset.path, v);
  });
  return out;
}

function validate(c) {
  const s = c.strategy, errs = [];
  if (!(c.account.starting_cash >= 100)) errs.push("Starting cash must be at least $100.");
  if (!(s.position_size > 0)) errs.push("Dollars per trade must be positive.");
  if (s.position_size > c.account.starting_cash) errs.push("Dollars per trade is more than the starting cash.");
  if (s.universe === "watchlist" && !s.watchlist.length) errs.push("Add at least one ticker to the watchlist, or let the bot scan the market.");
  if (s.watchlist.some(t => !/^[A-Z.\-]{1,10}$/.test(t))) errs.push("Watchlist has an invalid ticker.");
  if (c.storage.backend === "drive" && !c.storage.drive_folder_id) errs.push("Connect Google Drive first (no folder ID yet).");
  if (s.sell_mode === "claude" && !s.use_claude) errs.push("“Let Claude pick” the target needs Claude review switched on.");
  return errs;
}

function driveVisibility() {
  const drive = form.querySelector('input[name=backend][value=drive]').checked;
  $("drive-setup").style.opacity = drive ? "1" : ".55";
}

function folderLink() {
  const id = $("g-folder").value.trim();
  $("g-folder-link").innerHTML = id ? `<a href="https://drive.google.com/drive/folders/${encodeURIComponent(id)}" target="_blank" rel="noopener">Open the folder in Google Drive</a>` : "";
}

// ── status ──────────────────────────────────────────────────────────────────

async function refreshStatus() {
  const s = getSettings();
  if (!s.owner || !s.repo) return;
  const items = [];
  const ok = (good, text, fix = "") => items.push(`<div class="check-row"><div class="icon ${good ? "pos" : "neg"}">${good ? "✓" : "✗"}</div><div>${text}${!good && fix ? `<div class="muted">${fix}</div>` : ""}</div></div>`);
  try {
    const repo = await gh(repoPath());
    ok(!!repo, `Repository <b>${esc(s.owner)}/${esc(s.repo)}</b> found${repo?.private ? " (private)" : ""}`, "Check owner / repo name, or add a token for a private repo.");
    if (!repo) { $("status").innerHTML = items.join(""); return; }
    ok(!!cfg, "Settings file <code>paper_config.json</code> found", "Merge the paper-trading branch into the default branch.");
    if (s.token) {
      const wf = await gh(`${repoPath()}/actions/workflows/${WORKFLOW}`).catch(() => null);
      ok(wf?.state === "active", `Bot workflow on <b>${esc(repo.default_branch)}</b> is ${esc(wf?.state || "missing")}`,
        wf ? "Enable it in the Actions tab." : "Merge the paper-trading branch into the default branch — GitHub only runs schedules from there.");
      try {
        secrets = await listSecretNames();
        const need = [];
        if (cfg?.storage?.backend === "drive") need.push("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN");
        if (cfg?.strategy?.use_claude) need.push("ANTHROPIC_API_KEY");
        const missing = need.filter(n => !secrets.has(n));
        ok(!missing.length, `Secrets saved: ${[...secrets].map(esc).join(", ") || "none"}`, `Missing: ${missing.join(", ")}`);
      } catch (e) { ok(false, "Could not list secrets", esc(e.message)); }
      const runs = await recentRuns(1).catch(() => []);
      if (runs[0]) ok(runs[0].conclusion !== "failure", `Last bot run: <a href="${safeUrl(runs[0].html_url)}" target="_blank" rel="noopener">${esc(runs[0].conclusion || runs[0].status)}</a> · ${esc(fmtTime(runs[0].created_at))}`, "Open the run to see the error.");
    } else {
      ok(false, "No GitHub token saved", "Add a token in step 1 to save settings and start runs from this page.");
    }
  } catch (e) {
    ok(false, "GitHub check failed", esc(e.message));
  }
  $("status").innerHTML = items.join("");
  $("claude-badge").innerHTML = secrets.has("ANTHROPIC_API_KEY") ? `<span class="badge green">key saved</span>` : "";
  $("storage-badge").innerHTML = cfg?.storage?.backend === "drive"
    ? (secrets.has("GOOGLE_REFRESH_TOKEN") ? `<span class="badge green">Google Drive connected</span>` : `<span class="badge yellow">Drive not connected</span>`)
    : `<span class="badge gray">GitHub branch</span>`;
}

async function refreshRuns() {
  if (!getSettings().token) return;
  try {
    const runs = await recentRuns(8);
    $("runs").innerHTML = runs.length ? `<div class="table-wrap"><table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th></th></tr></thead><tbody>${
      runs.map(r => `<tr><td>${esc(fmtTime(r.created_at))}</td><td>${esc(r.event)}${r.display_title ? ` · ${esc(r.display_title)}` : ""}</td>
        <td><span class="badge ${r.conclusion === "success" ? "green" : r.conclusion === "failure" ? "red" : "yellow"}">${esc(r.conclusion || r.status)}</span></td>
        <td><a href="${safeUrl(r.html_url)}" target="_blank" rel="noopener">logs</a></td></tr>`).join("")}</tbody></table></div>` : `<p class="empty">No runs yet.</p>`;
    return runs;
  } catch (e) { $("runs").innerHTML = `<p class="empty">${esc(e.message)}</p>`; return []; }
}

let pollTimer;
function pollRuns() {
  clearInterval(pollTimer);
  let n = 0;
  pollTimer = setInterval(async () => {
    const runs = await refreshRuns();
    if (++n > 30 || (n > 2 && runs?.every(r => r.status === "completed"))) clearInterval(pollTimer);
  }, 8000);
}

// ── load ────────────────────────────────────────────────────────────────────

async function init() {
  const s = getSettings();
  $("gh-owner").value = s.owner; $("gh-repo").value = s.repo; $("gh-token").value = s.token;
  $("gh-remember").checked = s.remember;
  $("origin").value = location.origin;
  $("redirect").value = location.origin + location.pathname;
  $("gh-badge").innerHTML = s.token ? `<span class="badge green">token saved</span>` : `<span class="badge gray">no token</span>`;
  try { cfg = await loadConfig(); fillForm(); }
  catch (e) { toast(e.message, "err"); }
  refreshStatus();
  refreshRuns();
}

// ── events ──────────────────────────────────────────────────────────────────

$("gh-save").addEventListener("click", async () => {
  saveSettings({ owner: $("gh-owner").value.trim(), repo: $("gh-repo").value.trim(),
                 token: $("gh-token").value.trim(), remember: $("gh-remember").checked });
  try {
    if ($("gh-token").value.trim()) {
      await gh(`${repoPath()}/actions/secrets/public-key`);
      await gh(`${repoPath()}/actions/workflows`);
    }
    toast("GitHub connected ✓", "ok");
  } catch (e) { toast(`Token problem: ${e.message}`, "err"); }
  init();
});
$("gh-forget").addEventListener("click", () => {
  const s = getSettings(); saveSettings({ ...s, token: "", remember: false }); $("gh-token").value = ""; init();
});
form.addEventListener("submit", (e) => e.preventDefault());
$("recheck").addEventListener("click", refreshStatus);
$("g-folder").addEventListener("input", folderLink);
$("universe").addEventListener("change", universeVisibility);
form.querySelectorAll("input[name=backend]").forEach(r => r.addEventListener("change", driveVisibility));

document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => {
  navigator.clipboard.writeText($(b.dataset.copy).value).then(() => toast("Copied", "ok"));
}));

document.querySelectorAll("[data-secret]").forEach(b => b.addEventListener("click", async () => {
  const input = $(b.dataset.input), v = input.value.trim();
  if (!v) return toast("Paste the value first.", "err");
  if (!getSettings().token) return toast("Add a GitHub token in step 1 first.", "err");
  try { await setSecret(b.dataset.secret, v); input.value = ""; toast(`${b.dataset.secret} saved ✓`, "ok"); refreshStatus(); }
  catch (e) { toast(e.message, "err"); }
}));

async function saveConfig(next, message) {
  await writeRepoFile("paper_config.json", JSON.stringify(next, null, 2) + "\n", message);
  cfg = next;
  fillForm();
}

$("save-cfg").addEventListener("click", async () => {
  if (!getSettings().token) return toast("Add a GitHub token in step 1 first.", "err");
  const next = readForm();
  const errs = validate(next);
  if (errs.length) return toast(errs[0], "err");
  if ($("reset-account").checked) {
    if (!confirm(`Start a fresh paper account with ${next.account.starting_cash.toLocaleString("en-US", { style: "currency", currency: "USD" })}?`)) return;
    next.account.id = (cfg.account.id || 0) + 1;
  }
  try {
    await saveConfig(next, "paper: update settings from setup page");
    $("reset-account").checked = false;
    toast("Settings saved ✓ — the next run will use them.", "ok");
    refreshStatus();
  } catch (e) { toast(e.message, "err"); }
});

document.querySelectorAll("[data-bt]").forEach(b => b.addEventListener("click", async () => {
  if (!getSettings().token) return toast("Add a GitHub token in step 1 first.", "err");
  try {
    await dispatch({ command: "backtest", days: b.dataset.bt, starting_cash: String($("bt-cash").value || "") });
    toast("Backtest started — it shows up in the dashboard's dropdown in ~2 minutes.", "ok");
    pollRuns();
  } catch (e) { toast(e.message, "err"); }
}));
$("run-live").addEventListener("click", async () => {
  try { await dispatch({ command: "live" }); toast("Live check started.", "ok"); pollRuns(); } catch (e) { toast(e.message, "err"); }
});
$("run-verify").addEventListener("click", async () => {
  try { await dispatch({ command: "verify" }); toast("Verification started.", "ok"); pollRuns(); } catch (e) { toast(e.message, "err"); }
});

// ── Google Drive connection (authorization-code flow with PKCE, in a popup) ──

const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function googleCode(clientId) {
  return new Promise(async (resolve, reject) => {
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
    const url = "https://accounts.google.com/o/oauth2/v2/auth?" + new URLSearchParams({
      client_id: clientId, redirect_uri: location.origin + location.pathname, response_type: "code",
      scope: DRIVE_SCOPE, access_type: "offline", prompt: "consent", state,
      code_challenge: challenge, code_challenge_method: "S256",
    });
    const popup = window.open(url, "tradebot-google", "width=520,height=680");
    if (!popup) return reject(new Error("Popup blocked — allow popups for this page."));
    const onMsg = (e) => {
      if (e.origin !== location.origin || e.data?.type !== "tradebot-google") return;
      window.removeEventListener("message", onMsg);
      if (e.data.state !== state) return reject(new Error("OAuth state mismatch — try again."));
      if (e.data.error) return reject(new Error(`Google: ${e.data.error}`));
      resolve({ code: e.data.code, verifier });
    };
    window.addEventListener("message", onMsg);
  });
}

async function driveUpload(token, folderId, name, text) {
  const mime = name.endsWith(".json") ? "application/json" : "text/csv";
  const boundary = "tradebot" + Math.random().toString(36).slice(2);
  const body = `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify({ name, parents: [folderId], mimeType: mime })}\r\n` +
    `--${boundary}\r\nContent-Type: ${mime}\r\n\r\n${text}\r\n--${boundary}--`;
  const r = await fetch("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id", {
    method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": `multipart/related; boundary=${boundary}` }, body });
  if (!r.ok) throw new Error(`Upload ${name} failed (${r.status})`);
}

$("g-connect").addEventListener("click", async () => {
  const clientId = $("g-client-id").value.trim(), secret = $("g-client-secret").value.trim();
  if (!clientId || !secret) return toast("Paste the OAuth client ID and secret first.", "err");
  if (!getSettings().token) return toast("Add a GitHub token in step 1 first.", "err");
  const btn = $("g-connect"); btn.disabled = true; btn.textContent = "Connecting…";
  try {
    const { code, verifier } = await googleCode(clientId);
    const tr = await fetch("https://oauth2.googleapis.com/token", { method: "POST", body: new URLSearchParams({
      code, client_id: clientId, client_secret: secret, redirect_uri: location.origin + location.pathname,
      grant_type: "authorization_code", code_verifier: verifier }) });
    const tok = await tr.json();
    if (!tr.ok) throw new Error(`Google token exchange: ${tok.error_description || tok.error}`);
    if (!tok.refresh_token) throw new Error("Google did not return a refresh token. Remove the app at myaccount.google.com/permissions and try again.");
    const auth = { Authorization: `Bearer ${tok.access_token}` };

    let folderId = $("g-folder").value.trim();
    if (folderId) {
      const chk = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(folderId)}?fields=id,trashed`, { headers: auth });
      const j = chk.ok ? await chk.json() : null;
      if (!j || j.trashed) folderId = "";
    }
    if (!folderId) {
      const fr = await fetch("https://www.googleapis.com/drive/v3/files?fields=id", { method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ name: "TradeBot Paper Trading", mimeType: "application/vnd.google-apps.folder" }) });
      const fj = await fr.json();
      if (!fr.ok) throw new Error(`Could not create the Drive folder: ${fj.error?.message || fr.status}`);
      folderId = fj.id;
    }

    btn.textContent = "Saving secrets…";
    await setSecret("GOOGLE_CLIENT_ID", clientId);
    await setSecret("GOOGLE_CLIENT_SECRET", secret);
    await setSecret("GOOGLE_REFRESH_TOKEN", tok.refresh_token);

    if ($("g-copy").checked) {
      btn.textContent = "Copying data…";
      const names = ["state.json", "summary.json", "fills.csv", "equity.csv", "signals.csv", "positions.csv", "runs.csv",
        "backtests.csv", "backtest_fills.csv", "backtest_equity.csv", "backtest_signals.csv", "backtest_positions.csv",
        "verify_report.json", "verify_fills.csv"];
      const list = await (await fetch(`https://www.googleapis.com/drive/v3/files?q=${encodeURIComponent(`'${folderId}' in parents and trashed = false`)}&fields=files(name)`, { headers: auth })).json();
      const have = new Set((list.files || []).map(f => f.name));
      for (const n of names) {
        if (have.has(n)) continue;
        const text = await readRepoFile(n, "paper-data").catch(() => null);
        if (text) await driveUpload(tok.access_token, folderId, n, text);
      }
    }

    $("g-client-secret").value = "";
    $("g-folder").value = folderId;
    form.querySelector('input[name=backend][value=drive]').checked = true;
    const next = readForm();
    next.storage = { ...next.storage, backend: "drive", drive_folder_id: folderId, google_client_id: clientId };
    await saveConfig(next, "paper: store results in Google Drive");
    toast("Google Drive connected ✓ — results now save to your Drive folder.", "ok");
    refreshStatus();
  } catch (e) { toast(e.message, "err"); }
  btn.disabled = false; btn.textContent = "Connect Google Drive";
});

$("g-test").addEventListener("click", async () => {
  const clientId = $("g-client-id").value.trim(), folder = $("g-folder").value.trim();
  if (!clientId || !folder) return toast("Connect Google Drive first.", "err");
  try {
    const token = await driveAccessToken(clientId);
    const q = encodeURIComponent(`'${folder}' in parents and trashed = false`);
    const j = await (await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(name,modifiedTime)`, { headers: { Authorization: `Bearer ${token}` } })).json();
    if (j.error) throw new Error(j.error.message);
    toast(`Drive OK — ${j.files.length} file(s): ${j.files.map(f => f.name).slice(0, 6).join(", ") || "none yet"}`, "ok");
  } catch (e) { toast(`Drive: ${e.message}`, "err"); }
});

init();
