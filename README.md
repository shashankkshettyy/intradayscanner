# NSE Intraday Scanner — Ultimate (Research Edition)

Python 3.10+ · Windows / Linux / macOS · **paper trading and research only**

This is a thoroughly rewritten version of the supplied intraday scanner. It detects candidates **after a completed candle**, calculates realistic *illustrative* risk plans, ranks verified technical evidence on a consistent **0–100** point scale, exports results, and optionally sends Telegram alerts. It **does not** trade or claim to forecast profits.

## Installation (Windows PowerShell)

```powershell
cd intraday_scanner_ultimate
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
```

If PowerShell disallows activation, you may use `.\.venv\Scripts\python.exe` in place of `python`, without activating the environment.

## Run

```powershell
# Fastest initial dry run (no Telegram)
python scanner.py --universe nifty50 --mode watchlist

# Confirmed setups during live NSE trading hours (9:50–15:15 IST for auto)
python scanner.py --universe nifty50 --mode live

# Broader official NSE Nifty 500 universe, if the index CSV is accessible
python scanner.py --universe nifty500 --mode auto --threshold 72 --top 25

# Focus on long opportunities
python scanner.py --side long --min-rr 1.7

# Customize risk assumptions. Values are for sizing DEMONSTRATION only.
python scanner.py --capital 50000 --risk-pct 0.5 --slippage-bps 5 --fees-bps 3

# Optional headlines shown as context ONLY, never scored
python scanner.py --news
```

All scans export both `outputs/scan_*.csv` and `outputs/scan_*.json` with score breakdowns, freshness and metadata. If the market is closed, `auto` produces **WATCHLIST_ONLY**: it does **not** output buy-now trade levels.

## Telegram setup: rotate your old bot token first

The original uploaded script included a hard-coded Telegram token. Assume it has been exposed, revoke it through **@BotFather** and generate a fresh token. **Do not commit tokens or passwords to Git.** This project contains no usable bot credentials and does not auto-load `.env`.

```powershell
$env:TELEGRAM_BOT_TOKEN = 'YOUR_NEW_ROTATED_TOKEN'
$env:TELEGRAM_CHAT_ID = 'YOUR_CHAT_ID'
python scanner.py --universe nifty50 --mode live --telegram
```

For repeat scans every 15 minutes during the NSE session, keep this terminal open:

```powershell
python scanner.py --universe nifty500 --schedule --telegram --interval-minutes 15
```

Scheduler scan slots start at **09:50 IST** and stop after **15:05 IST** on weekdays. It will not know in advance whether a day is an exchange holiday; **missing fresh session/index data suppresses all live alerts**. Notifications are sent only for newly confirmed `(date, symbol, side, setup)` combinations; successful sends are deduplicated using the local SQLite alert database. Telegram is **off by default**.

## Input universes

The scanner tries NSE Indices' public index-constituent CSVs when using `--universe nifty50` or `nifty500`. Some networks block those files. In that case it warns loudly and uses your original **static fallback (50 / approximately 249 curated stocks)**, which is **not** necessarily the latest index membership. To force an accurate list, save an index constituents CSV with a `Symbol` column locally:

```powershell
python scanner.py --symbols-file latest_constituents.csv --mode live
```

For a smaller personalized list, make a `.txt` file with one NSE symbol per line and run `--symbols-file favorites.txt`. No NSE symbol is automatically assumed to be F&O eligible or short-saleable.

## Confirmed strategy logic

**Daily qualifier:** At least 210 **completed** daily bars, actual EMA 20/50/200, minimum ₹30 stock price, minimum 20-day median turnover ₹2 crore, sensible daily ATR. The **live scan uses only completed prior-session daily data**; it explicitly excludes the current day's incomplete daily candle.

**Live qualifying gates:** Fresh 5-minute candles, six correctly timestamped 5-minute opening candles (09:15–09:45), a live fresh NIFTY index regime, no excessive opening gap, aligned daily EMA trends, consistent session data without missing bars, at least 3 historical sessions for equivalent-time volume, sufficient volume, and an adequate 15-minute momentum history.

**Entry setups:** Fresh ORB break/breakdown, fresh VWAP reclaim/rejection, or new 5-minute fast-EMA continuation above/below the opening range. The latest completed candle must exceed the matching time-of-day historical volume benchmark. A price hovering near a resistance level is **not** a breakout.

**Scoring, 100 points:** EMA alignment 15; NIFTY index regime 10; relative strength 10; time-matched volume 15; VWAP alignment 10; opening-range quality 10; 15-minute RSI 10; entry candle 10; true open-vs-prior-close gap 5; historical turnover 5. Threshold default: 72. These are **heuristic, unvalidated weights**, not calibrated probabilities.

**Risk plan:** Stop is derived from recent 5-minute structure and ATR buffer. The target is a projected measured move, limited by nearby previous-session / 20-day levels when they obstruct the move. Reward/risk includes estimated two-sided slippage plus trading fees and is rejected below the configurable minimum. Entry is the latest *completed historical candle close*, not a guaranteed executable quote. **Targets and RR are scenarios, not promised price destinations**.

### Market restrictions / production readiness

* Yahoo Finance is a best-effort historical feed; it can lag, throttle, be unavailable, or omit symbols. It is **not** an exchange-certified streaming quote. Do **not** connect this project directly to live automated order execution.
* The scanner has **no verified option-chain PCR, true OI buildup, delivery percentage, circuit-band status, actual FII/DII firm-specific flow or GIFT Nifty series**. The previous code calculated or labelled some of these incorrectly. Their absence is intentional: **unknown is not bullish**.
* The optional headline search uses Google News RSS: treat title matches as **unverified context**. They do not change scores.
* Brokerage, STT, GST, exchange fees, price bands, corporate actions, tick sizes, margin, short-sale eligibility, individual stock news, and slippage can alter real fills. `fees-bps` is an estimate, not an exchange/broker tax calculation. `paper_qty` is for long-side capital-limited illustration only.
* No trade is automatically opened, monitored, closed or placed. A Telegram message is **not** a brokerage stop-loss order.
* Do not infer strategy effectiveness from the tests: these validate code mechanics using **synthetic data**. Obtain multi-month **survivorship-bias-aware**, broker-quality historical data; backtest walk-forward with next-bar fills, transaction fees, spread/slippage, and out-of-sample evaluation; then paper trade before even considering real risk.

## Test commands and key files

```powershell
python -m pytest -q
python -m py_compile scanner.py
python scanner.py --help
```

`scanner.py` — provider, data checks, scoring, risk, output, scheduler and Telegram.

`universe_fallback.py` — static symbols from the original file (no API tokens).

`tests/test_scanner.py` — synthetic tests for gap, no-lookahead, ORB, RVOL, risk model, score, data safety and reports.

`.env.example` — dummy placeholders; no secrets loaded automatically.

## Official references

* NSE cash market regular session: https://www.nseindia.com/static/market-data/market-timings
* NSE Indices constituents: https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500
* Yahoo Finance download API: https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html

## Backward-compatible worker / Procfile deployment

Your previous deployment used:

```procfile
worker: python indian_stock_scanner.py --schedule --universe nse
```

**The upgraded bundle now includes a `Procfile`** with the same filename and
`--universe nse`, plus `--telegram` for actual Telegram alerts:

```procfile
worker: python indian_stock_scanner.py --schedule --universe nse --telegram
```

`indian_stock_scanner.py` is a thin entry point for `scanner.py`. Keep both files,
`universe_fallback.py`, `requirements.txt`, and `Procfile` in your deployment root.
Install dependencies during build using `pip install -r requirements.txt`. Set
`TELEGRAM_BOT_TOKEN` (with a **new, rotated** token) and `TELEGRAM_CHAT_ID` in
your host's environment variables; never add secrets to the Procfile or Git.

`--universe nse` now loads the **full regular EQ-series NSE equity universe**
from an official exchange securities list, rather than substituting a manually
selected Nifty universe. This may be thousands of symbols; scanning them via
Yahoo Finance is slow and may hit data-provider rate limits. An official-list
cache in `outputs/` may be used for up to 14 days when the download is
blocked; if no valid list is available the scan fails explicitly instead of
claiming full NSE coverage. For a smaller, faster worker replace `nse` with
`nifty500`. Some cloud worker filesystems are ephemeral, including the report
and Telegram deduplication SQLite files; use persistent storage if you need
alerts to remain deduplicated across deployments or restarts.
