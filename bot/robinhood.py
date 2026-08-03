"""
Robinhood Agentic Trading MCP client.

Endpoint: https://agent.robinhood.com/mcp/trading

Uses JSON-RPC `tools/call` against the official MCP tools:
  Read:  get_accounts, get_portfolio, get_equity_positions,
         get_equity_quotes, get_equity_orders
  Trade: review_equity_order, place_equity_order, cancel_equity_order

Background sync every 60s caches account data to robinhood_cache.json.
Order placement is used by the engine after email approval.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests as req

logger = logging.getLogger("robinhood")

DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
CACHE_FILE = Path(__file__).parent.parent / "robinhood_cache.json"
SYNC_INTERVAL = 60

_cache: dict = {
    "last_sync": None,
    "sync_status": "never_synced",  # ok | error | auth_error | no_token | never_synced
    "sync_error": None,
    "accounts": [],
    "positions": [],
    "orders": [],
    "balances": {},
    "stats": {},
}

_sync_thread: threading.Thread | None = None
_stop_sync = threading.Event()
_token = ""
_mcp_url = DEFAULT_MCP_URL
_dry_run = True
_lock = threading.Lock()


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache():
    global _cache
    if CACHE_FILE.exists():
        try:
            _cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            pass


def _save_cache():
    try:
        CACHE_FILE.write_text(json.dumps(_cache, indent=2, default=str))
    except Exception as e:
        logger.warning("Cache write failed: %s", e)


def get_cache() -> dict:
    return _cache


def is_connected() -> bool:
    return bool(_token) and _cache.get("sync_status") not in ("no_token", "never_synced")


# ── MCP JSON-RPC helper ───────────────────────────────────────────────────────

def _mcp_call(tool: str, arguments: dict | None = None) -> Any:
    """Call a Robinhood MCP tool via JSON-RPC POST."""
    if not _token:
        raise ValueError("No Robinhood MCP auth token configured")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }

    resp = req.post(
        _mcp_url,
        json=payload,
        headers={
            "Authorization": f"Bearer {_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=20,
    )

    if resp.status_code == 401:
        raise PermissionError("Robinhood MCP token is invalid or expired")
    if resp.status_code == 403:
        raise PermissionError(
            "Access denied — is Agentic Trading enabled on your account?"
        )

    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"MCP error: {data['error']}")

    result = data.get("result", {})
    # MCP content blocks: [{type, text}, ...]
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
    # Some servers return structuredContent directly
    if "structuredContent" in result:
        return result["structuredContent"]
    return result if result else {}


# ── Quote normalization ───────────────────────────────────────────────────────

def _pick(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return default


def _normalize_quote(raw: dict, fallback_symbol: str = "") -> dict:
    """Normalize heterogeneous MCP quote shapes into engine-friendly fields."""
    symbol = str(_pick(raw, "symbol", "ticker", "Symbol", default=fallback_symbol) or "").upper()
    price = float(_pick(raw, "last_trade_price", "last_price", "price", "mark_price",
                        "ask_price", "bid_price", default=0) or 0)
    prev = float(_pick(raw, "previous_close", "previous_close_price", "prev_close",
                       "adjusted_previous_close", default=0) or 0)
    change_pct = _pick(raw, "change_percent", "change_pct", "percent_change", "last_percent_change")
    if change_pct is None and price and prev:
        change_pct = ((price - prev) / prev) * 100
    change_pct = float(change_pct or 0)

    volume = float(_pick(raw, "volume", "current_volume", "share_volume", default=0) or 0)
    avg_volume = float(_pick(raw, "average_volume", "avg_volume", "average_volume_2_weeks",
                             "average_volume_30_day", default=0) or 0)

    return {
        "ticker": symbol,
        "price": price,
        "volume": volume,
        "change_pct": change_pct,
        "avg_volume": avg_volume,
        "previous_close": prev,
        "raw": raw,
    }


# ── Public market / order API (used by engine) ────────────────────────────────

def get_equity_quotes(symbols: list[str]) -> list[dict]:
    """Fetch live quotes for a list of tickers. Returns normalized quote dicts."""
    if not symbols:
        return []
    if not _token:
        return [
            {
                "ticker": s.upper(),
                "price": 0.0,
                "volume": 0,
                "change_pct": 0.0,
                "avg_volume": 1,
                "_stub": True,
            }
            for s in symbols
        ]

    result = _mcp_call("get_equity_quotes", {"symbols": [s.upper() for s in symbols]})

    raw_list: list = []
    if isinstance(result, list):
        raw_list = result
    elif isinstance(result, dict):
        raw_list = (
            result.get("quotes")
            or result.get("results")
            or result.get("data")
            or []
        )
        # Map keyed by symbol: {"AAPL": {...}, ...}
        if not raw_list and result and all(isinstance(v, dict) for v in result.values()):
            raw_list = [{"symbol": k, **v} for k, v in result.items()]

    by_symbol: dict[str, dict] = {}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        q = _normalize_quote(item)
        if q["ticker"]:
            by_symbol[q["ticker"]] = q

    out = []
    for s in symbols:
        sym = s.upper()
        if sym in by_symbol:
            out.append(by_symbol[sym])
        else:
            out.append({
                "ticker": sym,
                "price": 0.0,
                "volume": 0,
                "change_pct": 0.0,
                "avg_volume": 0,
                "_missing": True,
            })
    return out


def review_equity_order(
    symbol: str,
    side: str,
    order_type: str,
    *,
    quantity: float | None = None,
    amount: float | None = None,
    limit_price: float | None = None,
    time_in_force: str = "gfd",
) -> dict:
    args: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side.lower(),
        "order_type": order_type.lower(),
        "time_in_force": time_in_force,
    }
    if quantity is not None:
        args["quantity"] = quantity
    if amount is not None:
        args["amount"] = amount
    if limit_price is not None:
        args["limit_price"] = limit_price
    return _mcp_call("review_equity_order", args)


def place_equity_order(
    symbol: str,
    side: str,
    order_type: str,
    *,
    quantity: float | None = None,
    amount: float | None = None,
    limit_price: float | None = None,
    time_in_force: str = "gfd",
    skip_review: bool = False,
) -> dict:
    """
    Place an equity order in the Agentic account.
    Always reviews first unless skip_review=True.
    Respects dry-run mode (logs only, no real order).
    """
    args: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side.lower(),
        "order_type": order_type.lower(),
        "time_in_force": time_in_force,
    }
    if quantity is not None:
        args["quantity"] = quantity
    if amount is not None:
        args["amount"] = amount
    if limit_price is not None:
        args["limit_price"] = limit_price

    if _dry_run:
        logger.info("[DRY-RUN] Would place order: %s", args)
        return {"ok": True, "dry_run": True, "order": args}

    if not skip_review:
        review = review_equity_order(
            symbol, side, order_type,
            quantity=quantity, amount=amount,
            limit_price=limit_price, time_in_force=time_in_force,
        )
        # Surface hard blocks if present
        if isinstance(review, dict):
            blocked = review.get("blocked") or review.get("can_place") is False
            if blocked:
                raise RuntimeError(f"Order review blocked: {review}")

    return _mcp_call("place_equity_order", args)


def place_market_buy(symbol: str, amount_usd: float) -> dict:
    """Dollar-notional market buy in the Agentic account."""
    return place_equity_order(
        symbol, "buy", "market",
        amount=round(amount_usd, 2),
    )


def place_limit_sell(symbol: str, shares: float, limit_price: float) -> dict:
    """Limit sell only — never a market sell."""
    return place_equity_order(
        symbol, "sell", "limit",
        quantity=round(shares, 6),
        limit_price=round(limit_price, 2),
        time_in_force="gtc",
    )


# ── Data fetchers (portfolio sync) ────────────────────────────────────────────

def _fetch_accounts() -> list:
    result = _mcp_call("get_accounts")
    return result.get("accounts", result if isinstance(result, list) else [])


def _fetch_positions() -> list:
    # Prefer current tool name; fall back to older alias if needed
    try:
        result = _mcp_call("get_equity_positions")
    except Exception:
        result = _mcp_call("get_positions")

    raw = result.get("positions", result if isinstance(result, list) else [])
    enriched = []
    for p in raw:
        try:
            qty = float(_pick(p, "quantity", "shares", default=0) or 0)
            avg_cost = float(_pick(p, "average_buy_price", "average_cost", "avg_cost", default=0) or 0)
            current = float(_pick(p, "current_price", "last_price", "mark_price", default=avg_cost) or avg_cost)
            cost = qty * avg_cost
            mkt_val = qty * current
            pnl = mkt_val - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            enriched.append({
                **p,
                "symbol": _pick(p, "symbol", "ticker", default="?"),
                "quantity": qty,
                "average_buy_price": avg_cost,
                "current_price": current,
                "cost_basis": round(cost, 2),
                "market_value": round(mkt_val, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
            })
        except Exception:
            enriched.append(p)
    return enriched


def _fetch_orders() -> list:
    try:
        result = _mcp_call("get_equity_orders")
    except Exception:
        result = _mcp_call("get_orders")
    orders = result.get("orders", result if isinstance(result, list) else [])
    try:
        orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    except Exception:
        pass
    return orders


def _fetch_balances() -> dict:
    try:
        result = _mcp_call("get_portfolio")
    except Exception:
        result = _mcp_call("get_account_balances")
    return result if isinstance(result, dict) else {}


# ── Stats from real fills ─────────────────────────────────────────────────────

def _compute_stats(orders: list, positions: list) -> dict:
    open_states = {"queued", "unconfirmed", "confirmed", "partially_filled"}
    filled_states = {"filled", "partially_filled"}

    wins = losses = 0
    total_realized = 0.0
    trade_pairs = []

    by_ticker: dict[str, list] = {}
    for o in sorted(orders, key=lambda x: x.get("created_at", "")):
        ticker = o.get("symbol") or o.get("ticker") or "?"
        by_ticker.setdefault(ticker, []).append(o)

    for ticker, ticker_orders in by_ticker.items():
        buy_queue = []
        for o in ticker_orders:
            if o.get("state") not in filled_states:
                continue
            side = (o.get("side") or "").lower()
            if side == "buy":
                buy_queue.append(o)
            elif side == "sell" and buy_queue:
                buy_o = buy_queue.pop(0)
                try:
                    buy_px = float(buy_o.get("average_price") or buy_o.get("price") or 0)
                    sell_px = float(o.get("average_price") or o.get("price") or 0)
                    qty = float(o.get("quantity") or buy_o.get("quantity") or 0)
                    pnl = (sell_px - buy_px) * qty
                    total_realized += pnl
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1
                    trade_pairs.append({
                        "ticker": ticker,
                        "buy_price": round(buy_px, 2),
                        "sell_price": round(sell_px, 2),
                        "qty": round(qty, 4),
                        "pnl": round(pnl, 2),
                        "result": "win" if pnl >= 0 else "loss",
                        "closed_at": o.get("updated_at") or o.get("created_at") or "",
                    })
                except Exception:
                    pass

    total_closed = wins + losses
    win_rate = round(wins / total_closed * 100, 1) if total_closed > 0 else 0
    total_unreal = sum(float(p.get("unrealized_pnl", 0)) for p in positions)
    open_orders = [o for o in orders if o.get("state") in open_states]
    limit_sells = [
        o for o in open_orders
        if (o.get("side") or "").lower() == "sell"
        and (o.get("type") or o.get("order_type") or "") in ("limit", "stop_limit")
    ]

    return {
        "wins": wins,
        "losses": losses,
        "total_closed_trades": total_closed,
        "win_rate": win_rate,
        "total_realized_pnl": round(total_realized, 2),
        "total_unrealized_pnl": round(total_unreal, 2),
        "total_pnl": round(total_realized + total_unreal, 2),
        "open_orders_count": len(open_orders),
        "pending_limit_sells_count": len(limit_sells),
        "closed_trade_pairs": trade_pairs[-50:],
        "dry_run": _dry_run,
    }


# ── Background sync ───────────────────────────────────────────────────────────

def _sync_once():
    global _cache
    with _lock:
        try:
            logger.info("Robinhood: syncing...")
            accounts = _fetch_accounts()
            positions = _fetch_positions()
            orders = _fetch_orders()
            balances = _fetch_balances()
            stats = _compute_stats(orders, positions)

            _cache.update({
                "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sync_status": "ok",
                "sync_error": None,
                "accounts": accounts,
                "positions": positions,
                "orders": orders[:200],
                "balances": balances,
                "stats": stats,
            })
            _save_cache()
            logger.info(
                "Robinhood: sync OK — %d positions, %d orders",
                len(positions), len(orders),
            )
        except PermissionError as e:
            _cache["sync_status"] = "auth_error"
            _cache["sync_error"] = str(e)
            _save_cache()
            logger.error("Robinhood auth error: %s", e)
        except Exception as e:
            _cache["sync_status"] = "error"
            _cache["sync_error"] = str(e)
            _save_cache()
            logger.warning("Robinhood sync failed: %s", e)


def _sync_loop():
    while not _stop_sync.is_set():
        _sync_once()
        _stop_sync.wait(SYNC_INTERVAL)


def start_sync(token: str, mcp_url: str | None = None, dry_run: bool = True):
    global _sync_thread, _token, _mcp_url, _dry_run
    _token = token or ""
    _mcp_url = (mcp_url or DEFAULT_MCP_URL).rstrip("/")
    _dry_run = bool(dry_run)
    if not _token:
        _cache["sync_status"] = "no_token"
        return
    _load_cache()
    _stop_sync.clear()
    if _sync_thread and _sync_thread.is_alive():
        return
    _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name="rh-sync")
    _sync_thread.start()
    logger.info("Robinhood: background sync started (dry_run=%s)", _dry_run)


def stop_sync():
    _stop_sync.set()


def force_refresh():
    threading.Thread(target=_sync_once, daemon=True).start()


def set_dry_run(enabled: bool):
    global _dry_run
    _dry_run = bool(enabled)


def configure(token: str, mcp_url: str | None = None, dry_run: bool | None = None):
    """Update credentials without restarting sync (used when bot starts)."""
    global _token, _mcp_url, _dry_run
    if token is not None:
        _token = token
    if mcp_url:
        _mcp_url = mcp_url.rstrip("/")
    if dry_run is not None:
        _dry_run = bool(dry_run)
