"""
Market data for paper trading.

Providers return regular-session intraday bars (default 5-minute) plus daily
bars. All timestamps are timezone-aware America/New_York and mark the START
of the bar.

  yahoo  — no API key. Unofficial endpoint; 5m bars available for ~60 days.
  alpaca — free account keys (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY),
           IEX feed. Used automatically as a fallback when keys are present.
  stooq  — daily bars only; used by verify.py as an independent price source.
"""

from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TradeBot-Paper/1.0"}


@dataclass(frozen=True)
class Bar:
    ts: datetime          # bar start, ET
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketData:
    intraday: dict[str, list[Bar]] = field(default_factory=dict)
    daily: dict[str, list[DailyBar]] = field(default_factory=dict)
    source: str = ""
    interval_minutes: int = 5
    errors: list[str] = field(default_factory=list)


def in_session(ts: datetime) -> bool:
    t = ts.astimezone(ET).time()
    return SESSION_OPEN <= t < SESSION_CLOSE


# ── Yahoo ─────────────────────────────────────────────────────────────────────

def _yahoo_chart(symbol: str, interval: str, start: datetime, end: datetime) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval": interval,
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "includePrePost": "false",
        "events": "div,splits",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code == 429:
                raise RuntimeError("Yahoo rate limited (429)")
            r.raise_for_status()
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                raise RuntimeError(f"Yahoo returned no data for {symbol}")
            return res[0]
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Yahoo {symbol} {interval}: {last_err}")


def _yahoo_rows(chart: dict):
    ts = chart.get("timestamp") or []
    q = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    for i, t in enumerate(ts):
        o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        yield t, float(o), float(h), float(l), float(c), float(v or 0)


def yahoo_intraday(symbol: str, start: datetime, end: datetime, interval_minutes: int = 5) -> list[Bar]:
    chart = _yahoo_chart(symbol, f"{interval_minutes}m", start, end)
    bars = []
    for t, o, h, l, c, v in _yahoo_rows(chart):
        ts = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(ET)
        if in_session(ts):
            bars.append(Bar(ts, o, h, l, c, v))
    return bars


def yahoo_daily(symbol: str, start: date, end: date) -> list[DailyBar]:
    s = datetime.combine(start, dtime(0, 0), ET)
    e = datetime.combine(end + timedelta(days=1), dtime(0, 0), ET)
    chart = _yahoo_chart(symbol, "1d", s, e)
    out = []
    for t, o, h, l, c, v in _yahoo_rows(chart):
        out.append(DailyBar(datetime.fromtimestamp(t, tz=timezone.utc).astimezone(ET).date(), o, h, l, c, v))
    return out


# ── Alpaca ────────────────────────────────────────────────────────────────────

def alpaca_keys() -> tuple[str, str] | None:
    k, s = os.environ.get("ALPACA_API_KEY_ID", ""), os.environ.get("ALPACA_API_SECRET_KEY", "")
    return (k, s) if k and s else None


def _alpaca_bars(symbols: list[str], timeframe: str, start: datetime, end: datetime) -> dict[str, list[dict]]:
    keys = alpaca_keys()
    if not keys:
        raise RuntimeError("Alpaca keys not configured")
    headers = {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]}
    params = {
        "symbols": ",".join(symbols), "timeframe": timeframe,
        "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "limit": 10000, "feed": "iex", "adjustment": "raw",
    }
    out: dict[str, list[dict]] = {}
    while True:
        r = requests.get("https://data.alpaca.markets/v2/stocks/bars", params=params,
                         headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        for sym, rows in (j.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        tok = j.get("next_page_token")
        if not tok:
            return out
        params["page_token"] = tok


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)


def alpaca_intraday(symbols, start, end, interval_minutes=5) -> dict[str, list[Bar]]:
    raw = _alpaca_bars(symbols, f"{interval_minutes}Min", start, end)
    return {
        sym: [b for b in (Bar(_parse_iso(x["t"]), x["o"], x["h"], x["l"], x["c"], x["v"]) for x in rows)
              if in_session(b.ts)]
        for sym, rows in raw.items()
    }


def alpaca_daily(symbols, start: date, end: date) -> dict[str, list[DailyBar]]:
    s = datetime.combine(start, dtime(0, 0), ET)
    e = datetime.combine(end + timedelta(days=1), dtime(0, 0), ET)
    raw = _alpaca_bars(symbols, "1Day", s, e)
    return {sym: [DailyBar(_parse_iso(x["t"]).date(), x["o"], x["h"], x["l"], x["c"], x["v"]) for x in rows]
            for sym, rows in raw.items()}


# ── Stooq (independent daily reference for verification) ─────────────────────

def stooq_daily(symbol: str) -> list[DailyBar]:
    r = requests.get("https://stooq.com/q/d/l/", params={"s": f"{symbol.lower()}.us", "i": "d"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows or "Close" not in rows[0]:
        raise RuntimeError(f"Stooq returned no data for {symbol}")
    return [DailyBar(date.fromisoformat(x["Date"]), float(x["Open"]), float(x["High"]),
                     float(x["Low"]), float(x["Close"]), float(x.get("Volume") or 0)) for x in rows]


# ── Unified fetch ─────────────────────────────────────────────────────────────

def fetch(symbols: list[str], start: date, end: date, provider: str = "yahoo",
          interval_minutes: int = 5, daily_lookback_days: int = 60,
          daily: dict[str, list[DailyBar]] | None = None) -> MarketData:
    """
    Fetch intraday bars for [start, end] (ET dates, inclusive) and daily bars
    going back `daily_lookback_days` calendar days before `start` (for the
    previous close and 30-day average volume). Pass `daily` to reuse daily bars
    already downloaded (e.g. by the scanner).
    """
    from concurrent.futures import ThreadPoolExecutor

    md = MarketData(interval_minutes=interval_minutes)
    s_dt = datetime.combine(start, dtime(0, 0), ET)
    e_dt = datetime.combine(end + timedelta(days=1), dtime(0, 0), ET)
    d_start = start - timedelta(days=daily_lookback_days)

    order = [provider] + [p for p in ("yahoo", "alpaca") if p != provider]
    for prov in order:
        if prov == "alpaca" and not alpaca_keys():
            continue
        try:
            if prov == "alpaca":
                md.intraday = {}
                for i in range(0, len(symbols), 200):
                    md.intraday.update(alpaca_intraday(symbols[i:i + 200], s_dt, e_dt, interval_minutes))
                md.daily = daily if daily is not None else alpaca_daily(symbols, d_start, end)
            else:
                errors: list[str] = []

                def one(sym):
                    try:
                        bars = yahoo_intraday(sym, s_dt, e_dt, interval_minutes)
                        d = daily.get(sym, []) if daily is not None else yahoo_daily(sym, d_start, end)
                        return sym, bars, d
                    except Exception as e:  # noqa: BLE001 — one bad ticker shouldn't kill the run
                        errors.append(str(e))
                        return sym, [], []

                with ThreadPoolExecutor(max_workers=8) as ex:
                    res = list(ex.map(one, symbols))
                md.intraday = {s: b for s, b, _ in res if b}
                md.daily = {s: d for s, _, d in res if d}
                md.errors.extend(errors)
                if not md.intraday and errors:
                    raise RuntimeError("; ".join(errors[:3]))
            md.source = prov
            return md
        except Exception as e:  # noqa: BLE001
            md.errors.append(f"{prov}: {e}")
    raise RuntimeError("All market data providers failed: " + " | ".join(md.errors[-3:]))
