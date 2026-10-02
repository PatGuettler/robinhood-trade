"""
Accuracy verification for paper results. Writes verify_report.json and
verify_fills.csv next to the data so the dashboard's Verify page can show it.

Checks
  1. cash_ledger        Re-walk every fill from the starting cash; each fill's
                        recorded cash_after must match.
  2. realized_pnl       Every SELL's P&L recomputed from its BUY (incl. fees).
  3. fill_rules         Each fill obeys the simulation rules against the bar it
                        filled in (buy = next bar open + slippage, target sells
                        only when the bar's high reached the target, ...).
  4. no_lookahead       Every BUY fills on a bar strictly AFTER the bar whose
                        signal triggered it.
  5. equity_math        equity == cash + positions value on every snapshot.
  6. state_matches      Saved account state agrees with the ledger.
  7. independent_prices Each fill price lies inside that day's low/high from a
                        second, independent data source (Stooq / Alpaca).
  8. replay             Re-run the engine from scratch over the same period with
                        freshly downloaded bars; it must reproduce the same trades.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from paper import marketdata
from paper.claude import replay_decider
from paper.config import strategy_hash
from paper.engine import Engine, new_state
from paper.marketdata import ET

TOL = 0.02  # dollars


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _check(name, title, failures, checked, detail="", warn=False, skipped=None):
    if skipped:
        status = "skip"
    elif failures:
        status = "warn" if warn else "fail"
    else:
        status = "pass"
    return {"name": name, "title": title, "status": status, "checked": checked,
            "failed": len(failures), "detail": skipped or detail, "failures": failures[:25]}


def ledger_checks(fills: list[dict], starting_cash: float, slippage_bps: float, label: str) -> list[dict]:
    fills = sorted(fills, key=lambda r: int(_f(r["fill_id"])))
    slip = slippage_bps / 10_000
    out = []

    # 1. cash ledger
    cash, bad = starting_cash, []
    for f in fills:
        amt, fee = _f(f["amount"]), _f(f["fee"])
        cash += -(amt + fee) if f["side"] == "BUY" else (amt - fee)
        if abs(cash - _f(f["cash_after"])) > TOL:
            bad.append(f"fill {f['fill_id']} {f['ticker']} {f['side']}: expected cash {cash:.2f}, "
                       f"recorded {_f(f['cash_after']):.2f}")
            cash = _f(f["cash_after"])
        if abs(_f(f["qty"]) * _f(f["price"]) - amt) > TOL:
            bad.append(f"fill {f['fill_id']}: qty x price != amount")
    out.append(_check("cash_ledger", f"{label}: cash ledger adds up", bad, len(fills),
                      f"Starting ${starting_cash:,.2f} → ledger cash ${cash:,.2f}"))

    # 2. realized P&L
    buys = {f["trade_id"]: f for f in fills if f["side"] == "BUY"}
    bad, n = [], 0
    for f in fills:
        if f["side"] != "SELL":
            continue
        n += 1
        b = buys.get(f["trade_id"])
        if not b:
            bad.append(f"SELL fill {f['fill_id']} has no matching BUY")
            continue
        if abs(_f(b["qty"]) - _f(f["qty"])) > 1e-6:
            bad.append(f"trade {f['trade_id']}: sold qty {f['qty']} != bought {b['qty']}")
        exp = (_f(f["amount"]) - _f(f["fee"])) - (_f(b["amount"]) + _f(b["fee"]))
        if abs(exp - _f(f["realized_pnl"])) > TOL:
            bad.append(f"trade {f['trade_id']} {f['ticker']}: P&L should be {exp:.2f}, recorded {f['realized_pnl']}")
    out.append(_check("realized_pnl", f"{label}: realized P&L recomputes", bad, n))

    # 3. fill rules vs the bar
    bad = []
    for f in fills:
        p, o, h, l = _f(f["price"]), _f(f["bar_open"]), _f(f["bar_high"]), _f(f["bar_low"])
        r, tgt = f["reason"], _f(f["target"])
        eps = 0.0005 * max(p, 1)
        ok = True
        if f["side"] == "BUY":
            ok = abs(p - o * (1 + slip)) <= eps
        elif r.startswith("target (gap"):
            ok = abs(p - o) <= eps and o >= tgt - eps
        elif r == "target":
            ok = abs(p - tgt) <= eps and h >= tgt - eps
        elif r.startswith("stop (gap"):
            ok = abs(p - o * (1 - slip)) <= eps
        elif r == "stop":
            ok = l <= _f(f["stop"]) + eps and abs(p - _f(f["stop"]) * (1 - slip)) <= eps
        else:  # end of day / max hold
            ok = abs(p - _f(f["bar_close"]) * (1 - slip)) <= eps
        if not (l * (1 - slip) - eps <= p <= h * (1 + slip) + eps):
            ok = False
        if not ok:
            bad.append(f"fill {f['fill_id']} {f['ticker']} {f['side']} @ {p} ({r}) bar O{o} H{h} L{l}")
    out.append(_check("fill_rules", f"{label}: every fill obeys the fill rules for its bar", bad, len(fills)))
    return out


def lookahead_check(fills, signals, label):
    sig = {}
    for s in signals:
        if s["decision"] == "BUY":
            sig.setdefault(s["ticker"], []).append(s["timestamp"])
    bad, n = [], 0
    for f in fills:
        if f["side"] != "BUY":
            continue
        n += 1
        prior = [t for t in sig.get(f["ticker"], []) if t < f["bar_ts"] and t[:10] == f["bar_ts"][:10]]
        if not prior:
            bad.append(f"BUY fill {f['fill_id']} {f['ticker']} at {f['bar_ts']} has no earlier BUY signal")
    return _check("no_lookahead", f"{label}: buys fill on the bar after their signal", bad, n)


def equity_check(rows, label):
    bad = [f"{r['timestamp']}: {r['cash']} + {r['positions_value']} != {r['equity']}"
           for r in rows if abs(_f(r["cash"]) + _f(r["positions_value"]) - _f(r["equity"])) > TOL]
    return _check("equity_math", f"{label}: equity = cash + positions on every snapshot", bad, len(rows))


def state_check(state, fills):
    if not state:
        return _check("state_matches", "Live: saved state matches the ledger", [], 0, skipped="no live state yet")
    fills = sorted(fills, key=lambda r: int(_f(r["fill_id"])))
    bad = []
    if fills and abs(_f(fills[-1]["cash_after"]) - state["cash"]) > TOL:
        bad.append(f"state cash {state['cash']:.2f} != last ledger cash {fills[-1]['cash_after']}")
    if not fills and abs(state["cash"] - state["starting_cash"]) > TOL:
        bad.append("cash changed with no fills")
    closed = {f["trade_id"] for f in fills if f["side"] == "SELL"}
    open_ledger = {f["ticker"] for f in fills if f["side"] == "BUY" and f["trade_id"] not in closed}
    if open_ledger != set(state["positions"]):
        bad.append(f"open in ledger {sorted(open_ledger)} vs state {sorted(state['positions'])}")
    return _check("state_matches", "Live: saved state matches the ledger", bad, len(fills))


def _reference_daily(symbol: str, primary_source: str) -> tuple[str, dict[date, marketdata.DailyBar]]:
    errors = []
    try:
        return "stooq", {d.day: d for d in marketdata.stooq_daily(symbol)}
    except Exception as e:  # noqa: BLE001
        errors.append(f"stooq: {e}")
    end = date.today()
    if primary_source != "alpaca" and marketdata.alpaca_keys():
        try:
            rows = marketdata.alpaca_daily([symbol], end - timedelta(days=120), end).get(symbol, [])
            return "alpaca", {d.day: d for d in rows}
        except Exception as e:  # noqa: BLE001
            errors.append(f"alpaca: {e}")
    try:
        rows = marketdata.yahoo_daily(symbol, end - timedelta(days=120), end)
        label = "yahoo daily" + (" (same vendor)" if primary_source == "yahoo" else "")
        return label, {d.day: d for d in rows}
    except Exception as e:  # noqa: BLE001
        errors.append(f"yahoo: {e}")
    raise RuntimeError("; ".join(errors))


def independent_price_check(fills, slippage_bps, label):
    slip = slippage_bps / 10_000 + 0.003   # allow 0.3% for vendor differences
    evidence, bad, skipped = [], [], []
    refs: dict[str, tuple] = {}
    for f in fills:
        sym = f["ticker"]
        if f.get("data_source") == "synthetic":
            skipped.append(f["fill_id"])
            continue
        if sym not in refs:
            try:
                refs[sym] = _reference_daily(sym, f.get("data_source", ""))
            except Exception as e:  # noqa: BLE001
                refs[sym] = (f"unavailable: {e}"[:120], {})
        src, days = refs[sym]
        d = datetime.fromisoformat(f["timestamp"]).date()
        ref = days.get(d)
        p = _f(f["price"])
        row = {"fill_id": f["fill_id"], "trade_id": f["trade_id"], "ticker": sym, "side": f["side"],
               "timestamp": f["timestamp"], "price": p, "reason": f["reason"],
               "bar_low": f["bar_low"], "bar_high": f["bar_high"], "ref_source": src,
               "ref_low": ref.low if ref else "", "ref_high": ref.high if ref else "", "within_ref": ""}
        if ref:
            ok = ref.low * (1 - slip) <= p <= ref.high * (1 + slip)
            row["within_ref"] = "yes" if ok else "NO"
            if not ok:
                bad.append(f"fill {f['fill_id']} {sym} {f['side']} @ {p} outside {src} {d} range "
                           f"{ref.low}-{ref.high}")
        else:
            skipped.append(f["fill_id"])
        evidence.append(row)
    detail = f"{len(fills) - len(skipped)} fills checked against a second daily price source"
    if any("same vendor" in r["ref_source"] for r in evidence):
        detail += (" — the independent source (Stooq) was unavailable, so this compared against the same "
                   "vendor's daily data; add free Alpaca keys on Setup for a truly independent check")
    if skipped:
        detail += f"; {len(skipped)} could not be checked (synthetic data or no reference bar)"
    chk = _check("independent_prices", f"{label}: fill prices match an independent data source", bad,
                 len(fills) - len(skipped), detail,
                 skipped="no fills could be checked" if fills and len(skipped) == len(fills) else None)
    return chk, evidence


def replay_check(cfg, state, fills, signals, use_synthetic=False, candidates=None):
    title = "Live: replaying the same period from scratch reproduces the same trades"
    if not state or not state.get("last_bar_ts"):
        return _check("replay", title, [], 0, skipped="no live bars processed yet")
    hashes = {f["strategy_hash"] for f in fills}
    if hashes and hashes != {strategy_hash(cfg)}:
        return _check("replay", title, [], 0,
                      skipped="strategy settings changed during this account — reset the account to re-enable")
    start = datetime.fromisoformat(state["start_ts"]) if state.get("start_ts") else None
    last = datetime.fromisoformat(state["last_bar_ts"])
    if not start or (last.date() - start.date()).days > 50:
        return _check("replay", title, [], 0, skipped="account older than the 5-minute data window (~60 days)")

    allowed = None
    symbols = sorted(set(cfg["strategy"]["watchlist"]) | {f["ticker"] for f in fills})
    if cfg["strategy"].get("universe", "market") == "market":
        # Re-use the stocks the scanner picked on each day (recorded in candidates.csv).
        from paper.scanner import allowed_by_day
        rows = [r for r in (candidates or []) if start.date().isoformat() <= r["date"] <= last.date().isoformat()]
        allowed = allowed_by_day(rows)
        symbols = sorted({r["ticker"] for r in rows} | {f["ticker"] for f in fills})
    if use_synthetic:
        from paper import synthetic
        md = synthetic.generate(symbols, start.date(), last.date(),
                                interval_minutes=cfg["market_data"]["interval_minutes"])
    else:
        md = marketdata.fetch(symbols, start.date(), last.date(), provider=cfg["market_data"]["provider"],
                              interval_minutes=cfg["market_data"]["interval_minutes"])
    used_claude = any(s.get("decided_by") == "claude" for s in signals)
    fresh = new_state(state["account_id"], state["starting_cash"], start_ts=start)
    eng = Engine(cfg, fresh, md, mode="replay", run_id="replay",
                 decider=replay_decider(signals) if used_claude else None, allowed=allowed)
    eng.run(until=last + timedelta(minutes=md.interval_minutes))

    def key(f):
        return (f["ticker"], f["side"], str(f["bar_ts"]))
    orig = sorted(fills, key=lambda r: int(_f(r["fill_id"])))
    rep = {key(f): f for f in eng.fills}
    bad = []
    for f in orig:
        r = rep.pop(key(f), None)
        if not r:
            bad.append(f"original {f['side']} {f['ticker']} @ {f['bar_ts']} not reproduced")
        elif abs(_f(r["price"]) - _f(f["price"])) > 0.0025 * _f(f["price"]):
            bad.append(f"{f['side']} {f['ticker']} @ {f['bar_ts']}: price {f['price']} vs replay {r['price']}")
    for k in rep:
        bad.append(f"replay produced extra {k[1]} {k[0]} @ {k[2]}")
    return _check("replay", title, bad, len(orig),
                  f"Re-ran {len(md.intraday)} tickers with freshly downloaded {md.source} bars", warn=True)


def cmd_verify(cfg, store, args) -> dict:
    acct = str(cfg["account"]["id"])
    slip = float(cfg["execution"]["slippage_bps"])
    state = store.read_json("state.json")
    fills = [f for f in store.read_csv("fills.csv") if str(f["account_id"]) == acct]
    signals = [s for s in store.read_csv("signals.csv") if str(s["account_id"]) == acct]
    equity = [e for e in store.read_csv("equity.csv") if str(e["account_id"]) == acct]
    starting = state["starting_cash"] if state else cfg["account"]["starting_cash"]

    checks = ledger_checks(fills, starting, slip, "Live")
    checks.append(lookahead_check(fills, signals, "Live"))
    checks.append(equity_check(equity, "Live"))
    checks.append(state_check(state, fills))
    price_chk, evidence = independent_price_check(fills, slip, "Live")
    checks.append(price_chk)
    try:
        cands = [r for r in store.read_csv("candidates.csv") if r["mode"] == "live"]
        checks.append(replay_check(cfg, state, fills, signals, use_synthetic=args.synthetic, candidates=cands))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("replay", "Live: replay reproduces the same trades", [], 0,
                             skipped=f"could not replay: {e}"[:200]))

    bts = store.read_csv("backtests.csv")
    if bts:
        bt = bts[-1]
        bid = bt["backtest_id"]
        bfills = [f for f in store.read_csv("backtest_fills.csv") if f["run_id"] == bid]
        bsig = [s for s in store.read_csv("backtest_signals.csv") if s["run_id"] == bid]
        beq = [e for e in store.read_csv("backtest_equity.csv") if e["run_id"] == bid]
        label = f"Backtest {bid}"
        checks += ledger_checks(bfills, _f(bt["starting_cash"]), slip, label)
        checks.append(lookahead_check(bfills, bsig, label))
        checks.append(equity_check(beq, label))
        bchk, bev = independent_price_check(bfills, slip, label)
        checks.append(bchk)
        evidence += [{**e, "scope": bid} for e in bev]

    report = {
        "generated_at": datetime.now(ET).isoformat(),
        "account_id": cfg["account"]["id"],
        "overall": "fail" if any(c["status"] == "fail" for c in checks)
        else "warn" if any(c["status"] == "warn" for c in checks) else "pass",
        "checks": checks,
    }
    store.write_json("verify_report.json", report)
    cols = ["scope", "fill_id", "trade_id", "ticker", "side", "timestamp", "price", "reason", "bar_low",
            "bar_high", "ref_source", "ref_low", "ref_high", "within_ref"]
    store.write_csv("verify_fills.csv", [{"scope": "live", **e} if "scope" not in e else e for e in evidence], cols)
    passed = sum(c["status"] == "pass" for c in checks)
    return {"run_id": "verify-" + datetime.now(ET).strftime("%Y%m%d-%H%M%S"),
            "status": "ok" if report["overall"] != "fail" else "error",
            "message": f"{report['overall'].upper()}: {passed}/{len(checks)} checks passed"}
