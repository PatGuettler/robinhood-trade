"""
Deterministic synthetic market data — for tests and for trying the dashboard
offline (`python -m paper backtest --synthetic`). Never used for real results;
runs made with it are tagged data_source=synthetic.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time as dtime, timedelta

from paper.marketdata import ET, Bar, DailyBar, MarketData

# Made-up tickers used as the "market" when scanning synthetic data.
UNIVERSE = [f"SY{a}{b}" for a in "ABCDEF" for b in "ABCDEFGHIJ"]
ANCHOR = date(2026, 6, 1)   # paths start here, so any date range gives the same prices


def trading_days(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate(symbols: list[str], start: date, end: date, seed: int = 7,
             interval_minutes: int = 5) -> MarketData:
    md = MarketData(source="synthetic", interval_minutes=interval_minutes)
    for sym in symbols:
        rng = random.Random(f"{seed}-{sym}")
        price = 20 + rng.random() * 280          # depends only on the ticker, not list order
        avg_vol = 5_000_000
        daily, intraday = [], []
        for d in trading_days(min(start - timedelta(days=60), ANCHOR), end):
            day_open = price * (1 + rng.gauss(0, 0.004))
            drift = rng.choice([0.0, 0.0, 0.0006, -0.0004, 0.0012])   # some days trend hard
            vol_mult = rng.uniform(0.7, 2.2)
            p, hi, lo, vol_total = day_open, day_open, day_open, 0.0
            bars = []
            t = datetime.combine(d, dtime(9, 30), ET)
            for _ in range(390 // interval_minutes):
                o = p
                c = o * (1 + drift + rng.gauss(0, 0.0025))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.0012)))
                l = min(o, c) * (1 - abs(rng.gauss(0, 0.0012)))
                v = avg_vol / 78 * vol_mult * rng.uniform(0.5, 1.5)
                bars.append(Bar(t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(v)))
                hi, lo, vol_total, p = max(hi, h), min(lo, l), vol_total + v, c
                t += timedelta(minutes=interval_minutes)
            if d >= start - timedelta(days=60):
                daily.append(DailyBar(d, round(day_open, 2), round(hi, 2), round(lo, 2), round(p, 2), round(vol_total)))
            if d >= start:
                intraday.extend(bars)
            price = p
        md.daily[sym] = daily
        md.intraday[sym] = intraday
    return md
