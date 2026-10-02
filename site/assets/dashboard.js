import {
  nav, loadConfig, loadFiles, parseCSV, sourceLabel, num, usd, pct, tone, esc, fmtTime, etDate,
  chartUrl, roundtrips, table, lineChart, barChart, getSettings,
} from "./core.js";

nav("index.html");

const FILES = ["state.json", "fills.csv", "equity.csv", "signals.csv", "positions.csv", "runs.csv",
  "backtests.csv", "backtest_fills.csv", "backtest_equity.csv", "backtest_signals.csv", "backtest_positions.csv"];

let cfg, raw, range = 0;
const $ = (id) => document.getElementById(id);

async function load() {
  $("sub").textContent = "Loading…";
  $("notices").innerHTML = "";
  try {
    const s = getSettings();
    cfg = await loadConfig();
    if (!s.owner && !location.search.includes("data=")) throw new Error("Repository not set.");
    const files = await loadFiles(cfg, FILES);
    raw = {
      state: files["state.json"] ? JSON.parse(files["state.json"]) : null,
      ...Object.fromEntries(FILES.filter(f => f.endsWith(".csv")).map(f => [f.replace(".csv", ""), parseCSV(files[f])])),
    };
    buildScopes();
    render();
  } catch (e) {
    $("sub").textContent = "";
    $("notices").innerHTML = `<div class="notice err">${esc(e.message)} — check the <a href="setup.html${location.search}">Setup page</a>.</div>`;
  }
}

function buildScopes() {
  const sel = $("scope"), prev = sel.value;
  const bts = [...raw.backtests].reverse();
  sel.innerHTML = `<option value="live">Live paper account #${esc(cfg.account.id)}</option>` +
    bts.map(b => `<option value="${esc(b.backtest_id)}">Backtest · ${esc(b.days)} day${b.days === "1" ? "" : "s"} ending ${esc(b.end_date)} · ${pct(b.return_pct)}</option>`).join("");
  const want = new URLSearchParams(location.hash.slice(1)).get("scope");
  sel.value = [...sel.options].some(o => o.value === (want || prev)) ? (want || prev) : "live";
}

function scopeData() {
  const id = $("scope").value;
  if (id === "live") {
    const acct = String(cfg.account.id);
    const mine = (rows) => rows.filter(r => String(r.account_id) === acct);
    const starting = raw.state && String(raw.state.account_id) === acct ? raw.state.starting_cash : num(cfg.account.starting_cash);
    return { id, live: true, starting, fills: mine(raw.fills), equity: mine(raw.equity), signals: mine(raw.signals),
             positions: mine(raw.positions) };
  }
  const bt = raw.backtests.find(b => b.backtest_id === id);
  const f = (rows) => rows.filter(r => r.run_id === id);
  return { id, live: false, bt, starting: num(bt.starting_cash), fills: f(raw.backtest_fills), equity: f(raw.backtest_equity),
           signals: f(raw.backtest_signals), positions: f(raw.backtest_positions) };
}

function sessions(equity) { return [...new Set(equity.map(e => etDate(e.timestamp)))].sort(); }

function windowStats(d, n) {
  const days = sessions(d.equity);
  const win = n ? days.slice(-n) : days;
  if (!win.length) return null;
  const first = win[0];
  const before = d.equity.filter(e => etDate(e.timestamp) < first);
  const base = before.length ? num(before[before.length - 1].equity) : d.starting;
  const end = num(d.equity[d.equity.length - 1].equity);
  const trips = roundtrips(d.fills);
  const closed = trips.filter(t => t.closed && etDate(t.exit_ts) >= first);
  const opened = trips.filter(t => etDate(t.entry_ts) >= first);
  const wins = closed.filter(t => t.pnl > 0).length;
  return { from: first, to: win[win.length - 1], days: win.length, base, end, chg: end - base, chgPct: (end - base) / base * 100,
           opened: opened.length, closed: closed.length, wins, losses: closed.length - wins,
           realized: closed.reduce((a, t) => a + t.pnl, 0),
           tickers: [...new Set(opened.map(t => t.ticker))] };
}

function render() {
  const d = scopeData();
  location.hash = d.live ? "" : `scope=${encodeURIComponent(d.id)}`;
  const last = d.equity[d.equity.length - 1];
  const trips = roundtrips(d.fills);
  const closed = trips.filter(t => t.closed), open = trips.filter(t => !t.closed);
  const wins = closed.filter(t => t.pnl > 0).length;
  const equity = last ? num(last.equity) : d.starting;
  const realized = closed.reduce((a, t) => a + t.pnl, 0);

  $("sub").innerHTML = d.live
    ? `Data from ${esc(sourceLabel(cfg))} · updated through ${esc(fmtTime(raw.state?.last_bar_ts))} ET`
    : `Backtest replay of ${esc(d.bt.start_date)} → ${esc(d.bt.end_date)} · ${esc(d.bt.data_source)} data · run ${esc(fmtTime(d.bt.created_at))}`;

  notices(d);

  let peak = d.starting, mdd = 0;
  d.equity.forEach(e => { peak = Math.max(peak, num(e.equity)); mdd = Math.max(mdd, (peak - num(e.equity)) / peak * 100); });
  const kpi = (k, v, s = "", cls = "") => `<div class="kpi"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="s">${s}</div></div>`;
  $("kpis").innerHTML = [
    kpi("Account value", usd(equity), `started with ${usd(d.starting)}`),
    kpi("Total return", usd(equity - d.starting, true), pct((equity - d.starting) / d.starting * 100), tone(equity - d.starting)),
    kpi("Realized P&L", usd(realized, true), `${closed.length} closed trade${closed.length === 1 ? "" : "s"}`, tone(realized)),
    kpi("Unrealized P&L", usd(last?.unrealized_pnl ?? 0, true), `${open.length} open position${open.length === 1 ? "" : "s"}`, tone(last?.unrealized_pnl)),
    kpi("Win rate", closed.length ? `${(wins / closed.length * 100).toFixed(0)}%` : "—", `${wins} W · ${closed.length - wins} L`),
    kpi("Cash", usd(last?.cash ?? d.starting), `max drawdown ${mdd.toFixed(2)}%`),
  ].join("");

  const rows = [[1, "Last day"], [2, "Last 2 days"], [5, "Last week (5 days)"], [0, d.live ? "Since account start" : "Whole backtest"]]
    .map(([n, label]) => ({ label, ...(windowStats(d, n) || {}) })).filter(r => r.from);
  $("period-note").textContent = d.live ? "trading sessions, measured from the prior close" : "windows inside this backtest";
  $("periods").innerHTML = table([
    { label: "Period", get: r => r.label },
    { label: "Dates", get: r => r.from === r.to ? r.from : `${r.from} → ${r.to}` },
    { label: "Start value", num: 1, get: r => usd(r.base) },
    { label: "End value", num: 1, get: r => usd(r.end) },
    { label: "Gain / loss", num: 1, html: r => `<span class="${tone(r.chg)}">${usd(r.chg, true)} (${pct(r.chgPct)})</span>` },
    { label: "Buys", num: 1, get: r => r.opened },
    { label: "Closed W/L", num: 1, get: r => `${r.wins}/${r.losses}` },
    { label: "Realized", num: 1, html: r => `<span class="${tone(r.realized)}">${usd(r.realized, true)}</span>` },
    { label: "Stocks bought", html: r => esc(r.tickers.join(", ") || "—") },
  ], rows, d.live ? "No trading sessions processed yet. The bot runs every 15 minutes during market hours." : "This backtest has no bars.");

  renderEquity(d);
  const days = sessions(d.equity);
  let prev = d.starting;
  barChart($("daily-chart"), days.map(day => {
    const rowsDay = d.equity.filter(e => etDate(e.timestamp) === day);
    const end = num(rowsDay[rowsDay.length - 1].equity), v = end - prev;
    const note = `${new Date(day + "T12:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" })} · ${pct(v / prev * 100)}`;
    prev = end;
    return { label: new Date(day + "T12:00").toLocaleDateString("en-US", { month: "short", day: "numeric" }), v, note };
  }));

  $("positions").innerHTML = table([
    { label: "Ticker", html: r => `<span class="ticker">${esc(r.ticker)}</span>` },
    { label: "Bought", get: r => fmtTime(r.entry_ts) },
    { label: "Shares", num: 1, get: r => num(r.qty).toFixed(4) },
    { label: "Entry", num: 1, get: r => usd(r.entry_price) },
    { label: "Last", num: 1, get: r => usd(r.mark_price) },
    { label: "Limit sell", num: 1, get: r => usd(r.target) },
    { label: "Stop", num: 1, get: r => (r.stop ? usd(r.stop) : "none") },
    { label: "Value", num: 1, get: r => usd(r.market_value) },
    { label: "Unrealized", num: 1, html: r => `<span class="${tone(r.unrealized_pnl)}">${usd(r.unrealized_pnl, true)} (${pct(r.unrealized_pct)})</span>` },
  ], d.positions, "No open positions.");

  renderTrades(d, trips);

  const by = {};
  trips.forEach(t => {
    const s = by[t.ticker] ||= { ticker: t.ticker, buys: 0, invested: 0, closed: 0, wins: 0, pnl: 0, open: 0 };
    s.buys++; s.invested += t.cost;
    if (t.closed) { s.closed++; s.wins += t.pnl > 0; s.pnl += t.pnl; } else s.open++;
  });
  $("stocks").innerHTML = table([
    { label: "Ticker", html: r => `<a class="ticker" href="${chartUrl(r.ticker)}" target="_blank" rel="noopener">${esc(r.ticker)}</a>` },
    { label: "Times bought", num: 1, get: r => r.buys },
    { label: "Total bought", num: 1, get: r => usd(r.invested) },
    { label: "Closed W/L", num: 1, get: r => `${r.wins}/${r.closed - r.wins}` },
    { label: "Still open", num: 1, get: r => r.open },
    { label: "Realized P&L", num: 1, html: r => `<span class="${tone(r.pnl)}">${usd(r.pnl, true)}</span>` },
  ], Object.values(by).sort((a, b) => b.pnl - a.pnl), "No stocks bought yet.");

  $("signals").innerHTML = table([
    { label: "Time (ET)", get: r => fmtTime(r.timestamp) },
    { label: "Ticker", html: r => `<span class="ticker">${esc(r.ticker)}</span>` },
    { label: "Price", num: 1, get: r => usd(r.price) },
    { label: "Move", num: 1, html: r => `<span class="${tone(r.change_pct)}">${pct(r.change_pct)}</span>` },
    { label: "Volume", num: 1, get: r => `${num(r.vol_ratio).toFixed(1)}×` },
    { label: "Decision", html: r => `<span class="badge ${r.decision === "BUY" ? "green" : r.decision === "PASS" ? "gray" : "yellow"}">${esc(r.decision)}</span>` },
    { label: "By", get: r => r.decided_by },
    { label: "Reason", cls: () => "wrap", get: r => r.reason },
  ], [...d.signals].reverse().slice(0, 100), "No signals yet.");

  const runs = [...raw.runs].reverse().slice(0, 20);
  $("runs").innerHTML = table([
    { label: "Finished", get: r => fmtTime(r.finished_at) },
    { label: "Type", get: r => r.mode },
    { label: "Status", html: r => `<span class="badge ${r.status === "ok" ? "green" : "red"}">${esc(r.status)}</span>` },
    { label: "Bars", num: 1, get: r => r.bars_processed },
    { label: "Fills", num: 1, get: r => r.fills },
    { label: "Data", get: r => r.data_source },
    { label: "Message", cls: () => "wrap", get: r => r.message },
  ], runs, "The bot hasn't run yet.");
}

function notices(d) {
  const out = [];
  if (d.live) {
    const runs = raw.runs.filter(r => r.mode === "live");
    const lastRun = runs[runs.length - 1];
    if (!lastRun) out.push(`<div class="notice warn">The live paper bot hasn't run yet. Merge this branch to <code>main</code> so the schedule starts, or use <a href="setup.html${location.search}">Setup → Run now</a>.</div>`);
    else if (lastRun.status !== "ok") out.push(`<div class="notice err">Last bot run failed: ${esc(lastRun.message)}</div>`);
    const hrs = lastRun ? (Date.now() - new Date(lastRun.finished_at)) / 36e5 : 0;
    const wd = new Date().getDay();
    if (lastRun && hrs > 26 && wd >= 2 && wd <= 5) out.push(`<div class="notice warn">No bot run in ${hrs.toFixed(0)} hours — check the Actions tab (GitHub pauses schedules on repos with no activity for 60 days).</div>`);
  }
  if (d.fills.some(f => f.data_source === "synthetic")) out.push(`<div class="notice warn">These results use <b>synthetic</b> demo data, not real prices.</div>`);
  $("notices").innerHTML = out.join("");
}

function renderEquity(d) {
  const days = sessions(d.equity);
  const keep = range ? new Set(days.slice(-range)) : null;
  const rows = keep ? d.equity.filter(e => keep.has(etDate(e.timestamp))) : d.equity;
  let baseline = d.starting;
  if (keep) {
    const before = d.equity.filter(e => etDate(e.timestamp) < days.slice(-range)[0]);
    if (before.length) baseline = num(before[before.length - 1].equity);
  }
  lineChart($("equity-chart"), rows.map(e => ({ t: e.timestamp, v: num(e.equity) })), { baseline, label: "Account value" });
}

function renderTrades(d, trips) {
  const f = $("filter").value.trim().toUpperCase();
  const rows = trips.filter(t => !f || t.ticker.includes(f)).sort((a, b) => (b.entry_ts || "").localeCompare(a.entry_ts || ""));
  const scope = d.live ? "live" : d.id;
  $("trades").innerHTML = table([
    { label: "Ticker", html: r => `<span class="ticker">${esc(r.ticker)}</span>` },
    { label: "Bought (ET)", get: r => fmtTime(r.entry_ts) },
    { label: "Buy price", num: 1, get: r => usd(r.entry_price) },
    { label: "Shares", num: 1, get: r => r.qty.toFixed(4) },
    { label: "Limit sell", num: 1, get: r => usd(r.target) },
    { label: "Sold (ET)", get: r => (r.closed ? fmtTime(r.exit_ts) : "open") },
    { label: "Sell price", num: 1, get: r => (r.closed ? usd(r.exit_price) : "—") },
    { label: "Why sold", get: r => r.exit_reason || "" },
    { label: "P&L", num: 1, html: r => (r.closed ? `<span class="${tone(r.pnl)}">${usd(r.pnl, true)} (${pct(r.pnl_pct)})</span>` : "—") },
    { label: "", html: r => `<a href="verify.html${location.search}#scope=${encodeURIComponent(scope)}&trade=${encodeURIComponent(r.trade_id)}">check</a> · <a href="${chartUrl(r.ticker)}" target="_blank" rel="noopener">chart</a>` },
  ], rows, "No trades yet.");
}

$("scope").addEventListener("change", render);
$("refresh").addEventListener("click", load);
$("filter").addEventListener("input", () => { const d = scopeData(); renderTrades(d, roundtrips(d.fills)); });
$("range").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  range = +b.dataset.n;
  $("range").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  renderEquity(scopeData());
});
let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => raw && render(), 200); });
load();
