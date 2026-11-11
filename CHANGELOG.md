# Improvements over the supplied 2026-11-11 scanner

## Critical correctness fixes

1. **Gap** was computed from daily close-to-close change; it is now today's first completed-session opening price vs previous completed-session close.
2. **Volume surge** previously compared partial-session daily volume with 20 full sessions; the new measure compares equal numbers of 5-minute slots from past sessions and separately confirms the breakout bar volume against the same slot on prior days.
3. **ORB** previously awarded points near an opening-range high before the two 15-minute candles existed. Now requires six correctly timestamped completed 5-minute bars, followed by a new close across the boundary.
4. **VWAP** previously compared a live-session VWAP to a possibly stale daily close. Now compares the latest fully completed 5-minute close to same-session VWAP, with a fresh cross for reclaim setups.
5. **EMA 200** previously shortened its window when fewer daily bars existed; now it requires sufficient completed history.
6. **F&O PCR/OI** previously inferred option metrics from equity buy/sell quantities, which are not option-chain PCR or true open interest. These fabricated signals were removed.
7. **Upper circuit** was inferred from a different trading-statistic endpoint. That signal was removed rather than wrongly labelled.
8. **GIFT Nifty** was labelled using an unrelated spot instrument. It was removed.
9. **FII/DII**: incomplete / market-wide data was presented as stock-specific conviction; it is not used for stock scoring.
10. **Delivery %**: an unreliable endpoint/fields combination was treated as authoritative. Missing delivery data no longer affects a score.
11. **R:R** was mechanically labelled 2:1 even if the real chart target was implausible. New stop/target planning rejects too-tight, too-wide or poor net-reward setups and explicitly labels projections.
12. **Data state:** stale quotes, partial 5m/15m bars, missing intraday market index, incomplete ORBs and missing historical volume comparisons are no-signal states, not positive signals.
13. **Scoring:** header, code, threshold, progress bar and alert scores used conflicting maxima (150/190/210). The new components sum to exactly 100.
14. **Credentials:** removed hard-coded Telegram bot credentials; alert history is deduped on disk; HTTP errors do not echo bot URLs.
15. **News:** unverified keyword-based sentiment bonuses were removed from the ranking; optional date-filtered RSS headlines are contextual only.
16. **Reports/tests:** console + JSON + CSV reports and offline synthetic unit tests.

## Trade-offs

This version prefers confirmed **high-quality candidates** over the maximum number of alerts. The strict filters may produce zero trades; that is intentional. The fallback static universe remains approximate and may be stale. Live brokerage quotes, exchange-grade holidays/price bands, authoritative F&O data, full Indian brokerage fees, and independently verified live/backtest performance are **not implemented**.

## Deployment compatibility update
- Added a `Procfile` and legacy `indian_stock_scanner.py` entry point.
- Added `--universe nse` using the official NSE EQ-series equity master with
  14-day local-cache fallback, and explicit failure rather than silently
  switching to Nifty 500.
- Background worker sends alerts only with `--telegram`; credentials must be
  set securely in the deployment environment.
