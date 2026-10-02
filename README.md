# TradeBot

An AI-assisted day trading bot that runs locally on your Windows, Mac, or Linux machine.
It watches a stock watchlist in real time, uses Claude AI to analyze opportunities,
emails you for approval, then places trades automatically via Robinhood's Agentic Trading MCP.

> **New: paper trading from GitHub.** Try the strategy with pretend money
> against real prices. It runs on GitHub Actions, saves results to Google Drive
> (or a repo branch), and comes with a GitHub Pages dashboard for setup, results
> and accuracy checks. No machine or Robinhood account needed. See
> [docs/PAPER_TRADING.md](docs/PAPER_TRADING.md).

---

## How It Works — High Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR MACHINE                             │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │  TradeBot    │───▶│  Claude API  │───▶│  Gmail (SMTP)    │  │
│  │  (Python)    │    │  (Analysis)  │    │  Trade Alert     │  │
│  └──────┬───────┘    └──────────────┘    └──────────────────┘  │
│         │                                         │             │
│         │ live quotes                    you reply YES/NO       │
│         ▼                                         │             │
│  ┌──────────────┐                                 ▼             │
│  │  Robinhood   │◀────────────────────────────────┘            │
│  │  MCP Server  │  buy order + limit sell                       │
│  └──────────────┘                                               │
└─────────────────────────────────────────────────────────────────┘
```

### The Full Daily Cycle

**Every 60 seconds during market hours (9:30 AM – 3:45 PM ET):**

```
Step 1 — SCAN
  Pull live quotes for all watchlist tickers from Robinhood MCP.
  Filter for tickers that have:
    • Moved ≥1.5% today (up or down)
    • Volume ≥1.2x their 30-day average
  These two conditions together signal that something unusual is happening.

Step 2 — ANALYZE
  For each triggered ticker, ask Claude:
    • Is this a high-probability same-day trade?
    • Does it fit the configured strategy?
    • What's the risk/reward?
    • (If sell mode = "Let Claude Decide") What's a good limit sell price?
  Claude returns: buy/no, confidence level, reasoning, and target price.

Step 3 — EMAIL ALERT
  If Claude says YES, send you an HTML email with:
    • Current price, % change, volume vs average
    • Claude's full reasoning and confidence level
    • Proposed buy amount ($100 default)
    • Limit sell target and estimated profit
    • Reply YES or NO — expires in 10 minutes

Step 4 — EXECUTE
  You reply YES  →  Bot places $100 market buy via Robinhood MCP
                    Bot immediately places limit sell at target price
                    You get a Robinhood push notification
  You reply NO   →  Trade skipped, bot keeps scanning
  No reply in 10m→  Alert expires, bot keeps scanning

Step 5 — PORTFOLIO SYNC (separate background thread)
  Every 60 seconds, pull from Robinhood MCP:
    • Account balances and buying power
    • All open positions with live P&L
    • Full order history → compute real win rate from actual fills
    • Open orders (your pending limit sells)
  Cached to robinhood_cache.json for instant Portfolio page loads.
```

### Key Design Decisions

**Never sells at a loss automatically.**
The bot only places limit sells. If a trade goes against you, the position
just sits there until you manually close it in the Robinhood app.
This prevents the bot from panic-selling on a dip.

**You approve every trade.**
No trade happens without your YES reply. The email gives you Claude's full
reasoning so you can make an informed decision in under 30 seconds.

**Only deploys a fraction of your capital.**
Your $2k account is a cushion. The bot only deploys $100 per trade (configurable).
You can have multiple positions open at once, but never risk the full account.

**Win rate from real Robinhood data.**
The Portfolio page computes win rate by pairing actual filled buy orders with
filled sell orders from Robinhood's order history — not from TradeBot's own logs.
This means the numbers are accurate even if a trade was placed or closed manually.

---

## Pages

### Dashboard
Your main view. Shows bot status, start/stop controls, configuration health,
pending email approvals, today's trade alerts, and a live scrolling log.

### Portfolio
Live data pulled from Robinhood every 60 seconds:
- **Stats bar** — buying power, portfolio value, unrealized P&L, realized P&L, win rate, closed trades
- **Open Orders tab** — all pending orders (your active limit sells show here)
- **Positions tab** — current holdings with live unrealized P&L
- **Order History tab** — filterable full order log with avg fill prices
- **Closed Trades tab** — every completed buy/sell pair with P&L and win/loss result

### Configure
Five sections, each with a ✓ / ⚠ status badge and ⓘ help tooltip with links:
1. Claude API key
2. Gmail sender + App Password + destination email
3. Robinhood MCP endpoint + auth token
4. Sell Strategy (fixed $, fixed %, or let Claude decide)
5. Trading Rules (watchlist, position size, scan interval, strategy description)

---

## What You Need

| Requirement | Notes |
|---|---|
| Python 3.10+ | python.org — check "Add to PATH" on Windows |
| Robinhood account | Agentic Trading enabled — separate $2k account |
| Anthropic API key | console.anthropic.com — ~$1–2/day usage |
| Gmail account | Dedicated bot Gmail recommended + App Password |
| Windows/Mac/Linux | Runs anywhere Python runs |

---

## Quick Start

### Windows
1. Install Python 3.10+ from https://python.org (check "Add Python to PATH")
2. Unzip the TradeBot folder anywhere
3. Double-click `START_TRADEBOT.bat`
4. Your browser opens automatically to http://localhost:5000

### Mac / Linux
1. Install Python 3.10+
2. In a terminal: `cd tradebot && chmod +x start_tradebot.sh && ./start_tradebot.sh`
3. Open http://localhost:5000

---

## First Run

1. **Create master password** — encrypts all your API keys at rest (AES-256)
2. **Configure Claude API** → https://console.anthropic.com/settings/keys
3. **Configure Gmail** → create App Password at https://myaccount.google.com/apppasswords
4. **Configure Robinhood** → enable Agentic Trading in the Robinhood app
5. **Set sell strategy** → fixed $2, fixed %, or let Claude choose
6. **Set trading rules** → add your watchlist tickers, position size, strategy description
7. **Click Start Bot** on the Dashboard

After first-time setup, just double-click START_TRADEBOT.bat each morning.
Your config stays encrypted on disk — you only enter your master password to unlock.

---

## Running Cost

| Item | Cost |
|---|---|
| Robinhood trading | Free (commission-free) |
| Robinhood MCP | Free |
| Claude API | ~$0.50–$2.00/day |
| Gmail SMTP/IMAP | Free |
| Python + Flask | Free |
| **Total** | **~$1–2/day** |

---

## Security

- All credentials encrypted with AES-256 (Fernet) using your master password
- Key derived with PBKDF2HMAC, 480,000 iterations
- Config stored in `~/.tradebot/config.enc` — unreadable without your password
- Web interface only accessible at 127.0.0.1 — not exposed to your network
- Session clears on "Lock" click

---

## Files Created at Runtime

| File | Contents |
|---|---|
| `~/.tradebot/config.enc` | Encrypted credentials |
| `~/.tradebot/salt.bin` | Key derivation salt |
| `~/.tradebot/meta.json` | Non-sensitive config flags (for dashboard display) |
| `stats.json` | Trade history and P&L log (no secrets) |
| `robinhood_cache.json` | Cached Robinhood account data (no secrets) |

---

## Connecting Robinhood MCP

TradeBot talks to Robinhood's official Agentic Trading MCP at
`https://agent.robinhood.com/mcp/trading` using these tools:

| Action | MCP tool |
|---|---|
| Live quotes | `get_equity_quotes` |
| Portfolio sync | `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders` |
| Preview order | `review_equity_order` |
| Place order | `place_equity_order` |

**Dry-run is on by default.** Orders are logged but not placed until you turn
dry-run off in Configure → Safety.

Paste your MCP auth token on the Configure page after enabling Agentic Trading
in the Robinhood app. See:
https://robinhood.com/us/en/support/articles/agentic-trading-overview/

---

## Disclaimer

This software is for educational and experimental purposes.
You are solely responsible for all trades placed through your account.
This is not financial advice. Trading involves risk of loss.
Past performance of any strategy does not guarantee future results.
