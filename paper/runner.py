"""
Entry points used by the GitHub Actions workflow (and runnable locally):

  python -m paper live                       process newly completed bars for the live paper account
  python -m paper backtest --days 5          replay the last N trading days from fresh paper cash
  python -m paper verify                     independent accuracy checks -> verify_report.json

Add --synthetic to use generated data (offline demo) and --data-dir DIR to
force local storage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, time as dtime, timedelta

from paper import config as config_mod
from paper import marketdata, synthetic
from paper.claude import make_decider
from paper.engine import EQUITY_COLS, FILL_COLS, POSITION_COLS, SIGNAL_COLS, Engine, new_state
from paper.marketdata import ET
from paper.report import summarize
from paper.storage import LocalStorage, from_config

RUN_COLS = ["run_id", "mode", "started_at", "finished_at", "status", "bars_processed", "fills",
            "signals", "equity", "cash", "data_source", "message"]
BACKTEST_COLS = [
    "backtest_id", "created_at", "days", "start_date", "end_date", "starting_cash", "ending_equity",
    "return_usd", "return_pct", "realized_pnl", "unrealized_pnl", "trades_opened", "trades_closed",
    "wins", "losses", "win_rate", "max_drawdown_pct", "open_positions", "data_source",
    "strategy_hash", "watchlist", "config_json",
]


def _now(arg: str | None) -> datetime:
    return datetime.fromisoformat(arg).astimezone(ET) if arg else datetime.now(ET)


def _fetch(cfg, symbols, start: date, end: date, use_synthetic: bool):
    if use_synthetic:
        return synthetic.generate(symbols, start, end, interval_minutes=cfg["market_data"]["interval_minutes"])
    return marketdata.fetch(symbols, start, end, provider=cfg["market_data"]["provider"],
                            interval_minutes=cfg["market_data"]["interval_minutes"])


def _decider(cfg):
    if not cfg["strategy"].get("use_claude"):
        return None
    d = make_decider()
    if d is None:
        print("use_claude is on but ANTHROPIC_API_KEY is not set — falling back to rules", file=sys.stderr)
    return d


# ── live ──────────────────────────────────────────────────────────────────────

def cmd_live(cfg, store, args) -> dict:
    now = _now(args.now)
    run_id = "live-" + now.strftime("%Y%m%d-%H%M%S")
    acct = cfg["account"]
    state = store.read_json("state.json")
    reset_msg = ""
    if not state or state.get("account_id") != acct["id"]:
        # New paper account: trade only bars that START after it was created.
        state = new_state(acct["id"], acct["starting_cash"], start_ts=now - timedelta(minutes=1))
        reset_msg = f"new paper account #{acct['id']} with ${acct['starting_cash']:,.2f}; "

    last = datetime.fromisoformat(state["last_bar_ts"]) if state.get("last_bar_ts") else now
    start = max(last.date(), now.date() - timedelta(days=30))
    if now.weekday() >= 5 or now.time() < dtime(9, 35):
        if not reset_msg and start == now.date():
            return {"run_id": run_id, "status": "idle", "message": "market closed"}

    symbols = sorted(set(cfg["strategy"]["watchlist"]) | set(state["positions"]))
    md = _fetch(cfg, symbols, start, now.date(), args.synthetic)
    eng = Engine(cfg, state, md, mode="live", run_id=run_id, decider=_decider(cfg))
    n = eng.run(until=now)

    store.append_csv("fills.csv", eng.fills, FILL_COLS)
    store.append_csv("signals.csv", eng.signals, SIGNAL_COLS)
    store.append_csv("equity.csv", eng.equity, EQUITY_COLS)
    store.write_csv("positions.csv", eng.positions_rows(), POSITION_COLS)
    store.write_json("state.json", state)

    acct_fills = [f for f in store.read_csv("fills.csv") if str(f["account_id"]) == str(acct["id"])]
    acct_eq = [e for e in store.read_csv("equity.csv") if str(e["account_id"]) == str(acct["id"])]
    summary = summarize(acct_fills, acct_eq, state["starting_cash"])
    summary.update(account_id=acct["id"], as_of=state.get("last_bar_ts"), updated_at=now.isoformat(),
                   data_source=md.source, strategy_hash=eng.hash)
    store.write_json("summary.json", summary)
    eq = state["cash"] + sum(p["qty"] * state["marks"].get(t, {}).get("price", p["entry_price"])
                             for t, p in state["positions"].items())
    return {"run_id": run_id, "status": "ok", "bars_processed": n, "fills": len(eng.fills),
            "signals": len(eng.signals), "equity": round(eq, 2), "cash": round(state["cash"], 2),
            "data_source": md.source,
            "message": reset_msg + (f"{len(md.errors)} data warnings: {md.errors[0][:120]}" if md.errors else "")}


# ── backtest ──────────────────────────────────────────────────────────────────

def cmd_backtest(cfg, store, args) -> dict:
    now = _now(args.now)
    days = max(1, int(args.days))
    cash = float(args.starting_cash or cfg["account"]["starting_cash"])
    bt_id = args.id or "bt-" + now.strftime("%Y%m%d-%H%M%S")
    symbols = cfg["strategy"]["watchlist"]

    md = _fetch(cfg, symbols, now.date() - timedelta(days=days * 2 + 7), now.date(), args.synthetic)
    sessions = sorted({b.ts.date() for bars in md.intraday.values() for b in bars})
    # Today only counts once the session is over, so "1 day" means the last full day.
    if sessions and sessions[-1] == now.date() and now.time() < dtime(16, 0):
        sessions = sessions[:-1]
    chosen = sessions[-days:]
    if not chosen:
        raise RuntimeError("No completed trading sessions found in market data")
    first, last = chosen[0], chosen[-1]
    md.intraday = {s: [b for b in bars if first <= b.ts.date() <= last] for s, bars in md.intraday.items()}

    state = new_state(0, cash, start_ts=datetime.combine(first, dtime(9, 30), ET))
    eng = Engine(cfg, state, md, mode="backtest", run_id=bt_id, decider=_decider(cfg))
    n = eng.run()

    store.append_csv("backtest_fills.csv", eng.fills, FILL_COLS)
    store.append_csv("backtest_signals.csv", eng.signals, SIGNAL_COLS)
    store.append_csv("backtest_equity.csv", eng.equity, EQUITY_COLS)
    store.append_csv("backtest_positions.csv", eng.positions_rows(), POSITION_COLS)
    sm = summarize(eng.fills, eng.equity, cash)
    row = {k: sm.get(k, "") for k in BACKTEST_COLS}
    row.update(backtest_id=bt_id, created_at=now.isoformat(), days=len(chosen), start_date=first.isoformat(),
               end_date=last.isoformat(), data_source=md.source, strategy_hash=eng.hash,
               watchlist=" ".join(symbols), config_json=json.dumps(cfg["strategy"], sort_keys=True))
    store.append_csv("backtests.csv", [row], BACKTEST_COLS)
    return {"run_id": bt_id, "status": "ok", "bars_processed": n, "fills": len(eng.fills),
            "signals": len(eng.signals), "equity": sm["ending_equity"], "cash": sm["cash"],
            "data_source": md.source,
            "message": f"{len(chosen)} day(s) {first}..{last}: {sm['return_pct']:+.2f}% "
                       f"({sm['trades_closed']} closed, {sm['open_positions']} open)"}


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m paper")
    ap.add_argument("command", choices=["live", "backtest", "verify"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--data-dir", default=None, help="force local storage in this directory")
    ap.add_argument("--synthetic", action="store_true", help="use generated market data (offline demo)")
    ap.add_argument("--now", default=None, help="override current time (ISO 8601)")
    ap.add_argument("--days", default=os.environ.get("BACKTEST_DAYS") or "1")
    ap.add_argument("--starting-cash", default=os.environ.get("BACKTEST_CASH") or None)
    ap.add_argument("--id", default=None)
    args = ap.parse_args(argv)

    cfg = config_mod.load(args.config)
    store = LocalStorage(args.data_dir) if args.data_dir else from_config(cfg)
    started = datetime.now(ET).isoformat()
    try:
        if args.command == "verify":
            from paper.verify import cmd_verify
            result = cmd_verify(cfg, store, args)
        elif args.command == "backtest":
            result = cmd_backtest(cfg, store, args)
        else:
            result = cmd_live(cfg, store, args)
    except Exception as e:  # noqa: BLE001 — record the failure, then fail the job
        traceback.print_exc()
        result = {"run_id": f"{args.command}-error", "status": "error", "message": str(e)[:300]}

    row = {"mode": args.command, "started_at": started, "finished_at": datetime.now(ET).isoformat(), **result}
    try:
        if result.get("status") != "idle":
            store.append_csv("runs.csv", [row], RUN_COLS)
    except Exception as e:  # noqa: BLE001
        print(f"could not record run: {e}", file=sys.stderr)
    print(json.dumps(row, indent=2))
    return 1 if result.get("status") == "error" else 0
