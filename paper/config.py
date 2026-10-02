"""
Paper-trading configuration.

The config lives in `paper_config.json` at the repo root. The GitHub Pages
setup page edits it through the GitHub API; the scheduled workflow reads it.
It contains NO secrets — API keys and Google tokens live in GitHub Actions
secrets and reach the bot as environment variables.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "paper_config.json"

DEFAULTS: dict = {
    "account": {
        # Bump `id` to wipe the live paper account and start over.
        "id": 1,
        "starting_cash": 10000.0,
    },
    "strategy": {
        # market    = the scanner picks stocks every day (watchlist = always include)
        # watchlist = only trade the tickers listed below
        "universe": "market",
        "scan_size": 40,                  # stocks watched per day (all liquid ones with Alpaca keys)
        "include_market_movers": True,    # live: also watch today's top gainers / most active
        "min_price": 5.0,                 # skip penny stocks
        "min_avg_dollar_volume": 20_000_000,
        "max_change_pct": 12.0,           # don't chase stocks already up more than this (0 = off)
        "require_above_vwap": True,       # only buy strength that is holding above VWAP
        "watchlist": [],
        "position_size": 1000.0,          # $ per trade
        "max_open_positions": 5,
        "min_change_pct": 1.5,            # % move vs previous close
        "min_volume_multiplier": 1.2,     # volume vs 30-day average
        "volume_mode": "prorated",        # prorated | full_day
        "direction": "up",                # up | both (both = also buy dips)
        "sell_mode": "dollar",            # dollar | percent | claude
        "profit_target": 2.0,             # $ per share when sell_mode=dollar
        "sell_percent": 2.0,              # % when sell_mode=percent
        "stop_loss_pct": 0.0,             # 0 = never sell at a loss (original bot)
        "close_at_eod": False,            # sell everything at the last bar of the day
        "max_hold_days": 0,               # 0 = hold until target / stop
        "one_entry_per_ticker_per_day": True,
        "entry_start": "09:30",
        "last_entry_time": "15:45",
        "use_claude": False,              # ask Claude to approve each trigger
        "claude_model": "claude-opus-5-5",
        "claude_max_calls_per_run": 20,
        "strategy_text": "momentum breakout on high volume with a clear catalyst",
    },
    "execution": {
        "slippage_bps": 5.0,              # applied to market buys / stop & EOD sells
        "fee_per_trade": 0.0,
    },
    "market_data": {
        "provider": "yahoo",              # yahoo | alpaca
        "interval_minutes": 5,
    },
    "storage": {
        "backend": "github",              # github (paper-data branch) | drive
        "drive_folder_id": "",
        "google_client_id": "",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize(cfg: dict) -> dict:
    cfg = _merge(DEFAULTS, cfg or {})
    wl = cfg["strategy"]["watchlist"]
    if isinstance(wl, str):
        wl = wl.split(",")
    cfg["strategy"]["watchlist"] = sorted({t.strip().upper() for t in wl if t.strip()})
    for k in ("position_size", "min_change_pct", "min_volume_multiplier", "profit_target",
              "sell_percent", "stop_loss_pct", "min_price", "min_avg_dollar_volume", "max_change_pct"):
        cfg["strategy"][k] = float(cfg["strategy"][k])
    cfg["account"]["starting_cash"] = float(cfg["account"]["starting_cash"])
    cfg["account"]["id"] = int(cfg["account"]["id"])
    return cfg


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    raw = json.loads(p.read_text()) if p.exists() else {}
    return normalize(raw)


def strategy_hash(cfg: dict) -> str:
    """Short fingerprint of everything that affects trading decisions."""
    blob = json.dumps({"s": cfg["strategy"], "e": cfg["execution"],
                       "m": cfg["market_data"]}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]
