"""
Paper-trading engine — one event loop shared by LIVE and BACKTEST modes.

Bars are processed strictly in time order. For every bar of every ticker:

  1. ENTRY FILL   A buy signalled on the previous bar fills at THIS bar's open
                  (+ slippage). Signals never fill on the bar that produced
                  them, so there is no look-ahead.
  2. EXITS        Limit-sell target: fills at the target when bar.high >= target
                  (or at the open if the bar gaps above it).
                  Optional stop-loss: fills at the stop (- slippage) when
                  bar.low <= stop (or at the open on a gap down).
                  If a single bar touches BOTH, the stop is assumed to have hit
                  first (worst case — we can't see inside a bar).
  3. TIME EXITS   Optional close-at-end-of-day / max-hold-days sell at the
                  close of the session's last bar (- slippage).
  4. SIGNALS      Same trigger as the live bot: |move vs previous close| >=
                  min_change_pct AND volume >= min_volume_multiplier x 30-day
                  average (pro-rated by time of day by default). Triggers are
                  approved by fixed rules or, optionally, by Claude.

Because live mode just processes the newest bars with this same loop, a
backtest replay of the same days with the same config reproduces the live
trades exactly — verify.py uses that as an accuracy check.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
from typing import Callable

from paper.config import strategy_hash
from paper.marketdata import ET, Bar, MarketData

SESSION_MINUTES = 390

FILL_COLS = [
    "account_id", "run_id", "mode", "fill_id", "trade_id", "timestamp", "ticker", "side",
    "qty", "price", "amount", "fee", "reason", "target", "stop", "realized_pnl", "cash_after",
    "bar_ts", "bar_open", "bar_high", "bar_low", "bar_close",
    "signal_change_pct", "signal_vol_ratio", "data_source", "strategy_hash",
]
EQUITY_COLS = [
    "account_id", "run_id", "mode", "timestamp", "cash", "positions_value", "equity",
    "realized_pnl", "unrealized_pnl", "open_positions",
]
SIGNAL_COLS = [
    "account_id", "run_id", "mode", "timestamp", "ticker", "price", "prev_close",
    "change_pct", "vol_ratio", "decision", "decided_by", "confidence", "reason", "target_price",
]
POSITION_COLS = [
    "account_id", "run_id", "trade_id", "ticker", "qty", "entry_price", "entry_ts", "cost_basis",
    "target", "stop", "mark_price", "mark_ts", "market_value", "unrealized_pnl", "unrealized_pct",
]

Decider = Callable[[str, dict, dict], dict]


def new_state(account_id: int, starting_cash: float, start_ts: datetime | None = None) -> dict:
    return {
        "account_id": account_id,
        "starting_cash": round(float(starting_cash), 2),
        "cash": round(float(starting_cash), 2),
        "realized_pnl": 0.0,
        "positions": {},          # ticker -> position dict
        "pending": {},            # ticker -> pending entry dict
        "marks": {},              # ticker -> {"price", "ts"}
        "entered_on": {},         # ticker -> "YYYY-MM-DD" of last entry
        "last_signal": {},        # ticker -> iso ts of last logged non-buy signal
        "start_ts": start_ts.isoformat() if start_ts else None,
        "last_bar_ts": None,
        "next_trade_id": 1,
        "next_fill_id": 1,
    }


def _r(x: float, n: int = 4) -> float:
    return round(float(x), n)


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Engine:
    def __init__(self, cfg: dict, state: dict, md: MarketData, *, mode: str, run_id: str,
                 decider: Decider | None = None):
        self.cfg = cfg
        self.s = cfg["strategy"]
        self.x = cfg["execution"]
        self.state = state
        self.md = md
        self.mode = mode
        self.run_id = run_id
        self.decider = decider
        self.interval = timedelta(minutes=md.interval_minutes)
        self.hash = strategy_hash(cfg)
        self.fills: list[dict] = []
        self.signals: list[dict] = []
        self.equity: list[dict] = []
        self.claude_calls = 0
        self._ctx = self._daily_context()
        self._cumvol = self._cumulative_volume()

    # ── Pre-computation ──────────────────────────────────────────────────────

    def _daily_context(self) -> dict:
        """(ticker, date) -> prev_close, avg_volume — strictly from days BEFORE `date`."""
        ctx = {}
        for sym, bars in self.md.intraday.items():
            daily = sorted(self.md.daily.get(sym, []), key=lambda d: d.day)
            for day in sorted({b.ts.date() for b in bars}):
                prior = [d for d in daily if d.day < day]
                if not prior:
                    continue
                last30 = prior[-30:]
                ctx[(sym, day)] = {
                    "prev_close": prior[-1].close,
                    "avg_volume": sum(d.volume for d in last30) / len(last30),
                }
        return ctx

    def _cumulative_volume(self) -> dict:
        out = {}
        for sym, bars in self.md.intraday.items():
            run, day = 0.0, None
            for b in sorted(bars, key=lambda b: b.ts):
                if b.ts.date() != day:
                    run, day = 0.0, b.ts.date()
                run += b.volume
                out[(sym, b.ts)] = run
        return out

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _slip(self, price: float, side: str) -> float:
        bps = float(self.x.get("slippage_bps", 0)) / 10_000
        return price * (1 + bps) if side == "buy" else price * (1 - bps)

    def _fee(self) -> float:
        return float(self.x.get("fee_per_trade", 0) or 0)

    def _is_last_bar(self, bar: Bar) -> bool:
        return (bar.ts + self.interval).time() >= dtime(16, 0)

    def _target(self, fill_price: float, claude_target) -> float:
        mode = self.s["sell_mode"]
        if mode == "claude":
            try:
                t = float(claude_target)
                if t > fill_price:
                    return t
            except (TypeError, ValueError):
                pass
            return fill_price + max(2.0, fill_price * 0.02)   # same fallback as the live bot
        if mode == "percent":
            return fill_price * (1 + self.s["sell_percent"] / 100)
        return fill_price + self.s["profit_target"]

    def _stop(self, fill_price: float) -> float | None:
        pct = self.s.get("stop_loss_pct", 0) or 0
        return fill_price * (1 - pct / 100) if pct > 0 else None

    def _positions_value(self) -> float:
        total = 0.0
        for sym, p in self.state["positions"].items():
            mark = self.state["marks"].get(sym, {}).get("price", p["entry_price"])
            total += p["qty"] * mark
        return total

    def _emit_fill(self, *, ts, sym, side, qty, price, reason, bar: Bar, pos: dict,
                   realized=None, sig=None):
        st = self.state
        fid = st["next_fill_id"]
        st["next_fill_id"] += 1
        self.fills.append({
            "account_id": st["account_id"], "run_id": self.run_id, "mode": self.mode,
            "fill_id": fid, "trade_id": pos["trade_id"], "timestamp": ts.isoformat(),
            "ticker": sym, "side": side, "qty": _r(qty, 6), "price": _r(price),
            "amount": _r(qty * price, 2), "fee": _r(self._fee(), 2), "reason": reason,
            "target": _r(pos["target"]), "stop": _r(pos["stop"]) if pos.get("stop") else "",
            "realized_pnl": _r(realized, 2) if realized is not None else "",
            "cash_after": _r(st["cash"], 2),
            "bar_ts": bar.ts.isoformat(), "bar_open": _r(bar.open), "bar_high": _r(bar.high),
            "bar_low": _r(bar.low), "bar_close": _r(bar.close),
            "signal_change_pct": _r(sig["change_pct"], 2) if sig else "",
            "signal_vol_ratio": _r(sig["vol_ratio"], 2) if sig else "",
            "data_source": self.md.source, "strategy_hash": self.hash,
        })

    def _emit_signal(self, bar: Bar, sym: str, sig: dict, decision: str, by: str,
                     reason: str, confidence: str = "", target=None):
        self.signals.append({
            "account_id": self.state["account_id"], "run_id": self.run_id, "mode": self.mode,
            "timestamp": bar.ts.isoformat(), "ticker": sym, "price": _r(bar.close),
            "prev_close": _r(sig["prev_close"]), "change_pct": _r(sig["change_pct"], 2),
            "vol_ratio": _r(sig["vol_ratio"], 2), "decision": decision, "decided_by": by,
            "confidence": confidence, "reason": reason[:300],
            "target_price": _r(target) if target else "",
        })

    # ── Per-bar steps ────────────────────────────────────────────────────────

    def _fill_entry(self, sym: str, bar: Bar):
        st = self.state
        pend = st["pending"].pop(sym, None)
        if not pend:
            return
        if bar.ts.date().isoformat() != pend["day"]:
            return   # day order — expired overnight
        size = self.s["position_size"]
        if st["cash"] < size + self._fee():
            return
        price = self._slip(bar.open, "buy")
        qty = size / price
        st["cash"] -= qty * price + self._fee()
        tid = st["next_trade_id"]
        st["next_trade_id"] += 1
        pos = {
            "trade_id": tid, "qty": qty, "entry_price": price, "entry_ts": bar.ts.isoformat(),
            "cost_basis": qty * price + self._fee(), "target": self._target(price, pend.get("claude_target")),
            "stop": self._stop(price), "sessions": 1, "last_day": bar.ts.date().isoformat(),
        }
        st["positions"][sym] = pos
        st["entered_on"][sym] = bar.ts.date().isoformat()
        self._emit_fill(ts=bar.ts, sym=sym, side="BUY", qty=qty, price=price,
                        reason=pend.get("reason", "signal"), bar=bar, pos=pos, sig=pend["sig"])

    def _close(self, sym: str, bar: Bar, price: float, reason: str):
        st = self.state
        pos = st["positions"].pop(sym)
        proceeds = pos["qty"] * price - self._fee()
        realized = proceeds - pos["cost_basis"]
        st["cash"] += proceeds
        st["realized_pnl"] += realized
        self._emit_fill(ts=bar.ts, sym=sym, side="SELL", qty=pos["qty"], price=price,
                        reason=reason, bar=bar, pos=pos, realized=realized)

    def _check_exits(self, sym: str, bar: Bar, just_opened: bool):
        pos = self.state["positions"].get(sym)
        if not pos:
            return
        day = bar.ts.date().isoformat()
        if pos["last_day"] != day:
            pos["sessions"] += 1
            pos["last_day"] = day
        tgt, stop = pos["target"], pos.get("stop")

        if not just_opened:
            if stop and bar.open <= stop:
                return self._close(sym, bar, self._slip(bar.open, "sell"), "stop (gap down)")
            if bar.open >= tgt:
                return self._close(sym, bar, bar.open, "target (gap up)")
        if stop and bar.low <= stop:
            return self._close(sym, bar, self._slip(stop, "sell"), "stop")
        if bar.high >= tgt:
            return self._close(sym, bar, tgt, "target")
        if self._is_last_bar(bar):
            if self.s.get("close_at_eod"):
                return self._close(sym, bar, self._slip(bar.close, "sell"), "end of day")
            mh = int(self.s.get("max_hold_days") or 0)
            if mh and pos["sessions"] >= mh:
                return self._close(sym, bar, self._slip(bar.close, "sell"), f"max hold {mh}d")

    def _signal(self, sym: str, bar: Bar) -> dict | None:
        c = self._ctx.get((sym, bar.ts.date()))
        if not c or not c["prev_close"]:
            return None
        end = bar.ts + self.interval
        change = (bar.close - c["prev_close"]) / c["prev_close"] * 100
        cum = self._cumvol.get((sym, bar.ts), 0.0)
        avg = c["avg_volume"]
        if avg > 0:
            if self.s.get("volume_mode") == "full_day":
                expected = avg
            else:
                opened = datetime.combine(bar.ts.date(), dtime(9, 30), ET)
                elapsed = max((end - opened).total_seconds() / 60, 1)
                expected = avg * min(elapsed / SESSION_MINUTES, 1.0)
            ratio = cum / expected
            vol_ok = ratio >= self.s["min_volume_multiplier"]
        else:
            ratio, vol_ok = 0.0, True
        if abs(change) < self.s["min_change_pct"] or not vol_ok:
            return None
        return {"change_pct": change, "vol_ratio": ratio, "prev_close": c["prev_close"],
                "avg_volume": avg, "volume": cum}

    def _throttled(self, sym: str, bar: Bar, minutes: int = 30) -> bool:
        last = self.state["last_signal"].get(sym)
        if last and bar.ts - datetime.fromisoformat(last) < timedelta(minutes=minutes):
            return True
        self.state["last_signal"][sym] = bar.ts.isoformat()
        return False

    def _evaluate(self, sym: str, bar: Bar):
        st = self.state
        end_t = (bar.ts + self.interval).time()
        if end_t < _hhmm(self.s["entry_start"]) or end_t > _hhmm(self.s["last_entry_time"]):
            return
        if sym in st["positions"] or sym in st["pending"]:
            return
        today = bar.ts.date().isoformat()
        if self.s.get("one_entry_per_ticker_per_day", True) and st["entered_on"].get(sym) == today:
            return
        sig = self._signal(sym, bar)
        if not sig:
            return

        if len(st["positions"]) + len(st["pending"]) >= int(self.s["max_open_positions"]):
            if not self._throttled(sym, bar):
                self._emit_signal(bar, sym, sig, "SKIP", "rules", "max open positions reached")
            return
        if st["cash"] < self.s["position_size"] + self._fee():
            if not self._throttled(sym, bar):
                self._emit_signal(bar, sym, sig, "SKIP", "rules", "not enough paper cash")
            return
        if self.s.get("direction", "up") == "up" and sig["change_pct"] < 0:
            if not self._throttled(sym, bar, minutes=24 * 60):
                self._emit_signal(bar, sym, sig, "PASS", "rules", "down move — strategy only buys strength")
            return

        decision = {"buy": True, "reason": "trigger met", "confidence": "", "target_price": None}
        by = "rules"
        if self.decider is not None:
            if self._throttled(sym, bar):
                return
            limit = int(self.s.get("claude_max_calls_per_run", 20))
            if self.claude_calls >= limit:
                self._emit_signal(bar, sym, sig, "SKIP", "claude", f"Claude call limit ({limit}) reached this run")
                return
            self.claude_calls += 1
            quote = {"price": bar.close, "change_pct": sig["change_pct"], "volume": sig["volume"],
                     "avg_volume": sig["avg_volume"], "vol_ratio": sig["vol_ratio"],
                     "timestamp": bar.ts.isoformat()}
            decision = self.decider(sym, quote, self.cfg)
            by = decision.get("decided_by", "claude")

        if decision.get("buy"):
            st["pending"][sym] = {"day": today, "sig": sig, "reason": f"{by}: {decision.get('reason', '')}"[:200],
                                  "claude_target": decision.get("target_price")}
            self._emit_signal(bar, sym, sig, "BUY", by, decision.get("reason", ""),
                              decision.get("confidence", ""), decision.get("target_price"))
        else:
            self._emit_signal(bar, sym, sig, "PASS", by, decision.get("reason", ""),
                              decision.get("confidence", ""))

    def _snapshot(self, ts: datetime):
        st = self.state
        pv = self._positions_value()
        cost = sum(p["cost_basis"] for p in st["positions"].values())
        self.equity.append({
            "account_id": st["account_id"], "run_id": self.run_id, "mode": self.mode,
            "timestamp": ts.isoformat(), "cash": _r(st["cash"], 2), "positions_value": _r(pv, 2),
            "equity": _r(st["cash"] + pv, 2), "realized_pnl": _r(st["realized_pnl"], 2),
            "unrealized_pnl": _r(pv - cost, 2), "open_positions": len(st["positions"]),
        })

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self, until: datetime | None = None) -> int:
        """Process every bar after state.last_bar_ts (and before `until`). Returns bars processed."""
        st = self.state
        after = datetime.fromisoformat(st["last_bar_ts"]) if st["last_bar_ts"] else None
        start = datetime.fromisoformat(st["start_ts"]) if st.get("start_ts") else None

        by_ts: dict[datetime, list[tuple[str, Bar]]] = defaultdict(list)
        for sym, bars in self.md.intraday.items():
            for b in bars:
                if after and b.ts <= after:
                    continue
                if start and b.ts < start:
                    continue
                if until and b.ts + self.interval > until:
                    continue   # bar not finished yet
                by_ts[b.ts].append((sym, b))

        processed = 0
        for ts in sorted(by_ts):
            for sym, bar in sorted(by_ts[ts], key=lambda x: x[0]):
                self._fill_entry(sym, bar)
                just_opened = st["positions"].get(sym, {}).get("entry_ts") == bar.ts.isoformat()
                self._check_exits(sym, bar, just_opened=just_opened)
                st["marks"][sym] = {"price": bar.close, "ts": bar.ts.isoformat()}
                self._evaluate(sym, bar)
                processed += 1
            st["last_bar_ts"] = ts.isoformat()
            self._snapshot(ts)
        st["cash"] = _r(st["cash"], 6)
        st["realized_pnl"] = _r(st["realized_pnl"], 6)
        return processed

    def positions_rows(self) -> list[dict]:
        rows = []
        for sym, p in sorted(self.state["positions"].items()):
            m = self.state["marks"].get(sym, {})
            mark = m.get("price", p["entry_price"])
            mv = p["qty"] * mark
            rows.append({
                "account_id": self.state["account_id"], "run_id": self.run_id,
                "trade_id": p["trade_id"], "ticker": sym,
                "qty": _r(p["qty"], 6), "entry_price": _r(p["entry_price"]), "entry_ts": p["entry_ts"],
                "cost_basis": _r(p["cost_basis"], 2), "target": _r(p["target"]),
                "stop": _r(p["stop"]) if p.get("stop") else "", "mark_price": _r(mark),
                "mark_ts": m.get("ts", ""), "market_value": _r(mv, 2),
                "unrealized_pnl": _r(mv - p["cost_basis"], 2),
                "unrealized_pct": _r((mv - p["cost_basis"]) / p["cost_basis"] * 100, 2),
            })
        return rows


def clone_state(state: dict) -> dict:
    return copy.deepcopy(state)
