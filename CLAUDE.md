# Crypto Trend Bot — Project Log

This file is the running memory of this project. Any AI (or human) picking
this up should read this whole file first, and append a dated entry after
any meaningful change (new file, new strategy, config/architecture decision,
backtest result). Do not delete past entries — this is a history, not a
snapshot.

## Goal
A crypto trading system built in stages, for learning + eventual live use.
Not a single "do everything" AI — a set of small, specialized components
(see Architecture) that each do one job well.

## Architecture (5 layers, planned)
1. **Research/News Collector** — `data_collector.py`. DONE (v1). Runs
   continuously: logs a price snapshot every `PRICE_LOG_INTERVAL_SECONDS` to
   `data/price_history.csv`, and every `NEWS_LOG_INTERVAL_SECONDS` appends
   *every* new headline (not just keyword matches) from RSS feeds to
   `data/news_log.jsonl`. Unlike Layer 2, it does not judge anything — it's
   raw history for Layer 3 to read later.
2. **Event/Anomaly Detector** — `anomaly_detector.py`. DONE (v1). Hard-coded
   (non-AI) checks: price shock (% move in a time window), volume spike
   (vs rolling average), and news keyword matches from free RSS feeds. Emails
   on any trigger and writes `anomaly_flag.json` for a future decision layer
   to read before trading.
3. **Decision/Strategy Agent** — `decision_agent.py`. DONE (v1). Computes the
   MA-crossover signal, overrides to flat if Layer 2's `anomaly_flag.json` is
   recent, pulls Layer 1's recent headlines for context, and writes it all to
   `decision.json`. Still rule-based, not a "smart" model — the point of this
   layer is separating decide-from-execute, so the rule can be upgraded later
   without touching Layer 4 at all.
4. **Execution Agent** — `execution_engine.py`. DONE (v1; order path verified
   on Binance TESTNET only, never on the real exchange — see Progress log).
   No longer computes any signal itself:
   reads `decision.json` from Layer 3 (refuses to act if it's missing or
   older than `DECISION_MAX_AGE_MINUTES`), fetches a fresh price for order
   sizing, and always attaches an exchange-side stop-loss on entry. Defaults
   to `DRY_RUN=true` + `USE_TESTNET=true`; both must be explicitly disabled
   before it can touch real money.
5. **Treasury Notifier** — `notify_on_balance.py`. DONE. Sends an email when
   balance crosses a threshold. Does NOT auto-transfer funds by design.

## Progress log
- **2026-09-20**: Created the project. Built `backtest.py` — Moving Average
  Crossover (fast=10, slow=50) backtest on BTC/USDT, 1h candles, via ccxt
  (Binance public data, no API key needed). Result on ~951 candles: strategy
  +19.4% vs buy&hold +26.2%, max drawdown -8.4%. Strategy underperformed
  because the market was in one continuous uptrend with no crash — MA
  crossover's value is limiting downside during crashes, not beating a clean
  uptrend. Chart saved to `backtest_result.png`.
- **2026-09-20**: Built `notify_on_balance.py` (Layer 5). Polls exchange
  balance via ccxt, emails via SMTP when balance crosses a configurable
  threshold (`.env` controlled). Deliberately does **not** auto-withdraw —
  crypto transfers are irreversible, so that step stays a manual human
  action. Secrets live in `.env` (gitignored); `.env.example` documents the
  required variables.
- **2026-09-20**: Extracted `email_utils.py` (shared `send_email()`) out of
  `notify_on_balance.py` so every layer sends alerts the same way instead of
  duplicating SMTP code.
- **2026-09-20**: Built `anomaly_detector.py` (Layer 2). Three checks: price
  shock (default: alert if price moves >=3% in 5 minutes), volume spike
  (default: alert if 1m volume >= 3x the last 20-candle average), and a news
  keyword scanner over CoinDesk/CoinTelegraph RSS (free, no API key) for
  words like "sec", "regulation", "government", "bond", "hack", "ban", etc.
  On any trigger it emails and writes `anomaly_flag.json` (reason, details,
  timestamp) for a future decision/execution layer to check before trading.
  Smoke-tested live: price/volume checks ran cleanly against real Binance
  data, and the news scanner correctly caught and flagged a real "SEC"
  headline on the first run.
- **2026-09-20**: Built `data_collector.py` (Layer 1). Logs price snapshots
  to `data/price_history.csv` and every RSS headline (CoinDesk, CoinTelegraph,
  Decrypt) to `data/news_log.jsonl`, deduping by link via
  `data/seen_news_links.json`. `data/` is gitignored (it's generated, not
  source). Smoke-tested live: collected real price ticks and 92 real
  headlines on the first run.
- **2026-09-20**: Fixed a real bug found during Layer 1 testing — `ccxt`'s
  default `binance` client tries to load futures market data
  (`fapi.binance.com`) as part of `load_markets()`, which times out on this
  network even though the spot API works fine. Fixed in all three scripts
  (`backtest.py`, `anomaly_detector.py`, `data_collector.py`) by
  constructing the exchange with `{"options": {"fetchMarkets": ["spot"]}}`,
  since none of them need futures data. If a new script creates a ccxt
  exchange instance, apply the same option.
- **2026-09-20**: Built `execution_engine.py` (Layer 4). Reuses the
  MA-crossover signal (same FAST/SLOW windows as `backtest.py`) to decide
  enter/exit, refuses to act if `anomaly_flag.json` (Layer 2) was written
  within the last `ANOMALY_FLAG_MAX_AGE_MINUTES`, and on entry places an
  exchange-side `stop_loss_limit` sell order immediately (not relying on
  this script staying alive). Defaults: `DRY_RUN=true`, `USE_TESTNET=true` —
  both must be turned off deliberately for real orders. Logs every action to
  `data/trade_log.jsonl` and keeps position state in `trade_state.json`
  (gitignored). Smoke-tested in DRY_RUN+TESTNET mode only (no real API keys
  available): correctly fetched live candles, computed the MA signal, and
  took no action since no crossover had occurred. (At this point the live
  order path was still unverified; see the later testnet entry, where it
  passed.)
- **2026-09-20**: Added cross-layer alerting so an urgent issue on ANY layer
  reaches a human, not just the console of a background process nobody is
  watching. `email_utils.py` gained `alert_error(component, error)`, rate
  limited to one email per component per `ERROR_ALERT_COOLDOWN_MINUTES`
  (default 30) via `error_alert_state.json` (gitignored), so a repeating
  error doesn't spam the inbox once per loop. Wired into the exception
  handler of all four continuously-running scripts (`data_collector.py`,
  `anomaly_detector.py`, `notify_on_balance.py`, `execution_engine.py`).
  Also made `execution_engine.py`'s `log_trade()` email immediately
  (bypassing the cooldown) whenever a `LIVE` action fires — a real order
  is always urgent, unlike a `DRY_RUN` simulation. Also relaxed
  `email_utils.py` to read `SMTP_USER`/`SMTP_PASSWORD` via `.get()` instead
  of `os.environ[...]`, so any script importing it can still run (with
  emailing silently skipped and printed instead) even before SMTP is
  configured — previously importing it without a configured `.env` would
  crash immediately.
- **2026-09-20**: `email_utils.py`'s `NOTIFY_TO` now accepts a comma-separated
  list of addresses (parsed into a list, joined into the `To` header, passed
  as the recipient list to `sendmail`) instead of a single address — every
  alert from every layer now goes to all configured recipients. Current
  `.env.example` default: `humzakhan.karachi@gmail.com,iamanusfromcrypto@gmail.com`.
- **2026-09-20**: Added Brevo SMTP relay support (`SMTP_PORT` != 465 now uses
  `smtplib.SMTP` + `starttls()` instead of `SMTP_SSL`; added optional
  `FROM_EMAIL` since Brevo's verified sender can differ from the SMTP
  login). Ultimately not used — Brevo's signup/sender-verification flow was
  too much friction, so the project settled back on Gmail SMTP
  (port 465, App Password). The Brevo code path is still there and works if
  someone wants it later; `.env.example` documents both.
- **2026-09-20**: Verified email alerting actually works end-to-end: sent a
  real test email via Gmail SMTP (port 465, App Password) to both
  `NOTIFY_TO` addresses successfully, no errors. Real `.env` now exists
  locally (gitignored, not in this file) with working Gmail credentials.
- **2026-09-20**: First test emails landed in spam on both recipient
  accounts. Fixed two things in `email_utils.py`'s `send_email()`: added
  missing `Date` and `Message-ID` headers (their absence is a strong spam
  signal that `email.mime.text.MIMEText` doesn't set automatically), and
  gave the sender a display name (`"Crypto Trading Bot <...>"`) instead of a
  bare address. User also manually marked the first email "Not spam" on
  both accounts, which trains Gmail to inbox future mail from this sender.
  Second test email sent after the fix - confirmed working.
- **2026-09-20**: Built `decision_agent.py` (Layer 3) and refactored
  `execution_engine.py` to consume it instead of computing the MA signal and
  checking `anomaly_flag.json` itself. `decision_agent.py` writes
  `decision.json` (position, price, reason, anomaly status, up to 20 recent
  headlines from `data/news_log.jsonl` within `NEWS_LOOKBACK_HOURS`) on a
  `DECISION_CHECK_INTERVAL_SECONDS` schedule. `execution_engine.py` now has
  a `read_decision()` that returns `None` (causing it to skip the cycle) if
  `decision.json` is missing or older than `DECISION_MAX_AGE_MINUTES` — a
  safety net in case Layer 3 stops running. `execution_engine.py` still
  fetches a fresh ticker price at execution time for order sizing rather
  than trusting the decision's (possibly a few minutes old) cached price.
  Smoke-tested live end-to-end: `decision_agent.decide()` correctly computed
  the MA signal, found no anomaly, pulled real recent headlines (collected
  by the already-running `data_collector.py`) into the decision, wrote
  `decision.json`; `execution_engine.run_once()` then read it and correctly
  took no action (already flat, decision said stay out).
  `FAST_WINDOW`/`SLOW_WINDOW`/`TIMEFRAME`/`ANOMALY_FLAG_MAX_AGE_MINUTES` are
  now read by `decision_agent.py`, not `execution_engine.py`.

- **2026-09-20**: Connected Binance TESTNET and verified the live order path.
  Keys live in Windows *user* environment variables (not a `.env` file);
  `load_dotenv()` does not override existing env vars, so both work, but a
  process only sees new variables after it is fully restarted (a closed
  window is not enough). `verify_exchange.py` (read-only auth + balances,
  refuses unless `USE_TESTNET=true`) passed: 10,000 fake USDT. Then
  `verify_order_path.py` (also testnet-only, uses throwaway state/log files)
  ran `enter_position` -> confirmed the stop-loss order was open on the
  exchange with the right `stopPrice` -> `exit_position` -> confirmed no
  orders left behind. PASSED. ccxt reports the stop as `type=limit` plus
  `stopPrice` (that is how it represents Binance STOP_LOSS_LIMIT).
  Bugs found and fixed along the way: (1) Binance error -1021 (local clock
  1s ahead) — added `"adjustForTimeDifference": True` to the exchange
  options in `execution_engine.py`, `notify_on_balance.py`,
  `verify_exchange.py`; any new authenticated ccxt client needs it. (2)
  `notify_on_balance.py` was missing the `fetchMarkets: ["spot"]` fix from
  earlier; added. (3) `log_trade()` emailed after a LIVE order with no
  error handling, so a failed email (it was failing: bad SMTP password)
  raised before `enter_position` saved state, leaving an open position the
  bot didn't know about. Email failure is now caught and printed; confirmed
  the trade + state save complete even when email fails. (4) I earlier
  deleted the real `.env` while cleaning up after a test that had
  overwritten it — use a separate throwaway file for tests, never touch
  `.env`.
- **2026-10-03**: Hardened `execution_engine.py` after a risk review
  (full review: `reviews/risk-safety-review.md` in the project files).
  (1) Entry saves state right after the market buy, before placing the
  stop, so a failed stop can no longer leave an untracked position that
  gets bought again every cycle; the stop is retried 3x and, if it still
  fails, the position is market-sold back out with an urgent email.
  (2) Quantity is now the filled amount minus base-asset fees (Binance
  takes the 0.1% fee from the BTC received unless paying in BNB), capped
  at the real free balance, so the stop no longer tries to sell more than
  is held. (3) New `reconcile()` runs at the start of every LIVE cycle:
  stop filled -> logged as `LIVE stop-loss filled`, state reset to flat;
  stop cancelled/missing -> re-placed. Sells are always capped at what the
  bot bought and the free balance, so it can't sell coins held outside the
  bot. Exit won't sell while a stop it failed to cancel is still open.
  (4) `DRY_RUN`/`USE_TESTNET` now fail safe: only an explicit `false` turns
  a guard off (previously `1`/`yes`/typos turned live trading ON).
  Tested against a fake exchange covering stop failure, stop+sell failure,
  stop fill, external cancel, sell failure on exit, and fee rounding.
  `verify_order_path.py` re-run on Binance testnet the same day: PASSED.
- **2026-10-03**: Built `long_backtest.py`: all BTC/USDT 1h candles since
  2017-08 from data.binance.vision (cached in `data/binance_klines/`),
  closed-candle signals, next-open fills, 0.1% fees, stop modeled on candle
  lows. Results on real data (run on the user's PC): the live config
  (10/50, 1h, 2% stop, re-enter after stop) returned **+33% since 2017 vs
  buy & hold +1744%**, max drawdown -87%, 1555 trades, 550 stop-outs, ~$8.2k
  fees on a $1000 start. It lost money every year from 2021 on except 2023,
  and didn't protect much in crashes (2022: -62% vs -64% buy & hold).
  Re-entering right after a stop is the worst variant; waiting for a fresh
  crossover did +225%. Window check: best 2017-22 setting (10/100) made only
  +2.6% on 2023-now vs buy & hold +375%; no 1h setting beat buy & hold out of
  sample. Conclusion: the 1h MA strategy has no edge after fees; do NOT go
  live with it. Added a 4h/1d timeframe comparison to the script next.
- **2026-10-03**: Timeframe comparison (2017-22 in-sample, 2023-now out of
  sample, B&H +375%): best was **4h, 10/50, 2% stop, wait for the signal to
  go flat before re-buying after a stop-out**: +265% out of sample, max DD
  -31%, ~29 trades/yr. Neighbouring 4h settings (10/50, 20/50) all made
  +126% to +265%, so it isn't one lucky point, but 45 combos were tried, so
  expect live results to be worse. Switched the bot to it:
  `decision_agent.py` default `TIMEFRAME` is now `4h` and the MA uses
  closed candles only (the forming candle made the signal flip within the
  candle); `execution_engine.py` sets `wait_for_flat` in `trade_state.json`
  when a stop-loss fills and won't buy again until a decision says flat.
  `.env.example` updated to `TIMEFRAME=4h`. Unit-tested with fakes; needs a
  few weeks on testnet before real money.
- **2026-10-03**: Moved the testnet run to an Oracle Cloud free server
  (Ubuntu 24.04, Mumbai) as 4 systemd services; see `deploy/README.md`.
  Only one copy may run: the laptop scripts are stopped.
- **2026-10-03**: Fixed review item 5 (double-buy on a timeout). Each buy
  gets a client order id saved to `trade_state.json` as `pending_buy`
  before it is sent; after an error or a restart the bot looks the order up
  by that id and adopts the position (with a stop) if it filled. It sends
  no new buy until the exchange answers, and only clears the record once the
  order has been missing for 60s. Unit-tested with fakes, not yet on testnet.
- **2026-10-03**: Early stop-loss warning (user request): when the price is
  `STOP_WARNING_PERCENT` (default 1.5%) below the entry, `execution_engine.py`
  emails both NOTIFY_TO addresses once per position (flag
  `stop_warning_sent` in `trade_state.json`, reset on every entry/exit). It
  runs before the stale-decision check. Checked every 5 minutes, so a fast
  crash can hit the 2% stop before the email; the stop sells regardless.
- **2026-10-03**: Volume spikes no longer force the bot flat (user chose
  "email only"). `anomaly_detector.py` checks 1-minute volume against the
  last 20 minutes every minute; on the server that kept the 4h strategy out
  of the market for hours on ordinary bursts, and the backtest never had this
  rule. Now it only emails, at most once per `VOLUME_ALERT_COOLDOWN_MINUTES`
  (60). Price shock and news matches still force flat.
  `VOLUME_SPIKE_FORCES_FLAT=true` restores the old behaviour.

## Key decisions and why (read before changing these)
- **Strategy chosen: trend/momentum (MA crossover)** over mean-reversion or
  market-making — simplest to reason about, most tolerant of slow/simple
  infrastructure. Good first strategy, not a final one.
- **Layer 5 notifies only, never transfers funds** — user wants irreversible
  money movement to always be a manual decision, not automated.
- **Exchange API keys must be read-only / no-withdraw permission** wherever
  possible — a leaked key should never be able to move funds.
- **Crash protection = code, not AI reasoning.** An LLM/agent is too slow to
  react to a crash happening in seconds. Real protection comes from
  exchange-side stop-loss orders and small position sizing. Any execution
  layer (Layer 4) must implement this in code, not rely on an agent noticing
  in time.
- **Every layer must be able to email a human on an urgent issue or action**
  — user explicitly asked for this. "Urgent" = a real (LIVE) trade firing,
  or a layer erroring persistently enough that it may have silently stopped
  working. Routine DRY_RUN activity and one-off transient errors should not
  email (cooldown-limited) to avoid alert fatigue.
- **Decide and execute are separate processes on purpose.** Layer 3
  (`decision_agent.py`) decides what to do; Layer 4 (`execution_engine.py`)
  only carries it out. Do not let execution_engine.py compute trading
  signals again - if the strategy changes, change `decision_agent.py`. This
  is what makes it possible to upgrade the decision logic (e.g. to a real
  model) later without touching order-placement code.
- **`decision.json` staleness is a safety check, not a formality.**
  `execution_engine.py` must keep refusing to act when `decision.json` is
  missing/old - this is what stops it from trading blind if Layer 3 crashes.

## Not yet built
- `NOTIFY_TO` is not set in the Windows environment variables, so alert
  emails currently go only to `SMTP_USER` itself. It must be set to
  `humzakhan.karachi@gmail.com,iamanusfromcrypto@gmail.com` (comma-separated,
  no spaces) for alerts to reach the intended recipients. (`SMTP_PASSWORD`
  was fixed 2026-09-20: it had been a normal account password, which Gmail
  rejects; now the 16-char App Password and Gmail login succeeds.)
- Running the whole pipeline (data_collector, anomaly_detector,
  decision_agent, execution_engine) together as long-running processes on
  testnet for a few days before trusting it with real money. The order path
  itself is verified; sustained behaviour (stop-loss actually triggering,
  stale-decision handling, restarts mid-position) is not.
- Reconciliation: built and testnet-verified 2026-10-03 (see Progress log). Double-buy on timeout fixed and closed-candle signal done (see Progress
  log). Remaining review items not yet done: no max trade size / daily loss limit / kill switch, notifier
  shares the trading API key. See `reviews/risk-safety-review.md`.
- Layer 3's decision rule is still just MA-crossover + a hard override on
  anomalies; the recent headlines are collected and included in
  `decision.json` but not yet scored/reasoned over (e.g. by an LLM or
  sentiment model) — that's the natural next upgrade to this layer.

## Instructions for the next AI/session
- Read this entire file before making changes.
- After finishing meaningful work, add a new dated bullet under
  **Progress log** (what changed, why, results/numbers if applicable).
- If you make an architectural decision or change one listed above, update
  **Key decisions and why** instead of leaving the reasoning only in chat.
- Keep entries factual and short — this file is context, not a report.
