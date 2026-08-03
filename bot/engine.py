"""
TradeBot engine — runs as a background daemon thread.

Flow each scan:
  1. Pull live quotes from Robinhood MCP for watchlist tickers
  2. Filter tickers that meet entry criteria (volume + price movement)
  3. Ask Claude to analyze the opportunity
  4. Compute target sell price based on configured sell strategy
  5. Email the user an HTML alert with Claude's reasoning
  6. Poll Gmail for YES/NO reply (10-minute expiry)
  7. On YES: place buy order + immediate limit sell via Robinhood MCP

The bot NEVER places a market sell. Only limit sells at the target price.
"""

import json
import time
import random
import smtplib
import imaplib
import email as email_lib
import threading
import logging
from datetime import datetime, time as dtime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import requests

from bot.stats import (
    record_alert_sent,
    record_alert_declined,
    record_alert_expired,
    record_trade_executed,
)
from bot import robinhood as rh

ET = ZoneInfo("America/New_York")

logger = logging.getLogger("engine")

DEMO_DEFAULTS = {
    "demo_mode": True,
    "dry_run": True,
    "watchlist": "AAPL, NVDA, TSLA, AMD, SPY, QQQ",
    "position_size": 100,
    "scan_interval_seconds": 8,
    "min_change_pct": 1.5,
    "min_volume_multiplier": 1.2,
    "sell_mode": "dollar",
    "profit_target": 2.0,
    "strategy": "momentum breakout on high volume with a clear catalyst",
}

# Seed prices for demo quote simulation
_DEMO_BASE = {
    "AAPL": 210.0, "NVDA": 120.0, "TSLA": 250.0, "AMD": 160.0,
    "SPY": 560.0, "QQQ": 490.0, "MSFT": 430.0, "AMZN": 185.0,
}

# ── Shared state — read by Flask routes every 5 seconds ──────────────────────
bot_state: dict = {
    "running":              False,
    "status":               "Stopped",
    "log":                  [],
    "trades":               [],         # alerts sent this session
    "open_positions":       [],
    "last_scan":            None,
    "pending_approval":     {},         # ticker -> details, expires in 10 min
    "started_at":           None,
    "scans_this_session":   0,
    "demo_mode":            False,
}

_stop_event  = threading.Event()
_bot_thread: threading.Thread | None = None
_config: dict = {}


# ── Internal logging ──────────────────────────────────────────────────────────

def _log(level: str, msg: str):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": msg}
    bot_state["log"].append(entry)
    if len(bot_state["log"]) > 500:
        bot_state["log"] = bot_state["log"][-500:]
    getattr(logger, level.lower(), logger.info)(msg)


# ── Market helpers ────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(ET)


def _market_is_open() -> bool:
    now = _now_et()
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(15, 45)


def _market_status_str() -> str:
    now = _now_et()
    if now.weekday() >= 5:
        return "closed (weekend)"
    if now.time() < dtime(9, 30):
        open_at = now.replace(hour=9, minute=30, second=0, microsecond=0)
        mins = int((open_at - now).total_seconds() / 60)
        return f"opens in {mins}m"
    if now.time() > dtime(15, 45):
        return "closed for today"
    return "open"


# ── Sell strategy ─────────────────────────────────────────────────────────────

def _compute_target(quote: dict, claude: dict) -> float:
    """
    Compute limit sell target based on configured sell_mode:
      dollar   — fixed dollar gain (e.g. +$2.00)
      percent  — fixed percentage gain (e.g. +2%)
      claude   — use Claude's suggested target_price from analysis
    """
    mode  = _config.get("sell_mode", "dollar")
    price = float(quote["price"])

    if mode == "claude":
        suggested = claude.get("target_price")
        try:
            suggested = float(suggested)
            if suggested > price:
                return suggested
        except (TypeError, ValueError):
            pass
        return price + max(2.0, price * 0.02)   # fallback

    if mode == "percent":
        pct = float(_config.get("sell_percent", 2.0))
        return price * (1 + pct / 100)

    # default: dollar
    amt = float(_config.get("profit_target", 2.0))
    return price + amt


# ── Demo simulation helpers ───────────────────────────────────────────────────

def _demo_quotes(watchlist: list) -> list:
    """Generate fake quotes; ~40% of tickers spike enough to trigger filters."""
    results = []
    for ticker in watchlist:
        base = _DEMO_BASE.get(ticker, 100.0)
        # Most quiet; some spike hard so the demo always has action
        if random.random() < 0.4:
            change_pct = random.choice([-1, 1]) * random.uniform(1.6, 4.5)
            vol_mult = random.uniform(1.3, 3.0)
        else:
            change_pct = random.uniform(-0.8, 0.8)
            vol_mult = random.uniform(0.6, 1.1)
        price = round(base * (1 + change_pct / 100), 2)
        avg_volume = 10_000_000
        results.append({
            "ticker": ticker,
            "price": price,
            "volume": int(avg_volume * vol_mult),
            "change_pct": round(change_pct, 2),
            "avg_volume": avg_volume,
            "_demo": True,
        })
    return results


def _demo_claude(ticker: str, quote: dict) -> dict:
    """Simulate Claude: buy on strong up-moves, pass on down/weak moves."""
    chg = quote["change_pct"]
    if chg >= 2.5:
        buy, conf = True, "HIGH"
        reason = f"{ticker} is up {chg:+.1f}% on elevated volume — clean momentum continuation setup."
    elif chg >= 1.5:
        buy, conf = True, "MEDIUM"
        reason = f"{ticker} cleared the {quote['change_pct']:+.1f}% trigger with volume confirmation."
    else:
        buy, conf = False, "LOW"
        reason = f"{ticker} move looks weak or against the strategy — passing."
    target = round(quote["price"] * 1.015, 2) if buy else quote["price"]
    return {
        "buy": buy,
        "reason": reason,
        "confidence": conf,
        "target_price": target,
        "target_reasoning": "Demo target ~1.5% above entry.",
    }


def _queue_alert(ticker: str, quote: dict, claude: dict, target_price: float) -> bool:
    """Record an alert in UI state without sending email (used by demo mode)."""
    position = float(_config.get("position_size", 100))
    est_profit = target_price - float(quote["price"])
    _log("INFO", f"✉️  [DEMO] Alert queued: {ticker} @ ${quote['price']:.2f} → ${target_price:.2f}")
    bot_state["trades"].append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M:%S"),
        "ticker": ticker,
        "price": quote["price"],
        "target": target_price,
        "profit_est": round(est_profit, 2),
        "confidence": claude.get("confidence", "?"),
        "claude": "BUY" if claude.get("buy") else "NO",
        "status": "⏳ Awaiting reply",
        "reason": claude.get("reason", ""),
    })
    bot_state["pending_approval"][ticker] = {
        "sent_at": time.time(),
        "quote": quote,
        "target_price": target_price,
        "position_size": position,
        "claude": claude,
    }
    record_alert_sent(ticker, quote["price"], True)
    return True


def approve_pending(ticker: str) -> dict:
    """UI/demo YES — execute buy + limit sell (respects dry-run)."""
    ticker = ticker.upper()
    details = bot_state["pending_approval"].get(ticker)
    if not details:
        return {"ok": False, "error": f"No pending alert for {ticker}"}

    _log("INFO", f"✅ YES for {ticker} — executing")
    if _place_buy_order(ticker, details["position_size"]):
        shares = details["position_size"] / details["quote"]["price"]
        _place_limit_sell(ticker, shares, details["target_price"])
        record_trade_executed(
            ticker,
            details["quote"]["price"],
            details["target_price"],
            details["position_size"],
        )
        bot_state["open_positions"].append({
            "ticker": ticker,
            "buy_price": details["quote"]["price"],
            "target": details["target_price"],
            "size": details["position_size"],
            "shares": round(shares, 4),
            "opened": datetime.now().strftime("%H:%M"),
        })
    for t in bot_state["trades"]:
        if t["ticker"] == ticker and "Awaiting" in t["status"]:
            t["status"] = "✅ Bought — limit sell active"
    bot_state["pending_approval"].pop(ticker, None)
    return {"ok": True, "ticker": ticker}


def decline_pending(ticker: str) -> dict:
    """UI/demo NO — skip the trade."""
    ticker = ticker.upper()
    if ticker not in bot_state["pending_approval"]:
        return {"ok": False, "error": f"No pending alert for {ticker}"}
    _log("INFO", f"❌ Declined: {ticker}")
    record_alert_declined(ticker)
    for t in bot_state["trades"]:
        if t["ticker"] == ticker and "Awaiting" in t["status"]:
            t["status"] = "❌ Declined by you"
    bot_state["pending_approval"].pop(ticker, None)
    return {"ok": True, "ticker": ticker}


# ── Robinhood MCP order helpers ───────────────────────────────────────────────

def _place_buy_order(ticker: str, amount_usd: float) -> bool:
    """Place a fractional dollar-amount market buy via Robinhood MCP."""
    dry = _config.get("dry_run", True)
    tag = "[DRY-RUN] " if dry else ""
    _log("INFO", f"{tag}[MCP] BUY ${amount_usd:.2f} of {ticker}")
    try:
        result = rh.place_market_buy(ticker, amount_usd)
        _log("INFO", f"{tag}Buy result: {result}")
        return True
    except Exception as e:
        _log("ERROR", f"Buy order failed for {ticker}: {e}")
        return False


def _place_limit_sell(ticker: str, shares: float, limit_price: float) -> bool:
    """Place a limit sell order via Robinhood MCP. NEVER a market sell."""
    dry = _config.get("dry_run", True)
    tag = "[DRY-RUN] " if dry else ""
    _log("INFO", f"{tag}[MCP] LIMIT SELL {shares:.4f} {ticker} @ ${limit_price:.2f}")
    try:
        result = rh.place_limit_sell(ticker, shares, limit_price)
        _log("INFO", f"{tag}Limit sell result: {result}")
        return True
    except Exception as e:
        _log("ERROR", f"Limit sell failed for {ticker}: {e}")
        return False


def _get_quotes(watchlist: list) -> list:
    """Fetch live quotes from Robinhood MCP, or demo quotes in demo mode."""
    if _config.get("demo_mode"):
        return _demo_quotes(watchlist)
    try:
        return rh.get_equity_quotes(watchlist)
    except Exception as e:
        _log("ERROR", f"Quote fetch failed: {e}")
        return [
            {
                "ticker": t,
                "price": 0.0,
                "volume": 0,
                "change_pct": 0.0,
                "avg_volume": 0,
                "_stub": True,
            }
            for t in watchlist
        ]


# ── Claude analysis ───────────────────────────────────────────────────────────

def _ask_claude(ticker: str, quote: dict, news: str = "") -> dict:
    """
    Ask Claude whether to buy this stock.
    Returns dict with: buy, reason, confidence, target_price, target_reasoning
    """
    if _config.get("demo_mode"):
        return _demo_claude(ticker, quote)

    api_key = _config.get("claude_api_key", "")
    if not api_key:
        return {"buy": False, "reason": "Claude API key not configured.", "confidence": "N/A"}

    sell_mode = _config.get("sell_mode", "dollar")
    position  = _config.get("position_size", 100)
    strategy  = _config.get("strategy", "momentum breakout on high volume with a clear catalyst")

    if sell_mode == "claude":
        target_instruction = (
            "You must also recommend a limit sell price. Based on the stock's momentum, "
            "volatility, and today's trading range, suggest a realistic target that balances "
            "profit potential with a good probability of filling before market close. "
            "Include 'target_price' (number) and 'target_reasoning' (short string) in your JSON."
        )
    elif sell_mode == "percent":
        pct = _config.get("sell_percent", 2.0)
        tp  = float(quote["price"]) * (1 + float(pct) / 100)
        target_instruction = f"Sell target is pre-set at ${tp:.2f} ({pct}% gain). Include as target_price."
    else:
        amt = _config.get("profit_target", 2.0)
        tp  = float(quote["price"]) + float(amt)
        target_instruction = f"Sell target is pre-set at ${tp:.2f} (${amt} gain). Include as target_price."

    prompt = f"""You are a day trading assistant. Analyze this stock and decide whether to BUY.

STOCK:   {ticker}
Price:   ${quote['price']:.2f}
Change:  {quote['change_pct']:+.2f}% today
Volume:  {quote['volume'] / max(quote['avg_volume'], 1):.1f}x vs 30-day average
News:    {news or 'None available'}

STRATEGY: {strategy}
POSITION SIZE: ${position}

TARGET INSTRUCTION: {target_instruction}

Think: Is this a high-probability same-day trade? Is the entry clean?

Respond ONLY with valid JSON — no markdown, no text outside the JSON object:
{{
  "buy": true or false,
  "reason": "1-2 sentence explanation",
  "confidence": "HIGH or MEDIUM or LOW",
  "target_price": <number>,
  "target_reasoning": "brief note (only needed if sell_mode is claude)"
}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 400,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        text = r.json()["content"][0]["text"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        record_alert_sent(ticker, quote["price"], bool(result.get("buy")))
        return result
    except Exception as e:
        _log("ERROR", f"Claude API error: {e}")
        return {"buy": False, "reason": f"Claude error: {e}", "confidence": "N/A", "target_price": 0}


# ── Email alert ───────────────────────────────────────────────────────────────

def _send_alert(ticker: str, quote: dict, claude: dict, target_price: float) -> bool:
    gmail_user = _config.get("gmail_user")
    gmail_pass = _config.get("gmail_password")
    alert_to   = _config.get("alert_email")
    position   = float(_config.get("position_size", 100))
    sell_mode  = _config.get("sell_mode", "dollar")

    if not all([gmail_user, gmail_pass, alert_to]):
        _log("ERROR", "Email not configured — cannot send alert")
        return False

    est_profit = target_price - float(quote["price"])
    est_pct    = (est_profit / float(quote["price"])) * 100

    conf_color = {"HIGH": "#00c896", "MEDIUM": "#f5c842", "LOW": "#ff4d4d"}.get(
        claude.get("confidence", "LOW"), "#888"
    )
    sell_label = {
        "claude":  "Claude's recommendation",
        "percent": f"Fixed {_config.get('sell_percent', 2)}% gain",
        "dollar":  f"Fixed ${_config.get('profit_target', 2)} gain",
    }.get(sell_mode, "Fixed")

    subject = (
        f"🤖 TRADE ALERT: BUY {ticker}? "
        f"({claude.get('confidence','?')} confidence) — Reply YES or NO"
    )

    target_note = ""
    if claude.get("target_reasoning"):
        target_note = f"""
<div style="color:#888;font-size:11px;margin-top:8px;padding-top:8px;border-top:1px solid #2a2a2a;">
  Target reasoning: {claude['target_reasoning']}
</div>"""

    body = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
background:#0d0d0d;color:#e0e0e0;padding:24px;margin:0;">
<div style="max-width:520px;margin:auto;background:#161616;border:1px solid #2a2a2a;
border-radius:10px;padding:28px;">

<div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;">
  <div>
    <div style="font-size:20px;font-weight:700;color:#fff;">📈 {ticker}</div>
    <div style="font-size:12px;color:#888;margin-top:2px;">
      {datetime.now().strftime('%A, %B %d at %H:%M:%S')}
    </div>
  </div>
  <span style="background:{conf_color}22;color:{conf_color};border:1px solid {conf_color}44;
  padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;white-space:nowrap;">
    {claude.get('confidence','?')} CONFIDENCE
  </span>
</div>

<table style="width:100%;border-collapse:collapse;font-size:13px;">
  {''.join(f"""<tr style="border-bottom:1px solid #2a2a2a;">
    <td style="padding:8px 0;color:#888;">{label}</td>
    <td style="color:{color};text-align:right;font-weight:{weight};">{value}</td>
  </tr>""" for label, value, color, weight in [
    ("Current Price",   f"${quote['price']:.2f}",         "#fff",    "600"),
    ("Today's Change",  f"{quote['change_pct']:+.2f}%",   "#00c896" if quote['change_pct'] >= 0 else "#ff4d4d", "600"),
    ("Volume vs Avg",   f"{quote['volume']/max(quote['avg_volume'],1):.1f}x", "#fff", "600"),
    ("Position Size",   f"${position:.0f}",                "#fff",    "400"),
    ("Sell Strategy",   sell_label,                        "#888",    "400"),
    ("Limit Sell At",   f"${target_price:.2f}",            "#00c896", "700"),
    ("Est. Profit",     f"+${est_profit:.2f} ({est_pct:.1f}%)", "#00c896", "700"),
  ])}
</table>

<div style="background:#0d1f16;border-left:3px solid #00c896;border-radius:4px;
padding:14px;margin:18px 0;">
  <div style="font-size:10px;color:#00c896;text-transform:uppercase;
  letter-spacing:1px;margin-bottom:6px;">Claude's Analysis</div>
  <div style="color:#fff;font-weight:600;margin-bottom:6px;">
    {'✅ RECOMMENDS BUY' if claude.get('buy') else '❌ RECOMMENDS PASS'}
  </div>
  <div style="color:#ccc;line-height:1.5;">{claude.get('reason','No reason provided.')}</div>
  {target_note}
</div>

<div style="background:#1a1a1a;border:1px solid #333;border-radius:6px;
padding:14px;text-align:center;">
  <div style="color:#888;font-size:12px;margin-bottom:8px;">Reply to this email:</div>
  <div style="font-size:16px;font-weight:700;">
    <span style="color:#00c896;">YES</span>
    <span style="color:#444;margin:0 16px;">·</span>
    <span style="color:#ff4d4d;">NO</span>
  </div>
  <div style="color:#555;font-size:11px;margin-top:8px;">
    Auto-expires in 10 minutes. Bot places buy + immediate limit sell only —
    never sells at a loss automatically.
  </div>
</div>

</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = gmail_user
        msg["To"]      = alert_to
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)

        _log("INFO", f"✉️  Alert sent: {ticker} @ ${quote['price']:.2f} → target ${target_price:.2f}")
        bot_state["trades"].append({
            "date":       datetime.now().strftime("%Y-%m-%d"),
            "time":       datetime.now().strftime("%H:%M:%S"),
            "ticker":     ticker,
            "price":      quote["price"],
            "target":     target_price,
            "profit_est": round(est_profit, 2),
            "confidence": claude.get("confidence", "?"),
            "claude":     "BUY" if claude.get("buy") else "NO",
            "status":     "⏳ Awaiting reply",
        })
        bot_state["pending_approval"][ticker] = {
            "sent_at":      time.time(),
            "quote":        quote,
            "target_price": target_price,
            "position_size":position,
        }
        return True
    except Exception as e:
        _log("ERROR", f"Email failed: {e}")
        return False


# ── Gmail reply poller ────────────────────────────────────────────────────────

def _check_replies():
    gmail_user = _config.get("gmail_user")
    gmail_pass = _config.get("gmail_password")
    if not gmail_user or not gmail_pass:
        return

    to_remove = []
    for ticker, details in list(bot_state["pending_approval"].items()):
        # Expire after 10 minutes
        if time.time() - details["sent_at"] > 600:
            _log("INFO", f"⏰ {ticker} alert expired (no reply)")
            record_alert_expired(ticker)
            for t in bot_state["trades"]:
                if t["ticker"] == ticker and "Awaiting" in t["status"]:
                    t["status"] = "⏰ Expired"
            to_remove.append(ticker)
            continue

        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(gmail_user, gmail_pass)
            mail.select("inbox")
            _, nums = mail.search(None, '(SUBJECT "TRADE ALERT" UNSEEN)')

            for num in nums[0].split():
                _, raw = mail.fetch(num, "(RFC822)")
                msg = email_lib.message_from_bytes(raw[0][1])
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="ignore").strip().upper()
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="ignore").strip().upper()

                if ticker.upper() not in (msg.get("Subject", "") + body).upper():
                    continue

                if body.startswith("YES"):
                    _log("INFO", f"✅ YES for {ticker} — executing")
                    if _place_buy_order(ticker, details["position_size"]):
                        shares = details["position_size"] / details["quote"]["price"]
                        _place_limit_sell(ticker, shares, details["target_price"])
                        record_trade_executed(
                            ticker,
                            details["quote"]["price"],
                            details["target_price"],
                            details["position_size"],
                        )
                        bot_state["open_positions"].append({
                            "ticker":    ticker,
                            "buy_price": details["quote"]["price"],
                            "target":    details["target_price"],
                            "size":      details["position_size"],
                            "shares":    round(shares, 4),
                            "opened":    datetime.now().strftime("%H:%M"),
                        })
                    for t in bot_state["trades"]:
                        if t["ticker"] == ticker and "Awaiting" in t["status"]:
                            t["status"] = "✅ Bought — limit sell active"
                    mail.store(num, "+FLAGS", "\\Seen")
                    to_remove.append(ticker)

                elif body.startswith("NO"):
                    _log("INFO", f"❌ Declined: {ticker}")
                    record_alert_declined(ticker)
                    for t in bot_state["trades"]:
                        if t["ticker"] == ticker and "Awaiting" in t["status"]:
                            t["status"] = "❌ Declined by you"
                    mail.store(num, "+FLAGS", "\\Seen")
                    to_remove.append(ticker)

            mail.logout()
        except Exception as e:
            _log("WARNING", f"Email reply check error: {e}")

    for ticker in to_remove:
        bot_state["pending_approval"].pop(ticker, None)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _bot_loop():
    demo = bool(_config.get("demo_mode"))
    bot_state["demo_mode"] = demo
    _log("INFO", "🚀 TradeBot started" + (" [DEMO MODE]" if demo else ""))
    dry = _config.get("dry_run", True)
    if demo:
        _log("INFO", "🧪 Demo uses fake quotes + simulated Claude — no APIs required")
        _log("INFO", "👉 Approve or decline pending alerts on the Dashboard")
    elif dry:
        _log("WARNING", "⚠️  DRY-RUN mode ON — orders will be logged, not placed")
    else:
        _log("WARNING", "🔴 LIVE mode — real orders will be placed after YES replies")

    bot_state["status"]             = "Running"
    bot_state["started_at"]         = datetime.now().isoformat()
    bot_state["scans_this_session"] = 0

    scan_interval  = int(_config.get("scan_interval_seconds", 8 if demo else 60))
    min_change     = float(_config.get("min_change_pct", 1.5))
    min_vol_mult   = float(_config.get("min_volume_multiplier", 1.2))
    watchlist      = [t.strip().upper() for t in _config.get("watchlist", "").split(",") if t.strip()]

    if not watchlist:
        _log("WARNING", "⚠️  Watchlist is empty — add tickers in Configure")

    while not _stop_event.is_set():
        if not demo and not _market_is_open():
            bot_state["status"] = f"Market {_market_status_str()} — standing by"
            _stop_event.wait(300)
            continue

        bot_state["scans_this_session"] += 1
        n = bot_state["scans_this_session"]
        _log("INFO", f"🔍 Scan #{n} — {len(watchlist)} tickers" + (" (demo)" if demo else ""))
        bot_state["status"]    = f"{'[DEMO] ' if demo else ''}Scanning {len(watchlist)} stocks..."
        bot_state["last_scan"] = _now_et().strftime("%H:%M:%S ET")

        quotes    = _get_quotes(watchlist)
        triggered = 0

        for q in quotes:
            if _stop_event.is_set():
                break
            ticker = q["ticker"]
            if ticker in bot_state["pending_approval"]:
                continue
            if q.get("_stub") or q.get("_missing"):
                continue
            if not q.get("price"):
                continue

            avg_vol = q.get("avg_volume") or 0
            if avg_vol > 0:
                vol_ratio = q["volume"] / avg_vol
                vol_ok = vol_ratio >= min_vol_mult
            else:
                vol_ratio = 0.0
                vol_ok = True

            if abs(q["change_pct"]) >= min_change and vol_ok:
                triggered += 1
                vol_note = f"{vol_ratio:.1f}x vol" if avg_vol > 0 else "vol n/a"
                _log("INFO", f"🎯 {ticker} triggered ({q['change_pct']:+.1f}%, {vol_note})")
                claude = _ask_claude(ticker, q)
                if claude.get("buy"):
                    target = _compute_target(q, claude)
                    _log("INFO", f"🤖 {'[DEMO] ' if demo else ''}Claude: BUY {ticker} → target ${target:.2f}")
                    if demo:
                        _queue_alert(ticker, q, claude, target)
                    else:
                        _send_alert(ticker, q, claude, target)
                else:
                    if demo:
                        record_alert_sent(ticker, q["price"], False)
                    _log("INFO", f"🤖 {'[DEMO] ' if demo else ''}Claude: pass on {ticker} — {claude.get('reason','')[:80]}")

        if triggered == 0 and not any(q.get("_stub") or q.get("_missing") for q in quotes):
            _log("INFO", "📊 No triggers this scan — watchlist quiet")
        elif all(q.get("_stub") for q in quotes):
            _log("WARNING", "⚠️  Robinhood MCP not connected — no live quote data")
        elif all(q.get("_missing") or q.get("_stub") for q in quotes):
            _log("WARNING", "⚠️  No quotes returned for watchlist — check MCP token / symbols")

        if not demo:
            _check_replies()
        else:
            # Expire demo pending after 10 minutes same as live
            now = time.time()
            for ticker, details in list(bot_state["pending_approval"].items()):
                if now - details["sent_at"] > 600:
                    _log("INFO", f"⏰ {ticker} alert expired (no reply)")
                    record_alert_expired(ticker)
                    for t in bot_state["trades"]:
                        if t["ticker"] == ticker and "Awaiting" in t["status"]:
                            t["status"] = "⏰ Expired"
                    bot_state["pending_approval"].pop(ticker, None)

        bot_state["status"] = (
            f"{'[DEMO] ' if demo else ''}Monitoring — last scan {bot_state['last_scan']} "
            f"({bot_state['scans_this_session']} scans this session)"
        )
        _stop_event.wait(scan_interval)

    bot_state["running"] = False
    bot_state["status"]  = "Stopped"
    bot_state["demo_mode"] = False
    _log("INFO", "🛑 TradeBot stopped")


# ── Public API ────────────────────────────────────────────────────────────────

def start_bot(config: dict) -> bool:
    global _bot_thread, _config
    if bot_state["running"]:
        return False
    _config = config
    rh.configure(
        token=config.get("robinhood_mcp_token", ""),
        mcp_url=config.get("robinhood_mcp_url") or None,
        dry_run=config.get("dry_run", True),
    )
    _stop_event.clear()
    bot_state["running"]          = True
    bot_state["demo_mode"]        = bool(config.get("demo_mode"))
    bot_state["trades"]           = []
    bot_state["open_positions"]   = []
    bot_state["pending_approval"] = {}
    bot_state["log"]              = []
    _bot_thread = threading.Thread(target=_bot_loop, daemon=True)
    _bot_thread.start()
    return True


def start_demo() -> bool:
    """Start bot with simulated market data — no credentials needed."""
    cfg = dict(DEMO_DEFAULTS)
    return start_bot(cfg)


def stop_bot() -> bool:
    _stop_event.set()
    bot_state["running"] = False
    bot_state["status"]  = "Stopping..."
    return True
