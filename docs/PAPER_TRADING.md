# Paper trading mode

Run the TradeBot strategy with **pretend money against real market prices**,
entirely from GitHub — no computer left running, no Robinhood account needed.

```
 GitHub Pages site                 GitHub Actions (every 15 min, market hours)
 ┌──────────────────────┐          ┌──────────────────────────────────────────┐
 │ Setup    ─ settings ─┼──commit─▶│ paper_config.json                        │
 │          ─ secrets  ─┼─encrypt─▶│ python -m paper live / backtest / verify │
 │          ─ Run now  ─┼dispatch─▶│   5-min bars (Yahoo / Alpaca)            │
 │ Dashboard ◀──────────┼── CSV ───┤   simulated fills + P&L                  │
 │ Verify    ◀──────────┼── CSV ───┤   ─▶ Google Drive folder  (or            │
 └──────────────────────┘          │      the paper-data branch)              │
                                   └──────────────────────────────────────────┘
```

## One-time setup (≈10 minutes)

1. **Merge this branch into `main`.** GitHub only runs scheduled workflows from
   the default branch.
2. **GitHub Pages:** the *Deploy dashboard site* workflow publishes `site/` to
   the `gh-pages` branch, served at `https://<you>.github.io/robinhood-trade/`.
   If the page doesn't appear, set repo → Settings → Pages → *Deploy from a
   branch* → `gh-pages` / root.
3. Open the site → **Setup**:
   1. Paste a fine-grained GitHub token (this repo only; Contents, Secrets and
      Actions: read & write). It stays in your browser.
   2. Set your **starting paper cash**, dollars per trade and strategy, and
      click **Save settings**.
   3. (Optional) **Google Drive**: follow the on-page steps to create a Google
      OAuth client, then click **Connect Google Drive**. The page creates a
      “TradeBot Paper Trading” folder, saves the Google credentials as GitHub
      secrets, and copies any existing results into Drive. Until you do this,
      results are saved on the repo's `paper-data` branch.
   4. Click **Backtest last day / 2 days / last week** to see how the bot would
      have done right away.

That's it. The live paper account then trades by itself every 15 minutes while
the market is open, and verification runs after every close.

## What you can see

**Dashboard**: account value, total return, realized and unrealized P&L, win
rate, a *How it would have done* table for the last day, last 2 days, last week
and since the start, an equity curve, daily P&L bars, open positions, every
trade with buy and sell prices and the reason it sold, a per-stock summary,
every signal the bot saw (and why it bought or passed), and the run log. Use
the dropdown to switch between the live account and any backtest.

**Verify**: see below.

## Files written (Drive folder or `paper-data` branch)

| File | Contents |
|---|---|
| `fills.csv` | Every simulated buy and sell, with the 5-minute bar it filled in (O/H/L/C), the trigger values, cash after, data source |
| `equity.csv` | Account value snapshot after every bar: cash, positions, realized/unrealized P&L |
| `signals.csv` | Every trigger and the decision (BUY / PASS / SKIP), by rules or Claude, with the reason |
| `positions.csv` | Currently open positions, marked to the latest price |
| `state.json` | The live account's cash and positions (what the next run continues from) |
| `summary.json` | Latest headline numbers |
| `runs.csv` | One row per bot run |
| `backtest_*.csv`, `backtests.csv` | Same shapes for every backtest, keyed by `run_id` / `backtest_id` |
| `verify_report.json`, `verify_fills.csv` | Output of the accuracy checks |

Every row carries `account_id`. Starting a fresh account (Setup → *Start a
fresh live paper account*) bumps the ID, so old rows are kept but hidden.

## How the simulation works

The live account and backtests run the exact same code (`paper/engine.py`),
one 5-minute bar at a time:

* **Trigger**: same as the live bot. The price must have moved at least
  *min change %* from the previous close, with volume at least *N×* the
  30-day average. By default the average is adjusted for time of day, because
  comparing 10 AM volume to a whole day's average would almost never trigger.
  Choose *Today so far vs full-day average* to match the original bot exactly.
* **Decision**: by default every trigger that fits the rules is bought (in
  paper mode nobody has to answer an email). Turn on *Claude review* to have
  Claude approve or pass each one, the way the live bot asks you.
* **Buy**: fills at the **next** bar's open plus slippage, so a signal never
  trades at a price it couldn't have seen.
* **Limit sell**: target = entry + $X, entry + X%, or Claude's pick. Fills at
  the target only when a later bar's high reaches it, or at the open if the
  price gaps above it.
* **Never sells at a loss** unless you set a stop-loss, *sell at end of day*,
  or *sell after N days*. If a single bar touches both the stop and the target,
  the stop is assumed to have hit first (the conservative assumption).
* **Cash**: fractional shares, at most *max open positions*, no buy without
  enough paper cash, at most one entry per ticker per day.

Known limits: 5-minute bars can't show the order of prices inside a bar, and a
limit order that is only touched might not fully fill in real life. Yahoo's
free data is unofficial, so add free Alpaca keys for a second source. Scheduled
Actions can start a few minutes late, but each run catches up on every bar it
missed, so no bars are skipped.

## How to verify it's accurate

The **Verify** page gives you three independent kinds of evidence:

1. **Automated checks** (`python -m paper verify`, run after each close):
   * the cash ledger re-adds from the starting cash to every recorded balance;
   * every trade's P&L recomputes from its buy and sell;
   * every fill obeys the fill rules for its bar (buy = open + slippage, target
     only if the high reached it, …);
   * every buy fills on a bar **after** its signal (no look-ahead);
   * account value = cash + positions on every snapshot;
   * every fill price lies within that day's low–high from a **second,
     independent vendor** (Stooq, or Alpaca if you added keys);
   * **replay**: the whole live period is re-run from scratch with freshly
     downloaded bars and must produce the same trades. Claude's recorded
     decisions are reused so the replay is deterministic.
2. **Re-checked in your browser**: the page recomputes the ledger, P&L and
   positions from the raw CSVs with separate JavaScript code, so a bug in the
   Python can't hide itself.
3. **Spot-check by hand**: every fill lists its bar's open/high/low/close and
   links to a chart. Open a 5-minute chart for that time and compare.

## Running locally

```bash
pip install -r requirements-paper.txt
python -m paper backtest --days 5            # real data → .paper-data/
python -m paper live
python -m paper verify
python -m paper backtest --synthetic --data-dir /tmp/demo   # offline demo data
python -m pytest tests                         # unit tests for the fill rules
```

To view local results, serve the repo root (`python -m http.server`) and open
`/site/index.html?data=/.paper-data/&config=/paper_config.json`.

## Secrets used by the workflow

| Secret | Needed for |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Google Drive storage (set by *Connect Google Drive*) |
| `ANTHROPIC_API_KEY` | Claude review (optional) |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Alpaca market data (optional) |

GitHub disables schedules on repositories with no activity for 60 days. If the
dashboard says the bot hasn't run, re-enable the workflow in the Actions tab.
