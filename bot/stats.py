"""
Persistent trade statistics — saved to stats.json in the app directory.
Survives bot restarts. Contains no sensitive data.

Tracks:
  - Alerts sent / declined / expired
  - Trades executed (by TradeBot)
  - Wins and losses (from TradeBot's records — Robinhood fills tracked separately)
  - P&L by day for chart display
  - Trade history log (last 200 entries)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

STATS_FILE = Path(__file__).parent.parent / "stats.json"

_DEFAULTS = {
    "all_time": {
        "alerts_sent":        0,
        "alerts_declined":    0,
        "alerts_expired":     0,
        "claude_buy_signals": 0,
        "claude_no_signals":  0,
        "trades_executed":    0,
        "wins":               0,
        "losses":             0,
        "total_profit":       0.0,
        "total_invested":     0.0,
    },
    "daily":         {},   # "YYYY-MM-DD" -> {profit, wins, losses, trades}
    "trade_history": [],   # last 200 trades
}


def _load() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULTS))


def _save(data: dict):
    try:
        STATS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[stats] Save error: {e}")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ensure_day(data: dict, day: str):
    if day not in data["daily"]:
        data["daily"][day] = {"profit": 0.0, "wins": 0, "losses": 0, "trades": 0}


# ── Public recording functions ────────────────────────────────────────────────

def record_alert_sent(ticker: str, price: float, claude_says_buy: bool):
    d = _load()
    d["all_time"]["alerts_sent"] += 1
    if claude_says_buy:
        d["all_time"]["claude_buy_signals"] += 1
    else:
        d["all_time"]["claude_no_signals"] += 1
    _save(d)


def record_alert_declined(ticker: str):
    d = _load()
    d["all_time"]["alerts_declined"] += 1
    _append_history(d, ticker, "declined", 0, 0, 0)
    _save(d)


def record_alert_expired(ticker: str):
    d = _load()
    d["all_time"]["alerts_expired"] += 1
    _save(d)


def record_trade_executed(ticker: str, buy_price: float, target_price: float, position_size: float):
    d = _load()
    d["all_time"]["trades_executed"] += 1
    d["all_time"]["total_invested"] += position_size
    _append_history(d, ticker, "open", buy_price, target_price, position_size)
    _save(d)


def record_trade_filled(ticker: str, sell_price: float, buy_price: float, shares: float):
    profit = (sell_price - buy_price) * shares
    d = _load()
    today = _today()
    _ensure_day(d, today)
    d["daily"][today]["profit"] += profit
    d["daily"][today]["trades"] += 1
    if profit >= 0:
        d["all_time"]["wins"] += 1
        d["daily"][today]["wins"] += 1
    else:
        d["all_time"]["losses"] += 1
        d["daily"][today]["losses"] += 1
    d["all_time"]["total_profit"] += profit
    # Close the matching open entry
    for t in reversed(d["trade_history"]):
        if t["ticker"] == ticker and t["status"] == "open":
            t["status"]    = "win" if profit >= 0 else "loss"
            t["sell_price"] = sell_price
            t["profit"]    = round(profit, 2)
            t["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            break
    _save(d)


def _append_history(d: dict, ticker: str, status: str, buy_price: float, target_price: float, position_size: float):
    entry = {
        "date":          _today(),
        "time":          datetime.now().strftime("%H:%M"),
        "ticker":        ticker,
        "buy_price":     round(buy_price, 2),
        "target_price":  round(target_price, 2),
        "position_size": round(position_size, 2),
        "status":        status,
        "sell_price":    None,
        "profit":        None,
        "closed_at":     None,
    }
    d["trade_history"].append(entry)
    if len(d["trade_history"]) > 200:
        d["trade_history"] = d["trade_history"][-200:]


# ── Public read functions ─────────────────────────────────────────────────────

def get_all() -> dict:
    return _load()


def get_today_stats() -> dict:
    d = _load()
    return d["daily"].get(_today(), {"profit": 0.0, "wins": 0, "losses": 0, "trades": 0})


def get_daily_pnl_series(days: int = 14) -> list:
    """Returns last N days of P&L for chart display."""
    d = _load()
    result = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = d["daily"].get(day, {})
        result.append({
            "date":   day,
            "profit": round(day_data.get("profit", 0.0), 2),
            "trades": day_data.get("trades", 0),
        })
    return result
