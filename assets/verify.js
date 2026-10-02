import {
  nav, loadConfig, loadFiles, parseCSV, num, usd, esc, fmtTime, chartUrl, roundtrips, table,
  getSettings, DEV_QS, dispatch, toast,
} from "./core.js";

nav("verify.html");
const $ = (id) => document.getElementById(id);
const FILES = ["verify_report.json", "verify_fills.csv", "state.json", "fills.csv", "equity.csv", "positions.csv",
  "backtests.csv", "backtest_fills.csv", "backtest_equity.csv", "backtest_positions.csv"];
const ICON = { pass: ["✓", "pos"], fail: ["✗", "neg"], warn: ["!", ""], skip: ["–", "muted"] };
const BADGE = { pass: "green", fail: "red", warn: "yellow", skip: "gray" };
let cfg, raw;

function hashParams() { return new URLSearchParams(location.hash.slice(1)); }

async function load() {
  try {
    cfg = await loadConfig();
    const files = await loadFiles(cfg, FILES);
    raw = { report: files["verify_report.json"] ? JSON.parse(files["verify_report.json"]) : null,
            state: files["state.json"] ? JSON.parse(files["state.json"]) : null };
    FILES.filter(f => f.endsWith(".csv")).forEach(f => { raw[f.replace(".csv", "")] = parseCSV(files[f]); });
    $("slip").textContent = `${cfg.execution.slippage_bps} bps = ${(cfg.execution.slippage_bps / 100).toFixed(2)}%`;
    const sel = $("scope");
    sel.innerHTML = `<option value="live">Live paper account #${esc(cfg.account.id)}</option>` +
      [...raw.backtests].reverse().map(b => `<option value="${esc(b.backtest_id)}">Backtest ${esc(b.backtest_id)}</option>`).join("");
    const want = hashParams().get("scope");
    if (want && [...sel.options].some(o => o.value === want)) sel.value = want;
    renderReport();
    render();
  } catch (e) {
    $("notices").innerHTML = `<div class="notice err">${esc(e.message)} — check the <a href="setup.html${esc(DEV_QS)}">Setup page</a>.</div>`;
  }
}

function renderReport() {
  const r = raw.report;
  if (!r) {
    $("checks").innerHTML = `<p class="empty">No verification report yet. It runs automatically after each market close, or click “Run verification now”.</p>`;
    return;
  }
  $("overall").innerHTML = `<span class="badge ${BADGE[r.overall]}">${esc(r.overall.toUpperCase())}</span>`;
  $("gen").textContent = `generated ${fmtTime(r.generated_at)} ET`;
  $("checks").innerHTML = r.checks.map(c => {
    const [ic, cls] = ICON[c.status] || ["?", ""];
    return `<div class="check-row"><div class="icon ${cls}">${ic}</div><div>
      <div><b>${esc(c.title)}</b> <span class="muted">· ${Number(c.checked) || 0} checked${c.failed ? ` · ${Number(c.failed) || 0} problem${c.failed > 1 ? "s" : ""}` : ""}</span></div>
      ${c.detail ? `<div class="muted">${esc(c.detail)}</div>` : ""}
      ${c.failures?.length ? `<details><summary>Show problems</summary><ul>${c.failures.map(f => `<li><code>${esc(f)}</code></li>`).join("")}</ul></details>` : ""}
    </div></div>`;
  }).join("");
}

function scopeData() {
  const id = $("scope").value;
  if (id === "live") {
    const a = String(cfg.account.id), mine = rows => rows.filter(r => String(r.account_id) === a);
    const starting = raw.state && String(raw.state.account_id) === a ? raw.state.starting_cash : num(cfg.account.starting_cash);
    return { id, live: true, starting, fills: mine(raw.fills), equity: mine(raw.equity), positions: mine(raw.positions),
             evidence: raw.verify_fills.filter(e => e.scope === "live") };
  }
  const bt = raw.backtests.find(b => b.backtest_id === id), f = rows => rows.filter(r => r.run_id === id);
  return { id, live: false, starting: num(bt.starting_cash), fills: f(raw.backtest_fills), equity: f(raw.backtest_equity),
           positions: f(raw.backtest_positions), evidence: raw.verify_fills.filter(e => e.scope === id) };
}

function jsChecks(d) {
  const out = [], TOL = 0.02, slip = cfg.execution.slippage_bps / 10000;
  const fills = [...d.fills].sort((a, b) => num(a.fill_id) - num(b.fill_id));

  let cash = d.starting; const bad1 = [];
  fills.forEach(f => {
    cash += f.side === "BUY" ? -(num(f.amount) + num(f.fee)) : num(f.amount) - num(f.fee);
    if (Math.abs(cash - num(f.cash_after)) > TOL) { bad1.push(`fill ${f.fill_id}: expected ${cash.toFixed(2)}, file says ${f.cash_after}`); cash = num(f.cash_after); }
  });
  out.push(["Cash ledger: starting cash ± every buy and sell = recorded cash", bad1, fills.length, `ends at ${usd(cash)}`]);

  const trips = roundtrips(fills), bad2 = [];
  trips.filter(t => t.closed).forEach(t => {
    if (Math.abs(t.pnl - t.recorded_pnl) > TOL) bad2.push(`trade ${t.trade_id} ${t.ticker}: computed ${t.pnl.toFixed(2)} vs recorded ${t.recorded_pnl}`);
    if (Math.abs(num(t.buy.qty) - num(t.sell.qty)) > 1e-6) bad2.push(`trade ${t.trade_id}: sold a different share count than bought`);
  });
  out.push(["Profit/loss of every closed trade recomputes from its buy and sell", bad2, trips.filter(t => t.closed).length]);

  const bad3 = [];
  fills.forEach(f => {
    const p = num(f.price), lo = num(f.bar_low) * (1 - slip), hi = num(f.bar_high) * (1 + slip), eps = 0.0005 * p;
    if (p < lo - eps || p > hi + eps) bad3.push(`fill ${f.fill_id} ${f.ticker} @ ${p} outside its bar ${f.bar_low}–${f.bar_high}`);
    if (f.side === "BUY" && Math.abs(p - num(f.bar_open) * (1 + slip)) > eps) bad3.push(`buy ${f.fill_id} not at bar open + slippage`);
    if (f.reason === "target" && num(f.bar_high) < num(f.target) - eps) bad3.push(`sell ${f.fill_id} at target but the bar never reached it`);
  });
  out.push(["Every fill price is inside the 5-minute bar it filled in", bad3, fills.length]);

  const bad4 = d.equity.filter(e => Math.abs(num(e.cash) + num(e.positions_value) - num(e.equity)) > TOL)
    .map(e => `${e.timestamp}: ${e.cash} + ${e.positions_value} ≠ ${e.equity}`);
  out.push(["Account value = cash + positions on every snapshot", bad4, d.equity.length]);

  const openLedger = trips.filter(t => !t.closed).map(t => t.ticker).sort().join(",");
  const openFile = d.positions.map(p => p.ticker).sort().join(",");
  const last = d.equity[d.equity.length - 1];
  const bad5 = [];
  if (openLedger !== openFile) bad5.push(`ledger open [${openLedger}] vs positions file [${openFile}]`);
  if (last && fills.length && Math.abs(num(last.cash) - cash) > TOL) bad5.push(`last snapshot cash ${last.cash} vs ledger ${cash.toFixed(2)}`);
  const realized = trips.filter(t => t.closed).reduce((a, t) => a + t.pnl, 0);
  if (last && Math.abs(num(last.realized_pnl) - realized) > TOL * Math.max(1, trips.length)) bad5.push(`snapshot realized ${last.realized_pnl} vs trades ${realized.toFixed(2)}`);
  out.push(["Open positions and totals agree with the trade ledger", bad5, trips.length]);
  return out;
}

function render() {
  const d = scopeData();
  const checks = jsChecks(d);
  const failed = checks.some(c => c[1].length);
  $("js-overall").innerHTML = `<span class="badge ${failed ? "red" : "green"}">${failed ? "PROBLEMS FOUND" : "ALL MATCH"}</span>`;
  $("js-checks").innerHTML = checks.map(([title, bad, n, extra]) => `<div class="check-row">
    <div class="icon ${bad.length ? "neg" : "pos"}">${bad.length ? "✗" : "✓"}</div><div>
    <div><b>${esc(title)}</b> <span class="muted">· ${n} checked${extra ? ` · ${esc(extra)}` : ""}</span></div>
    ${bad.length ? `<details open><summary>${bad.length} problem(s)</summary><ul>${bad.slice(0, 25).map(b => `<li><code>${esc(b)}</code></li>`).join("")}</ul></details>` : ""}
    </div></div>`).join("");
  renderEvidence(d);
}

function renderEvidence(d) {
  const ev = Object.fromEntries(d.evidence.map(e => [e.fill_id, e]));
  const f = $("filter").value.trim().toUpperCase();
  const trade = hashParams().get("trade");
  const rows = [...d.fills].sort((a, b) => num(b.fill_id) - num(a.fill_id)).filter(r => !f || r.ticker.includes(f));
  $("evidence").innerHTML = table([
    { label: "Trade", html: r => `<span${r.trade_id === trade ? ' class="badge yellow"' : ""}>#${esc(r.trade_id)}</span>` },
    { label: "Bar time (ET)", get: r => fmtTime(r.bar_ts) },
    { label: "Ticker", html: r => `<span class="ticker">${esc(r.ticker)}</span>` },
    { label: "Side", get: r => r.side },
    { label: "Fill price", num: 1, get: r => usd(r.price) },
    { label: "Why", get: r => r.reason.replace(/^rules: /, "") },
    { label: "Bar O / H / L / C", num: 1, get: r => [r.bar_open, r.bar_high, r.bar_low, r.bar_close].map(x => num(x).toFixed(2)).join(" / ") },
    { label: "Independent range", num: 1, html: r => { const e = ev[r.fill_id]; return e && e.ref_low ? `${num(e.ref_low).toFixed(2)}–${num(e.ref_high).toFixed(2)} <span class="muted">${esc(e.ref_source)}</span>` : `<span class="muted">${e ? esc(e.ref_source || "n/a") : "not checked yet"}</span>`; } },
    { label: "OK?", html: r => { const e = ev[r.fill_id]; return !e || !e.within_ref ? "—" : e.within_ref === "yes" ? `<span class="pos">✓</span>` : `<span class="neg">✗</span>`; } },
    { label: "", html: r => `<a href="${chartUrl(r.ticker)}" target="_blank" rel="noopener">chart</a>` },
  ], rows, "No fills to check yet.");
}

$("scope").addEventListener("change", () => { location.hash = `scope=${encodeURIComponent($("scope").value)}`; render(); });
$("filter").addEventListener("input", () => renderEvidence(scopeData()));
$("run-verify").addEventListener("click", async () => {
  if (!getSettings().token) return toast("Add a GitHub token on the Setup page first.", "err");
  try { await dispatch({ command: "verify" }); toast("Verification started — refresh in ~2 minutes.", "ok"); }
  catch (e) { toast(e.message, "err"); }
});
load();
