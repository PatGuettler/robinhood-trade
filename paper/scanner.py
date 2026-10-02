"""
Market scanner — picks which stocks the bot watches each day, so no
hand-made watchlist is needed.

  1. UNIVERSE   ~500 large, liquid US stocks: the S&P 500 (downloaded weekly from
                a public dataset) plus popular ETFs and high-volume names.
  2. DAILY PICK Before each session, rank the universe using ONLY data from
                earlier days (no hindsight, so backtests are honest):
                  * liquid:   price >= min_price, avg daily $ volume >= minimum
                  * in play:  big daily range (volatile enough to hit a target),
                              unusual volume and/or a big move the previous day
                The top `scan_size` become the day's candidates. With Alpaca keys
                (cheap bulk data) every liquid stock is scanned instead.
  3. MOVERS     Live mode also adds the market's current top gainers / most active
                (Yahoo or Alpaca screeners), filtered by the same liquidity rules.

Candidates are recorded per day (candidates.csv) so the dashboard shows what
the bot looked at and why, and verification can replay the exact same choice.
"""

from __future__ import annotations

import csv
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

from paper import marketdata
from paper.marketdata import UA, DailyBar

CANDIDATE_COLS = ["run_id", "mode", "date", "ticker", "source", "score", "reason"]
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

# Always scanned in addition to the S&P 500 (and the fallback if the download fails).
EXTRA = """SPY QQQ IWM DIA SMH SOXX XLK XLF XLE XLV XLI XLY XLP XLU XLB XLC XBI ARKK TQQQ SQQQ SOXL
GLD SLV TLT HYG EEM FXI KWEB GDX USO UNG
PLTR SOFI HOOD COIN MSTR RIVN LCID NIO SHOP SNOW NET CRWD DDOG ZS MDB TEAM U RBLX AFRM UPST
MARA RIOT CLSK HUT SMCI ARM TSM ASML BABA PDD JD BIDU SE MELI NU SPOT ROKU PINS SNAP DKNG
CVNA CHWY ETSY W PATH AI IONQ RGTI QBTS SOUN APP CELH ELF ONON BIRK CAVA DUOL RDDT ALAB
TOST GTLB S OKTA TWLO DOCU ZM LYFT DASH ABNB UBER""".split()

FALLBACK = """AAPL MSFT NVDA AMZN GOOGL GOOG META TSLA AVGO JPM LLY V UNH XOM MA JNJ PG HD COST ABBV MRK
ORCL CVX KO PEP ADBE CRM WMT BAC NFLX AMD TMO MCD CSCO ACN LIN ABT INTC WFC DIS CMCSA DHR TXN
VZ PM AMGN NEE INTU IBM QCOM UNP CAT LOW SPGI GS HON BA AMAT RTX ISRG NOW BKNG PFE GE MS T
BLK SYK ELV DE PLD MDT LRCX UBER ADP VRTX TJX MU CB REGN PANW SBUX MMC GILD ADI C SCHW BMY
CI MDLZ LMT ZTS KLAC SNPS CDNS MO SO DUK BSX FI ETN ICE CME APH EQIX PYPL CRWD ANET ABNB MAR
WM ORLY MCK CVS TGT FDX GM F NKE SLB OXY COP EOG DVN MPC PSX VLO HAL FCX NEM DOW DD ON MRVL
MCHP NXPI WDAY FTNT DELL HPQ HPE WBD PARA EBAY EA TTWO DAL UAL AAL CCL RCL NCLH LVS WYNN MGM""".split()


def _cache_dir() -> Path:
    p = Path(os.environ.get("PAPER_CACHE_DIR", ".cache"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_universe(refresh_days: int = 7) -> list[str]:
    cache = _cache_dir() / "universe.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        if date.fromisoformat(c["as_of"]) >= date.today() - timedelta(days=refresh_days):
            return c["symbols"]
    syms: list[str] = []
    try:
        r = requests.get(SP500_CSV, headers=UA, timeout=20)
        r.raise_for_status()
        syms = [row["Symbol"].strip().upper() for row in csv.DictReader(io.StringIO(r.text))]
    except Exception:  # noqa: BLE001 — fall back to the bundled list
        syms = list(FALLBACK)
    # Share classes like BRK.B are written differently by every vendor — skip them.
    out = sorted({s for s in syms + EXTRA if s and "." not in s and "/" not in s})
    cache.write_text(json.dumps({"as_of": date.today().isoformat(), "symbols": out}))
    return out


def daily_history(symbols: list[str], start: date, end: date, provider: str = "yahoo") -> dict[str, list[DailyBar]]:
    """Daily bars for many symbols, cached on disk for the day (one download per symbol per day)."""
    cache = _cache_dir() / f"daily-{provider}.json"
    blob = json.loads(cache.read_text()) if cache.exists() else {}
    if blob.get("end") != end.isoformat() or blob.get("start", "9999") > start.isoformat():
        blob = {"end": end.isoformat(), "start": start.isoformat(), "bars": {}}
    bars = blob["bars"]
    missing = [s for s in symbols if s not in bars]
    if missing:
        if provider == "alpaca" and marketdata.alpaca_keys():
            got = {}
            for i in range(0, len(missing), 200):
                got.update(marketdata.alpaca_daily(missing[i:i + 200], start, end))
        else:
            def one(sym):
                try:
                    return sym, marketdata.yahoo_daily(sym, start, end)
                except Exception:  # noqa: BLE001
                    return sym, []
            with ThreadPoolExecutor(max_workers=8) as ex:
                got = dict(ex.map(one, missing))
        for sym in missing:
            bars[sym] = [[d.day.isoformat(), d.open, d.high, d.low, d.close, d.volume] for d in got.get(sym, [])]
        cache.write_text(json.dumps(blob))
    return {s: [DailyBar(date.fromisoformat(r[0]), *r[1:]) for r in bars.get(s, [])] for s in symbols}


def _metrics(prior: list[DailyBar]) -> dict | None:
    if len(prior) < 20:
        return None
    last30, last14 = prior[-30:], prior[-14:]
    avg_vol = sum(d.volume for d in last30) / len(last30)
    prev, before = prior[-1], prior[-2]
    return {
        "price": prev.close,
        "dollar_volume": avg_vol * prev.close,
        "rel_volume": prev.volume / avg_vol if avg_vol else 0.0,
        "prev_change": (prev.close / before.close - 1) * 100 if before.close else 0.0,
        "range_pct": sum((d.high - d.low) / d.close for d in last14 if d.close) / len(last14) * 100,
    }


def liquid(m: dict | None, s: dict) -> bool:
    return bool(m) and m["price"] >= float(s.get("min_price", 5)) and \
        m["dollar_volume"] >= float(s.get("min_avg_dollar_volume", 20e6))


def pick_for_day(day: date, daily: dict[str, list[DailyBar]], strategy: dict, scan_all: bool = False) -> list[dict]:
    """Rank the universe for `day` using only bars dated before `day`."""
    scored = []
    for sym, bars in daily.items():
        m = _metrics([d for d in bars if d.day < day])
        if not liquid(m, strategy):
            continue
        score = m["range_pct"] * (0.5 + min(m["rel_volume"], 5.0)) * (1 + min(abs(m["prev_change"]), 15) / 5)
        scored.append({"ticker": sym, "score": round(score, 3),
                       "reason": f"{m['range_pct']:.1f}% avg daily range, {m['rel_volume']:.1f}× volume and "
                                 f"{m['prev_change']:+.1f}% the day before"})
    scored.sort(key=lambda r: (-r["score"], r["ticker"]))
    n = len(scored) if scan_all else int(strategy.get("scan_size", 40))
    return [{**r, "source": "scan"} for r in scored[:n]]


def market_movers(limit: int = 25) -> list[tuple[str, str]]:
    """Current top gainers / most active stocks. Best effort — returns [] if unavailable."""
    out: list[tuple[str, str]] = []
    keys = marketdata.alpaca_keys()
    if keys:
        h = {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]}
        try:
            j = requests.get("https://data.alpaca.markets/v1beta1/screener/stocks/movers",
                             params={"top": limit}, headers=h, timeout=15).json()
            out += [(g["symbol"], "top gainer (Alpaca)") for g in j.get("gainers", [])]
            j = requests.get("https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
                             params={"top": limit, "by": "volume"}, headers=h, timeout=15).json()
            out += [(g["symbol"], "most active (Alpaca)") for g in j.get("most_actives", [])]
        except Exception:  # noqa: BLE001
            pass
    for scr, label in (("day_gainers", "top gainer (Yahoo)"), ("most_actives", "most active (Yahoo)")):
        try:
            r = requests.get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                             params={"scrIds": scr, "count": limit, "formatted": "false"}, headers=UA, timeout=15)
            quotes = ((r.json().get("finance") or {}).get("result") or [{}])[0].get("quotes", [])
            out += [(q["symbol"], label) for q in quotes if q.get("quoteType") in (None, "EQUITY", "ETF")]
        except Exception:  # noqa: BLE001
            pass
    seen, uniq = set(), []
    for sym, why in out:
        sym = sym.upper()
        if sym not in seen and "." not in sym and "^" not in sym:
            seen.add(sym)
            uniq.append((sym, why))
    return uniq


def candidate_rows(picks: list[dict], day: date, run_id: str, mode: str) -> list[dict]:
    return [{"run_id": run_id, "mode": mode, "date": day.isoformat(), "ticker": p["ticker"],
             "source": p["source"], "score": p.get("score", ""), "reason": p.get("reason", "")} for p in picks]


def allowed_by_day(rows: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["date"], set()).add(r["ticker"])
    return out

