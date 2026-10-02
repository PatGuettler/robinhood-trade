"""
TradeBot web interface — Flask app.
Serves the dashboard, portfolio view, and configuration page.
All routes require session unlock (master password) except /setup and /unlock.
"""

import os
import sys
import json
import secrets
import threading
import webbrowser
from datetime import datetime

from flask import Flask, abort, render_template, request, jsonify, session, redirect, url_for

sys.path.insert(0, os.path.dirname(__file__))

import config_store
from bot.engine    import (
    start_bot, stop_bot, start_demo, bot_state,
    approve_pending, decline_pending, DEMO_DEFAULTS,
)
from bot.stats     import get_all as get_stats, get_today_stats, get_daily_pnl_series
from bot.robinhood import (
    get_cache    as rh_cache,
    start_sync   as rh_start,
    stop_sync    as rh_stop,
    force_refresh as rh_refresh,
)

app = Flask(__name__)
app.secret_key = os.urandom(32)   # ephemeral session key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",   # other websites can't make requests with your session
)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.before_request
def _local_only():
    # Blocks DNS-rebinding attacks: a malicious site pointing its own domain at 127.0.0.1.
    if request.host.rsplit(":", 1)[0] not in ALLOWED_HOSTS:
        abort(403)


# ── Auth helpers ──────────────────────────────────────────────────────────────
# The decrypted config and master password live only in this process's memory.
# The browser cookie holds nothing but a random session id (Flask's default
# cookie session is signed, not encrypted, so secrets must never go in it).

_SESSIONS: dict[str, dict] = {}


def _sess() -> dict | None:
    sid = session.get("sid")
    return _SESSIONS.get(sid) if sid else None


def _login(password: str | None, config: dict, demo: bool = False):
    old = session.get("sid")
    if old:
        _SESSIONS.pop(old, None)
    session.clear()
    sid = secrets.token_urlsafe(32)
    _SESSIONS[sid] = {"password": password, "config": config, "demo": demo}
    session["sid"] = sid


def _unlocked() -> bool:
    return _sess() is not None


def _can_change_config() -> bool:
    """Demo sessions started from the lock screen never know the master password."""
    s = _sess()
    return bool(s and s["password"] and not s["demo"])


def _cfg() -> dict:
    s = _sess()
    return s["config"] if s else {}


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not _unlocked():
        return redirect(url_for("unlock" if config_store.is_configured() else "setup"))
    return render_template("dashboard.html", meta=config_store.get_meta(), bot=bot_state)


@app.route("/portfolio")
def portfolio():
    if not _unlocked():
        return redirect(url_for("unlock"))
    return render_template("portfolio.html")


@app.route("/configure")
def configure():
    if not _unlocked():
        return redirect(url_for("unlock"))
    return render_template("configure.html", meta=config_store.get_meta(), cfg=_cfg())


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if config_store.is_configured():
        return redirect(url_for("unlock"))
    if request.method == "POST":
        pw  = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 8:
            return render_template("setup.html", error="Password must be at least 8 characters.")
        if pw != pw2:
            return render_template("setup.html", error="Passwords do not match.")
        config_store.save_config({}, pw)
        _login(pw, {})
        return redirect(url_for("configure"))
    return render_template("setup.html")


@app.route("/unlock", methods=["GET", "POST"])
def unlock():
    if request.method == "POST":
        pw  = request.form.get("password", "")
        cfg = config_store.load_config(pw)
        if cfg is None:
            return render_template("unlock.html", error="Incorrect password.")
        _login(pw, cfg)
        # Start Robinhood background sync if token is present
        token = cfg.get("robinhood_mcp_token", "")
        if token:
            rh_start(
                token,
                mcp_url=cfg.get("robinhood_mcp_url") or None,
                dry_run=cfg.get("dry_run", True),
            )
        return redirect(url_for("index"))
    return render_template("unlock.html")


@app.route("/logout")
def logout():
    rh_stop()
    _SESSIONS.pop(session.get("sid"), None)
    session.clear()
    return redirect(url_for("unlock"))


# ── Config API ────────────────────────────────────────────────────────────────

@app.route("/api/save_section", methods=["POST"])
def save_section():
    if not _can_change_config():
        return jsonify({"ok": False, "error": "Unlock with your master password to change settings."}), 401
    data    = request.get_json()
    section = data.get("section", "")
    values  = data.get("values", {})
    cfg     = _cfg()
    cfg.update(values)
    if section == "robinhood":
        cfg["robinhood_configured"] = True
    config_store.save_config(cfg, _sess()["password"])
    # Restart RH sync if token was just saved
    if section == "robinhood" and cfg.get("robinhood_mcp_token"):
        rh_stop()
        rh_start(
            cfg["robinhood_mcp_token"],
            mcp_url=cfg.get("robinhood_mcp_url") or None,
            dry_run=cfg.get("dry_run", True),
        )
    if section == "safety":
        from bot.robinhood import set_dry_run
        set_dry_run(cfg.get("dry_run", True))
    return jsonify({"ok": True})


@app.route("/api/test_claude", methods=["POST"])
def test_claude():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    import requests as req
    key = request.get_json().get("api_key", "")
    try:
        r = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=8,
        )
        if r.status_code == 200:
            return jsonify({"ok": True,  "message": "API key is valid ✓"})
        return jsonify({"ok": False, "message": f"API returned {r.status_code}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/test_email", methods=["POST"])
def test_email():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    import smtplib
    from email.mime.text import MIMEText
    d = request.get_json()
    gmail_user = d.get("gmail_user", "")
    gmail_pass = d.get("gmail_password", "")
    alert_to   = d.get("alert_email", "")
    try:
        msg = MIMEText("TradeBot email is working! You'll receive trade alerts here.")
        msg["Subject"] = "✅ TradeBot — Email Test"
        msg["From"]    = gmail_user
        msg["To"]      = alert_to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_pass)
            s.send_message(msg)
        return jsonify({"ok": True, "message": f"Test email sent to {alert_to} ✓"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/reset", methods=["POST"])
def reset_config():
    if not _can_change_config():
        return jsonify({"ok": False, "error": "Unlock with your master password first."}), 401
    stop_bot()
    rh_stop()
    config_store.delete_config()
    _SESSIONS.pop(session.get("sid"), None)
    session.clear()
    return jsonify({"ok": True})


# ── Bot API ───────────────────────────────────────────────────────────────────

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    ok = start_bot(_cfg())
    return jsonify({"ok": ok, "status": bot_state["status"]})


@app.route("/api/bot/demo", methods=["POST"])
def bot_demo():
    """One-click local demo — no Claude/Gmail/Robinhood needed."""
    if not _unlocked():
        # Demo session lives in memory only: it never writes a config file and
        # never learns (or sets) the master password, so it can't change settings.
        _login(None, {**dict(DEMO_DEFAULTS), "demo_mode": True, "dry_run": True}, demo=True)

    if bot_state["running"]:
        stop_bot()
        import time as _t
        _t.sleep(0.4)

    ok = start_demo()
    return jsonify({"ok": ok, "status": bot_state["status"], "demo": True})


@app.route("/api/bot/approve", methods=["POST"])
def bot_approve():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    ticker = (request.get_json() or {}).get("ticker", "")
    return jsonify(approve_pending(ticker))


@app.route("/api/bot/decline", methods=["POST"])
def bot_decline():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    ticker = (request.get_json() or {}).get("ticker", "")
    return jsonify(decline_pending(ticker))


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    stop_bot()
    return jsonify({"ok": True, "status": bot_state["status"]})


@app.route("/api/bot/status")
def bot_status_route():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    today  = get_today_stats()
    at     = get_stats().get("all_time", {})
    pending_details = []
    for ticker, d in bot_state.get("pending_approval", {}).items():
        pending_details.append({
            "ticker": ticker,
            "price": d["quote"].get("price"),
            "target": d.get("target_price"),
            "confidence": (d.get("claude") or {}).get("confidence", "?"),
            "reason": (d.get("claude") or {}).get("reason", ""),
        })
    return jsonify({
        "running":    bot_state["running"],
        "demo_mode":  bot_state.get("demo_mode", False),
        "status":     bot_state["status"],
        "last_scan":  bot_state["last_scan"],
        "scans":      bot_state.get("scans_this_session", 0),
        "started_at": bot_state.get("started_at"),
        "log":        bot_state["log"][-60:],
        "trades":     bot_state["trades"][-20:],
        "pending":    list(bot_state["pending_approval"].keys()),
        "pending_details": pending_details,
        "open_positions": bot_state.get("open_positions", []),
        "today":      today,
        "all_time":   at,
        "pnl_series": get_daily_pnl_series(14),
    })


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    stats  = get_stats()
    at     = stats.get("all_time", {})
    total  = at.get("wins", 0) + at.get("losses", 0)
    wr     = round(at["wins"] / total * 100, 1) if total > 0 else 0
    return jsonify({
        "ok":           True,
        "today":        get_today_stats(),
        "all_time":     at,
        "win_rate":     wr,
        "pnl_series":   get_daily_pnl_series(14),
        "trade_history":stats.get("trade_history", [])[-20:],
    })


# ── Robinhood API ─────────────────────────────────────────────────────────────

@app.route("/api/robinhood/data")
def rh_data():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "data": rh_cache()})


@app.route("/api/robinhood/refresh", methods=["POST"])
def rh_refresh_route():
    if not _unlocked():
        return jsonify({"ok": False}), 401
    token = _cfg().get("robinhood_mcp_token", "")
    if not token:
        return jsonify({"ok": False, "error": "No Robinhood token configured"})
    rh_refresh()
    return jsonify({"ok": True, "message": "Sync triggered — refreshes in ~5 seconds"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 5000
    print(f"\n{'='*48}")
    print(f"  TradeBot starting on http://localhost:{port}")
    print(f"{'='*48}\n")

    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")

    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=port, debug=False)
