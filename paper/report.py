"""Round-trip trades and performance summaries computed from fills + equity rows."""

from __future__ import annotations

from collections import defaultdict


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def roundtrips(fills: list[dict]) -> list[dict]:
    """Pair BUY/SELL fills by trade_id. Open trades have exit fields blank."""
    by_trade: dict[str, dict] = {}
    for f in sorted(fills, key=lambda r: int(_f(r["fill_id"]))):
        t = by_trade.setdefault(str(f["trade_id"]), {"trade_id": f["trade_id"], "ticker": f["ticker"]})
        if f["side"] == "BUY":
            t.update(entry_ts=f["timestamp"], entry_price=_f(f["price"]), qty=_f(f["qty"]),
                     cost=_f(f["amount"]) + _f(f["fee"]), target=_f(f["target"]), entry_reason=f["reason"])
        else:
            t.update(exit_ts=f["timestamp"], exit_price=_f(f["price"]), exit_reason=f["reason"],
                     pnl=_f(f["realized_pnl"]))
    out = []
    for t in by_trade.values():
        if "pnl" in t and t.get("cost"):
            t["pnl_pct"] = t["pnl"] / t["cost"] * 100
        out.append(t)
    return out


def max_drawdown_pct(equity_rows: list[dict]) -> float:
    peak, mdd = None, 0.0
    for r in equity_rows:
        e = _f(r["equity"])
        peak = e if peak is None else max(peak, e)
        if peak:
            mdd = max(mdd, (peak - e) / peak * 100)
    return mdd


def summarize(fills: list[dict], equity_rows: list[dict], starting_cash: float) -> dict:
    trips = roundtrips(fills)
    closed = [t for t in trips if "pnl" in t]
    wins = [t for t in closed if t["pnl"] > 0]
    last = equity_rows[-1] if equity_rows else None
    ending = _f(last["equity"]) if last else starting_cash
    per_ticker: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for t in closed:
        pt = per_ticker[t["ticker"]]
        pt["trades"] += 1
        pt["pnl"] = round(pt["pnl"] + t["pnl"], 2)
        pt["wins"] += t["pnl"] > 0
    return {
        "starting_cash": round(starting_cash, 2),
        "ending_equity": round(ending, 2),
        "return_usd": round(ending - starting_cash, 2),
        "return_pct": round((ending - starting_cash) / starting_cash * 100, 3) if starting_cash else 0,
        "cash": _f(last["cash"]) if last else starting_cash,
        "realized_pnl": round(sum(t["pnl"] for t in closed), 2),
        "unrealized_pnl": _f(last["unrealized_pnl"]) if last else 0.0,
        "trades_opened": len(trips),
        "trades_closed": len(closed),
        "open_positions": len(trips) - len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct(equity_rows), 3),
        "per_ticker": dict(per_ticker),
    }
