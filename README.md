# EZ-Money

Buys the stocks you choose a couple of minutes before the market closes and sells them right after the next open, on Robinhood. It comes with a web dashboard for picking tickers, pausing, and checking results, plus Discord/Telegram alerts.

> **Read this first**
> - **Robinhood has no official stock-trading API.** EZ-Money uses the unofficial [`robin_stocks`](https://github.com/jmfernandes/robin_stocks) library, which talks to Robinhood's private endpoints. It can stop working whenever Robinhood changes something, and using it may break Robinhood's terms of service. You use it at your own risk.
> - Overnight gaps go both ways. Earnings, news, and weekends can move a stock a lot between the close and the open. This is not investment advice.
> - It starts in **paper mode** (simulated trades). Run it that way for a while before you switch to live.

## How it works

| When (ET) | What happens |
| --- | --- |
| 7:00 AM | The planner reads the NYSE calendar and schedules the day. Nothing is scheduled on holidays, and on 1 PM early-close days the buy moves earlier. |
| 9:30:05 AM (open + 5 s) | Sells every lot bought in an earlier session. Each sell is retried up to 3 times. |
| Every 5 min | Reconcile: updates orders still working, and sells any lot that missed the open (for example, if the server was down). |
| 3:58 PM (close − 2 min) | Buys each enabled watchlist ticker for its dollar amount, as a fractional-share market order. |

Robinhood has no "market on close" or "market on open" order types, so EZ-Money sends market orders at those times instead. You can change both offsets in the dashboard.

Safety rules built into the trader:
- **It only sells what it bought.** Every buy is tracked as a lot, and the sell is for exactly that quantity, so other shares you own of the same stock are never touched.
- **Each buy runs at most once per session.** This is enforced in the database, so a restart, the CLI, and the daemon can't double-buy.
- **Hard caps** on dollars per ticker and dollars per night come from `.env`, and the dashboard can't raise them. If the night's total is over the cap, or buying power is short, the buy is skipped and you're notified.
- **No stacking.** It won't buy a ticker again while an earlier lot of it is still unsold.
- **Live mode is set only in `.env`.** The dashboard can't switch it on, and `--force` works only in paper mode.
- **A pause switch** skips buys. Sells still happen, so nothing gets stuck.

## Quick start (paper mode, on your computer)

```bash
git clone https://github.com/JamesRobinson188/EZ-Money.git && cd EZ-Money
```

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

Start the trader in one terminal:

```bash
ez-money run
```

Start the dashboard in another, then open http://127.0.0.1:8501:

```bash
streamlit run dashboard/app.py
```

Add tickers in the dashboard (or with `ez-money watch add SPY 500`) and let it run. Paper trades fill at Yahoo Finance prices and start with `PAPER_STARTING_CASH`. To try it outside market hours, run `ez-money buy-now --force` and then `ez-money sell-all`.

## Going live

1. **Turn on two-factor authentication with an authenticator app** in Robinhood (Account → Security). When Robinhood shows the QR code, also copy the **setup key**. Put it in `.env` as `RH_TOTP_SECRET`, and add it to your authenticator app too. This lets the daemon log in again by itself.
2. Fill in `RH_USERNAME` and `RH_PASSWORD`, and set `TRADING_MODE=live`.
3. Log in once by hand. Robinhood may ask you to approve the new device in its app:

   ```bash
   ez-money login
   ```

   This saves a session under `data/robinhood-session/` that the daemon reuses.
4. Start with small amounts and low caps (`MAX_DOLLARS_PER_TICKER`, `MAX_DOLLARS_PER_NIGHT`).

**Account type:** in a cash account, the money from a sale takes a business day to settle. Trading every night with the proceeds of that morning's sell can cause good-faith violations. A margin account (Robinhood Instant or Gold) avoids that, but check Robinhood's rules for your account. Buying at the close and selling the next day is not a day trade, so the pattern-day-trader rule doesn't apply.

## Running it on a server (Docker)

The trader has to run all the time, so a small cloud VM is the easiest home for it.

```bash
sudo mkdir -p /opt/ez-money && sudo chown $USER /opt/ez-money && git clone https://github.com/JamesRobinson188/EZ-Money.git /opt/ez-money && cd /opt/ez-money
```

```bash
cp .env.example .env && nano .env
```

```bash
mkdir -p data && sudo chown 1000:1000 data
```

```bash
docker compose build && docker compose run --rm trader ez-money login
```

```bash
docker compose up -d
```

To start it on boot, install the systemd unit:

```bash
sudo cp deploy/ez-money.service /etc/systemd/system/ && sudo systemctl enable --now ez-money
```

The dashboard listens only on `127.0.0.1:8501` on the server. Open it from your laptop through an SSH tunnel, then browse to http://127.0.0.1:8501:

```bash
ssh -L 8501:127.0.0.1:8501 you@your-server
```

Set `DASHBOARD_PASSWORD` anyway.

## Dashboard

- **Dashboard tab:**
  - The top bar shows paper/live mode and whether the daemon is alive (from its heartbeat).
  - **Pause trading** switch, and **Sell all now**, which queues a sell of every bot position. If the market is closed, those sells execute at the next open.
  - Tonight's cycle, open positions with unrealized P&L, and recent activity.
  - The watchlist editor: turn tickers on or off, set the dollars per night, add or remove tickers.
  - A 30-night P&L chart, and the schedule settings (buy offset, sell delay, skip buys before weekends and holidays).
  - A **Send test notification** button.
- **History tab:** realized P&L, average return, win rate and drawdown for the last 7, 30 or 90 nights (or all of them); P&L by night; results by ticker; recent nights; and CSV export.

The dashboard never talks to Robinhood. It writes to the shared SQLite database (`data/ezmoney.db`), and the daemon picks up changes and commands within a few seconds.

## Command line

```text
ez-money run                 start the trader daemon
ez-money login               log in to Robinhood and save the session
ez-money status              mode, next times, watchlist, open lots, recent activity
ez-money watch add SPY 500   add or update a ticker (dollars per night)
ez-money watch remove SPY
ez-money buy-now [--force]   run the close buy now (--force: paper only, ignores the clock)
ez-money sell-now            run the open sell now (lots from earlier sessions)
ez-money sell-all [--yes]    sell every open bot position now
```

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `RH_USERNAME`, `RH_PASSWORD`, `RH_TOTP_SECRET` | | Robinhood login (live only) |
| `MAX_DOLLARS_PER_TICKER` | `1000` | Hard cap per ticker per night |
| `MAX_DOLLARS_PER_NIGHT` | `5000` | Hard cap on the whole night's buy |
| `PAPER_STARTING_CASH` | `10000` | Paper account size |
| `QUOTE_SOURCE` | `yahoo` | Prices for paper fills and the dashboard. `demo` makes up prices so you can try it offline. |
| `DISCORD_WEBHOOK_URL` | | Discord channel webhook |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | | Telegram bot |
| `DASHBOARD_PASSWORD` | | Password for the dashboard |
| `DATA_DIR` | `data` | Database and Robinhood session |

## Development

```bash
python -m pytest
```

The code lives in `src/ezmoney/`:
- `strategy.py`: buy, sell and reconcile, with all the safety checks.
- `scheduler.py`: the daemon.
- `brokers/`: the Robinhood and paper brokers.
- `market_calendar.py`: the NYSE calendar.
- `db.py`: the SQLite store.
- `history.py`: dashboard math.
- `notify.py`: Discord and Telegram.

The dashboard is `dashboard/app.py`. The tests use a fake broker and a fake clock, so they cover holidays, early closes, partial fills, retries and missed sells without touching Robinhood.
