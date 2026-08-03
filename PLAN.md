# TradeBot — Architecture & Build Plan

## What This App Does

TradeBot is an AI-assisted day trading bot that runs locally on your Windows machine
(or Mac/Linux). It watches a stock watchlist in real time via the Robinhood Agentic
Trading MCP, asks Claude to analyze entry opportunities, emails you for approval,
then places a buy order followed immediately by a limit sell at your profit target.

**The bot never sells at a loss automatically.** It only places limit sell orders.
If a trade goes against you, it sits as an open position until you manually decide
what to do on Robinhood's app.

---

## Core Workflow

```
Every 60 seconds during market hours (9:30 AM – 3:45 PM ET):

  1. SCAN
     Pull live quotes for all watchlist tickers via Robinhood MCP.
     Filter for tickers that meet entry criteria:
       - Price moved ≥1.5% today (up or down)
       - Volume is ≥1.2x the 30-day average

  2. ANALYZE
     For each triggered ticker, send price + volume data to Claude API.
     Claude evaluates the setup against your configured strategy.
     Claude returns: buy/no, confidence, reason, and a suggested target price
     (if sell mode is set to "let Claude decide").

  3. EMAIL ALERT
     If Claude says YES, send you an HTML email containing:
       - Current price, volume, % change
       - Claude's recommendation + reasoning + confidence level
       - Proposed buy amount, limit sell target, estimated profit
       - Sell strategy mode (fixed $ / fixed % / Claude's pick)
     You reply YES or NO within 10 minutes.

  4. EXECUTE
     If you reply YES:
       - Place market buy order ($X position size) via Robinhood MCP
       - Immediately place limit sell at target price via Robinhood MCP
       - Record trade in stats.json and bot_state
     If NO or no reply within 10 min:
       - Skip trade, log it, keep scanning

  5. PORTFOLIO SYNC
     Separate background thread syncs with Robinhood MCP every 60s:
       - Account balances + buying power
       - All open positions with unrealized P&L
       - Full order history (used to compute real win rate from fills)
       - Open orders (pending limit sells etc.)
```

---

## File Structure

```
tradebot/
│
├── app.py                    Flask web server — all routes and API endpoints
├── config_store.py           Encrypted config read/write (AES-256 / Fernet)
├── requirements.txt          Python dependencies
├── START_TRADEBOT.bat        Windows double-click launcher
├── start_tradebot.sh         Mac/Linux launcher
├── stats.json                Persisted trade stats (auto-created at runtime)
├── robinhood_cache.json      Robinhood data cache (auto-created at runtime)
├── PLAN.md                   This file
├── README.md                 User-facing setup guide
│
├── bot/
│   ├── __init__.py
│   ├── engine.py             Main bot loop, Claude analysis, email alert/reply
│   ├── robinhood.py          Robinhood MCP client + background sync
│   └── stats.py              Trade stats persistence (stats.json)
│
└── templates/
    ├── base.html             Shared nav, styles, JS helpers
    ├── setup.html            First-run: create master password
    ├── unlock.html           Return visits: enter master password
    ├── dashboard.html        Main page: bot status, start/stop, live log, today's trades
    ├── portfolio.html        Robinhood data: positions, open orders, history, P&L, win rate
    └── configure.html        All configuration sections with status badges + ⓘ help
```

---

## Module Details

### `config_store.py`
- All sensitive data (API keys, passwords, tokens) encrypted at rest
- Uses Fernet symmetric encryption with PBKDF2HMAC key derivation (480,000 iterations)
- Salt stored separately in `~/.tradebot/salt.bin`
- Encrypted blob at `~/.tradebot/config.enc`
- Non-sensitive flags (has_claude, has_gmail, etc.) stored in `~/.tradebot/meta.json`
  for the dashboard to read without requiring the master password

### `bot/engine.py`
- Runs in a background daemon thread when bot is started
- `_market_is_open()` — checks weekday + time, skips weekends and after-hours
- `_ask_claude()` — sends stock data to Claude API, parses JSON response
- `_compute_target_price()` — applies sell strategy: fixed $, fixed %, or Claude's pick
- `_send_trade_alert()` — builds and sends HTML email via Gmail SMTP
- `_check_email_replies()` — polls Gmail IMAP for YES/NO replies, executes or skips
- `_place_buy_order()` — stub for Robinhood MCP buy call (replace with MCP SDK)
- `_place_limit_sell()` — stub for Robinhood MCP limit sell (NEVER market sell)
- `bot_state` dict — shared in-memory state read by the web UI every 5 seconds

### `bot/robinhood.py`
- Separate background thread, polls Robinhood MCP every 60 seconds
- `_mcp_call()` — JSON-RPC POST to Robinhood MCP endpoint with Bearer auth
- `_fetch_accounts()` / `_fetch_positions()` / `_fetch_orders()` / `_fetch_balances()`
- `_compute_stats()` — pairs filled buys with filled sells to calculate real win rate,
  realized P&L, unrealized P&L from Robinhood data (not from TradeBot's own logs)
- Results cached to `robinhood_cache.json` — portfolio page loads from cache instantly
- `force_refresh()` — triggers immediate sync from the UI refresh button

### `bot/stats.py`
- Persists trade activity to `stats.json` in the app directory
- Tracks: alerts sent, declined, expired; trades executed; wins/losses; P&L by day
- `get_daily_pnl_series(14)` — returns last 14 days for charting
- Survives bot restarts — all-time stats accumulate across sessions
- Note: win/loss from stats.json is based on TradeBot's records.
  The portfolio page shows win rate from actual Robinhood fills (more accurate).

### `app.py` — Routes
```
GET  /                        Dashboard (requires unlock)
GET  /portfolio               Portfolio / orders view
GET  /configure               Configuration page
GET  /setup                   First-time password creation
POST /setup                   Save master password
GET  /unlock                  Password entry
POST /unlock                  Decrypt config + start Robinhood sync
GET  /logout                  Clear session (re-locks config)

POST /api/save_section        Save one config section (encrypted)
POST /api/test_claude         Validate Claude API key
POST /api/test_email          Send test email
POST /api/bot/start           Start bot loop
POST /api/bot/stop            Stop bot loop
GET  /api/bot/status          Live bot state (log, trades, pending)
GET  /api/stats               All-time stats + daily P&L series
GET  /api/robinhood/data      Cached Robinhood account data
POST /api/robinhood/refresh   Trigger immediate Robinhood sync
POST /api/reset               Wipe all config + stop bot
```

---

## Configuration Sections (Configure Page)

### 1. Claude API
- API key from console.anthropic.com
- "Test" button validates the key live
- ⓘ links to Anthropic console + docs

### 2. Gmail — Trade Alerts
- Gmail address (sender account — recommend a dedicated bot Gmail)
- Gmail App Password (NOT your regular password — requires 2FA enabled)
- Alert destination email (where you receive and reply to alerts)
- "Send Test Email" button
- ⓘ links to Google App Passwords page + help article

### 3. Robinhood Agentic Trading
- MCP endpoint URL
- MCP auth token (from Robinhood Agentic account setup)
- ⓘ links to Robinhood Agentic Trading support page
- Warning shown: "Bot will ONLY place limit sells — never market sells"

### 4. Sell Strategy
- **Fixed $ amount** (default): sell when up $X (e.g. $2.00)
- **Fixed % gain**: sell when up X% (e.g. 2%)
- **Let Claude decide**: Claude analyzes momentum and suggests the target.
  Claude's reasoning for the target is shown in the email alert.
- Toggle between modes — only the relevant input shows

### 5. Trading Rules
- Position size ($ per trade, default $100)
- Scan interval (seconds between watchlist checks, default 60)
- Min volume multiplier (vs 30-day avg, default 1.2x)
- Watchlist (comma-separated tickers)
- Strategy description (plain English — Claude reads this when analyzing)

### 6. Danger Zone
- Reset all configuration (wipes ~/.tradebot/ and stops bot)

---

## Dashboard Page

- **Bot status badge** — Running (green) / Stopped (red)
- **Status text** — current activity ("Scanning 12 stocks...", "Market opens in 47m", etc.)
- **Start / Stop buttons** — always visible, disabled when not applicable
- **Config health panel** — each integration shown as ✓ Good to go or ⚠ Not set
- **Pending approvals** — tickers waiting for your YES/NO reply
- **Today's stats** — trades executed, alerts sent, session scans
- **Recent trade alerts** — this session's alerts with status
- **Live log** — scrollable log of bot activity, auto-refreshes every 5 seconds

---

## Portfolio Page

Pulls from `robinhood_cache.json` (updated every 60s from Robinhood MCP):

**Stats bar (top):**
- Buying power, Portfolio value, Unrealized P&L, Realized P&L, Win rate, Closed trades

**Tabs:**
1. **Open Orders** — all pending orders (queued, confirmed, partially filled)
   Shows: ticker, side, type, qty, limit price, filled qty, state, created time
2. **Positions** — current holdings
   Shows: ticker, shares, avg cost, current price, market value, cost basis, unrealized P&L, return %
3. **Order History** — all orders with filter by ticker / state / side
   Shows: date, ticker, side, type, qty, limit price, avg fill price, total, state
4. **Closed Trades (P&L)** — buy/sell pairs computed from filled orders
   Shows: close date, ticker, buy price, sell price, shares, P&L, win/loss badge

Last sync time shown in header. "↻ Refresh" button triggers immediate sync.

---

## Security Model

- Config encrypted with AES-256 (Fernet) using master password + PBKDF2 (480k iterations)
- Only accessible at 127.0.0.1 — never exposed on the local network
- Session cleared on "Lock" click or browser close
- No secrets stored in session beyond the decrypted config (held in Flask session)
- stats.json and robinhood_cache.json contain no secrets — only trade data

---

## What You Need

| Requirement | Notes |
|---|---|
| Python 3.10+ | From python.org — check "Add to PATH" on Windows |
| Robinhood account | With Agentic Trading enabled (separate $2k account) |
| Anthropic API key | From console.anthropic.com — ~$1-2/day usage |
| Gmail account | Dedicated bot Gmail recommended + App Password |
| Windows/Mac/Linux | Runs anywhere Python runs |

---

## Running Cost

| Item | Cost |
|---|---|
| Robinhood trading | Free (commission-free) |
| Robinhood MCP | Free |
| Claude API (analysis) | ~$0.50–$2.00/day |
| Gmail SMTP/IMAP | Free |
| Python + Flask | Free |
| **Total** | **~$1-2/day** |

---

## Limitations & Known Gaps

1. **MCP auth token** — Robinhood's consumer MCP clients use OAuth browser login.
   TradeBot expects a Bearer token pasted into Configure. Token acquisition depends
   on how Robinhood exposes tokens for custom agents; refresh if sync returns auth errors.

2. **Quote field variance** — `get_equity_quotes` response shapes can vary. The client
   normalizes common field names. If average volume is missing, the volume filter is
   skipped and only the price-change trigger applies.

3. **No news feed** — Claude analyzes price/volume only. A future enhancement would
   pipe in a news API (e.g. Benzinga, Polygon.io news) so Claude can factor in catalysts.

4. **No stop-loss automation** — by design. The bot never sells at a loss automatically.
   You manage losing positions manually via the Robinhood app.

5. **Email reply parsing** — reads the first word of your reply. Reply with just "YES"
   or "NO" as the first word. Subject line must contain the ticker symbol.

6. **Single-machine** — designed to run on one machine during market hours.
   Not a cloud service. If your machine sleeps, the bot pauses.

7. **Dry-run default** — live order placement is off until you disable dry-run in
   Configure → Safety.
