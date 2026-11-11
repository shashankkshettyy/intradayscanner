"""NSE Intraday Scanner Ultimate: confirmation-first, research/paper trading only.

Uses delayed/best-effort Yahoo historical data; never places orders.
Unknown inputs are never represented as positive confirmations.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as clocktime, timedelta
from pathlib import Path
from statistics import median
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from universe_fallback import NIFTY_50, NIFTY_500_EXTRA

IST = ZoneInfo('Asia/Kolkata')
LOG = logging.getLogger('nse_scanner')
OPEN = clocktime(9, 15)
CLOSE = clocktime(15, 30)
# These are official NSE Indices constituent download endpoints; if blocked, use --symbols-file.
INDEX_CSVS = {
    'nifty50': 'https://niftyindices.com/IndexConstituent/ind_nifty50list.csv',
    'nifty500': 'https://niftyindices.com/IndexConstituent/ind_nifty500list.csv',
}
# Official NSE equity master (EQ series only). Alternative archives host for availability.
NSE_EQUITY_CSV_URLS = (
    'https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv',
    'https://archives.nseindia.com/content/equities/EQUITY_L.csv',
)

@dataclass(frozen=True)
class Config:
    universe: str = 'nifty500'
    symbols_file: Optional[str] = None
    side: str = 'both'
    mode: str = 'auto'
    threshold: int = 72
    min_price: float = 30.0
    min_median_turnover: float = 20_000_000  # ₹2 cr over 20 completed daily sessions
    min_atr_pct: float = 0.65
    max_atr_pct: float = 9.0
    max_gap_pct: float = 5.5
    min_rvol: float = 1.15
    min_rr: float = 1.7
    vix_cutoff: float = 24.0
    capital: float = 100_000.0  # OPTIONAL illustrative paper capital
    risk_pct: float = 0.5
    slippage_bps: float = 5.0  # adverse each side
    fees_bps: float = 3.0      # estimated each side, user-configurable
    batch_size: int = 15
    top: int = 25
    telegram: bool = False
    news: bool = False
    output_dir: str = 'outputs'
    alert_db: str = 'outputs/alert_history.sqlite3'
    max_data_age_minutes: int = 16


def now_ist() -> datetime:
    return datetime.now(IST)


def valid_symbol(symbol: str) -> Optional[str]:
    symbol = str(symbol).strip().upper().removesuffix('.NS')
    return f'{symbol}.NS' if re.fullmatch(r'[A-Z0-9&-]{1,30}', symbol) else None


def parse_nse_equity_symbols(contents: str) -> list[str]:
    """Read official EQUITY_L.csv and select ordinary EQ-series NSE equities."""
    reader = csv.DictReader(io.StringIO(contents.lstrip('\ufeff')))
    if not reader.fieldnames:
        return []
    header = {k.strip().upper(): k for k in reader.fieldnames if k}
    if 'SYMBOL' not in header or 'SERIES' not in header:
        raise ValueError('NSE equity master missing SYMBOL/SERIES columns')
    result = []
    for row in reader:
        if str(row.get(header['SERIES'], '')).strip().upper() == 'EQ':
            symbol = str(row.get(header['SYMBOL'], '')).strip().upper()
            if valid_symbol(symbol):
                result.append(symbol)
    return list(dict.fromkeys(result))


def fetch_nse_universe(cfg: Config) -> list[str]:
    """Load all ordinary NSE EQ listings; never silently substitute Nifty 500.

    A cached official list is allowed for up to 14 calendar days, for transient
    HTTP blocks. A missing or stale list fails explicitly rather than mislabelling
    a smaller curated universe as 'nse'.
    """
    cache = Path(cfg.output_dir) / 'nse_eq_symbols_cache.csv'
    sess = requests.Session()
    sess.headers.update({
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'),
        'Accept': 'text/csv,text/plain,*/*',
        'Referer': 'https://www.nseindia.com/',
    })
    try:
        # Some official NSE mirrors require an initial visit to acquire cookies.
        sess.get('https://www.nseindia.com/', timeout=8)
    except requests.RequestException:
        pass
    for url in NSE_EQUITY_CSV_URLS:
        try:
            resp = sess.get(url, timeout=20)
            resp.raise_for_status()
            symbols = parse_nse_equity_symbols(resp.content.decode('utf-8-sig'))
            if not 1000 <= len(symbols) <= 6000:
                raise ValueError(f'Incomplete EQ-series list ({len(symbols)} symbols)')
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text('SYMBOL,SERIES\n' + ''.join(f'{s},EQ\n' for s in symbols),
                             encoding='utf-8')
            LOG.info('Loaded %d NSE EQ-series symbols from official equity master', len(symbols))
            return symbols
        except (requests.RequestException, UnicodeError, ValueError) as exc:
            LOG.warning('NSE EQ-series source unavailable (%s): %s', url, type(exc).__name__)
    if cache.exists():
        age_days = (time.time() - cache.stat().st_mtime) / 86400
        if 0 <= age_days <= 14:
            symbols = parse_nse_equity_symbols(cache.read_text(encoding='utf-8'))
            if len(symbols) >= 1000:
                LOG.warning('Using cached NSE EQ list (%d symbols), %.1f days old',
                            len(symbols), age_days)
                return symbols
    raise RuntimeError('Full NSE EQ symbols unavailable; no Nifty-500 substitution. '
                       'Provide a current official symbols CSV via --symbols-file, '
                       'or retry when NSE is accessible.')


def resolve_symbols(cfg: Config) -> list[str]:
    if cfg.symbols_file:
        raw_text = Path(cfg.symbols_file).read_text(encoding='utf-8-sig')
        if cfg.symbols_file.lower().endswith('.csv'):
            rows = list(csv.DictReader(io.StringIO(raw_text)))
            if rows and any(str(k).strip().lower() == 'symbol' for k in rows[0]):
                key = next(k for k in rows[0] if str(k).strip().lower() == 'symbol')
                symbols = [r[key] for r in rows]
            else:
                symbols = [s for row in csv.reader(io.StringIO(raw_text)) for s in row[:1]]
        else:
            symbols = re.split(r'[,\s]+', raw_text)
    else:
        symbols = fetch_nse_universe(cfg) if cfg.universe == 'nse' else []
        if cfg.universe in INDEX_CSVS:
            try:
                r = requests.get(INDEX_CSVS[cfg.universe], timeout=12,
                                 headers={'User-Agent': 'Mozilla/5.0 (compatible; MarketResearch/1.0)'})
                r.raise_for_status()
                rows = list(csv.DictReader(io.StringIO(r.content.decode('utf-8-sig'))))
                key = next((k for k in (rows[0] if rows else {}) if k.strip().lower() == 'symbol'), None)
                if key:
                    symbols = [row[key] for row in rows]
                if len(symbols) < (40 if cfg.universe == 'nifty50' else 400):
                    raise ValueError('Incomplete constituent CSV')
                LOG.info('Loaded %d current constituents from NSE Indices', len(symbols))
            except Exception as ex:
                LOG.warning('NSE Indices constituent list not available: %s', ex)
        if not symbols:
            symbols = NIFTY_50 if cfg.universe == 'nifty50' else NIFTY_50 + NIFTY_500_EXTRA
            LOG.warning('Using a FALLBACK (%d symbols), NOT a verified current %s universe',
                        len(symbols), cfg.universe)
    seen = set()
    out = []
    for symbol in symbols:
        v = valid_symbol(symbol)
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    d = frame.copy()
    if isinstance(d.columns, pd.MultiIndex):
        # A single downloaded ticker occasionally still has a two-level index.
        if len(set(d.columns.get_level_values(0))) == 1:
            d.columns = d.columns.get_level_values(1)
        elif len(set(d.columns.get_level_values(1))) == 1:
            d.columns = d.columns.get_level_values(0)
        else:
            return pd.DataFrame()
    required = ['Open', 'High', 'Low', 'Close', 'Volume']
    if any(k not in d for k in required):
        return pd.DataFrame()
    d = d[required].copy()
    for col in required:
        d[col] = pd.to_numeric(d[col], errors='coerce')
    d = d.replace([np.inf, -np.inf], np.nan).dropna()
    d = d[(d.Close > 0) & (d.Open > 0) & (d.Volume >= 0) &
          (d.High >= d[['Open', 'Close', 'Low']].max(axis=1)) &
          (d.Low <= d[['Open', 'Close', 'High']].min(axis=1))]
    if not isinstance(d.index, pd.DatetimeIndex):
        return pd.DataFrame()
    return d.sort_index()[~d.sort_index().index.duplicated(keep='last')]


def split_download(data: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    if data is None or data.empty:
        return {}
    out = {}
    for ticker in tickers:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if ticker in data.columns.get_level_values(0):
                    sub = data[ticker]
                elif ticker in data.columns.get_level_values(1):
                    sub = data.xs(ticker, axis=1, level=1)
                else:
                    continue
            elif len(tickers) == 1:
                sub = data
            else:
                continue
            sub = normalize_frame(sub)
            if not sub.empty:
                out[ticker] = sub
        except (KeyError, TypeError, ValueError):
            LOG.debug('Invalid downloaded shape for %s', ticker)
    return out


def download(tickers: list[str], *, interval: str, period: str, batch_size: int = 15) -> dict:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError('Install requirements.txt first (yfinance is missing)') from exc
    out = {}
    for offset in range(0, len(tickers), batch_size):
        batch = tickers[offset:offset + batch_size]
        try:
            raw = yf.download(tickers=batch, period=period, interval=interval,
                              group_by='ticker', auto_adjust=True, prepost=False,
                              threads=False, progress=False, timeout=15)
            chunk = split_download(raw, batch)
            out.update(chunk)
            if len(chunk) < len(batch):
                LOG.warning('%s: %d/%d usable in batch %d', interval, len(chunk), len(batch), offset // batch_size + 1)
        except Exception as exc:
            LOG.warning('%s batch failed: %s', interval, exc)
        if offset + batch_size < len(tickers):
            time.sleep(0.25)
    LOG.info('%s: usable data for %d/%d tickers', interval, len(out), len(tickers))
    return out


def to_ist(frame: pd.DataFrame) -> pd.DataFrame:
    d = normalize_frame(frame)
    if d.empty:
        return d
    # Yahoo daily history is date-labelled; only intraday timestamps need tz conversion.
    d.index = d.index.tz_localize(IST) if d.index.tz is None else d.index.tz_convert(IST)
    return d


def daily_before(frame: pd.DataFrame, session: date) -> pd.DataFrame:
    d = to_ist(frame)
    return d[pd.Index(d.index.date) < session]


def completed_5m(frame: pd.DataFrame, at: datetime) -> pd.DataFrame:
    d = to_ist(frame)
    if d.empty:
        return d
    if at.tzinfo is None:
        raise ValueError('at must be timezone-aware')
    # yfinance timestamps 5-minute candle STARTS; drop in-progress bar and future bars.
    cutoff = at.astimezone(IST) - timedelta(minutes=5, seconds=10)
    d = d[d.index <= cutoff]
    minute = d.index.hour * 60 + d.index.minute
    d = d[(minute >= 9 * 60 + 15) & (minute < 15 * 60 + 30)]
    return d


def today_5m(frame: pd.DataFrame, at: datetime) -> pd.DataFrame:
    d = completed_5m(frame, at)
    return d[pd.Index(d.index.date) == at.astimezone(IST).date()]


def is_fresh(frame: pd.DataFrame, at: datetime, max_age_minutes: int) -> bool:
    return (not frame.empty and at.astimezone(IST) - frame.index[-1].to_pydatetime() <=
            timedelta(minutes=max_age_minutes))


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    gain = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = down.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - 100 / (1 + rs)
    return result.where(loss != 0, 100).where(gain != 0, 0).where(~((gain == 0) & (loss == 0)), 50)


def atr(d: pd.DataFrame, n: int = 14) -> pd.Series:
    previous_close = d.Close.shift(1)
    tr = pd.concat([d.High - d.Low, (d.High - previous_close).abs(),
                    (d.Low - previous_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def session_vwap(d: pd.DataFrame) -> pd.Series:
    typ = (d.High + d.Low + d.Close) / 3.0
    denom = d.Volume.cumsum().replace(0, np.nan)
    return (typ * d.Volume).cumsum() / denom


def session_bars(d: pd.DataFrame, session: date) -> pd.DataFrame:
    return d[pd.Index(d.index.date) == session]


def opening_range(d: pd.DataFrame, session: date) -> Optional[tuple[float, float]]:
    today = session_bars(d, session)
    opening = today.between_time('09:15', '09:40')
    expected = [datetime.combine(session, clocktime(9, 15), IST) + timedelta(minutes=5 * i)
                for i in range(6)]
    if len(opening) != 6 or list(opening.index) != expected:
        return None
    return float(opening.High.max()), float(opening.Low.min())


def resample_15m(d: pd.DataFrame) -> pd.DataFrame:
    if d.empty:
        return d
    parts = []
    for day in sorted(set(d.index.date)):
        group = session_bars(d, day)
        b = group.resample('15min', label='left', closed='left', origin='start_day').agg(
            {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
        counts = group.Close.resample('15min', label='left', closed='left', origin='start_day').count()
        b = b[counts == 3]
        if not b.empty:
            parts.append(b)
    return pd.concat(parts) if parts else pd.DataFrame(columns=d.columns)


def intraday_rvol(all_5m: pd.DataFrame, session: date, bars_today: int,
                   lookback_days: int = 8) -> Optional[float]:
    """Cumulative volume vs equal-number-of-bars volumes on prior sessions.
    Excludes the current day and sessions with insufficient aligned bars.
    """
    if bars_today < 1:
        return None
    today = session_bars(all_5m, session)
    current = float(today.Volume.iloc[:bars_today].sum())
    history = []
    for day in sorted(set(all_5m.index.date), reverse=True):
        if day >= session:
            continue
        b = session_bars(all_5m, day)
        # Reject sessions with missing first bars / gaps in the window.
        expected = [datetime.combine(day, clocktime(9, 15), IST) + timedelta(minutes=5*i)
                    for i in range(bars_today)]
        if len(b) >= bars_today and list(b.index[:bars_today]) == expected:
            history.append(float(b.Volume.iloc[:bars_today].sum()))
        if len(history) == lookback_days:
            break
    usable = [v for v in history if v > 0]
    return current / median(usable) if len(usable) >= 3 else None


def last_bar_relative_volume(all_5m: pd.DataFrame, at_index: pd.Timestamp,
                             lookback_days: int = 8) -> Optional[float]:
    # Compare the specific five-minute slot (e.g. 10:05) on previous sessions.
    values = []
    today = at_index.date()
    for day in sorted(set(all_5m.index.date), reverse=True):
        if day >= today:
            continue
        ts = datetime.combine(day, at_index.timetz().replace(tzinfo=None), IST)
        if ts in all_5m.index and float(all_5m.loc[ts, 'Volume']) > 0:
            values.append(float(all_5m.loc[ts, 'Volume']))
        if len(values) == lookback_days:
            break
    return float(all_5m.loc[at_index, 'Volume']) / median(values) if len(values) >= 3 else None


# Running in --schedule reuses the same *daily* history for the trading day.
# Never reuse the intraday snapshot: live confirmation must stay fresh.
_DAILY_HISTORY_CACHE: dict[tuple, tuple[date, dict]] = {}


def daily_history_cached(symbols: list[str], cfg: Config, at: datetime) -> dict:
    key = tuple(symbols)
    day = at.astimezone(IST).date()
    cached = _DAILY_HISTORY_CACHE.get(key)
    if cached and cached[0] == day and len(cached[1]) >= 0.5 * len(symbols):
        LOG.info('Daily history cache hit: %d symbols', len(cached[1]))
        return cached[1]
    result = download(symbols, interval='1d', period='2y', batch_size=cfg.batch_size)
    if len(result) >= 0.5 * len(symbols):
        _DAILY_HISTORY_CACHE[key] = (day, result)
    return result


def daily_features(d: pd.DataFrame, at: datetime, cfg: Config) -> Optional[dict]:
    session = at.astimezone(IST).date()
    # Watchlist after 16:00 may use today's completed daily bar; never live.
    exclusive = session + timedelta(days=1) if at.astimezone(IST).time() >= clocktime(16, 0) else session
    history = daily_before(d, exclusive)
    if len(history) < 210:
        return None  # Never pretend a 60-day moving average is EMA200.
    if (at.astimezone(IST).date() - history.index[-1].date()).days > 7:
        return None
    c = history.Close
    price = float(c.iloc[-1])
    median_turnover = float((history.Close * history.Volume).iloc[-20:].median())
    daily_atr = float(atr(history).iloc[-1])
    atr_pct = daily_atr / price * 100
    if not (math.isfinite(daily_atr) and daily_atr > 0 and
            price >= cfg.min_price and median_turnover >= cfg.min_median_turnover and
            cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct):
        return None
    return {
        'prev_close': price,
        'prev_high': float(history.High.iloc[-1]),
        'prev_low': float(history.Low.iloc[-1]),
        'high20': float(history.High.iloc[-20:].max()),
        'low20': float(history.Low.iloc[-20:].min()),
        'ema20': float(ema(c, 20).iloc[-1]),
        'ema50': float(ema(c, 50).iloc[-1]),
        'ema200': float(ema(c, 200).iloc[-1]),
        'slope20': float(ema(c, 20).iloc[-1] / ema(c, 20).iloc[-6] - 1),
        'rsi_daily': float(rsi(c).iloc[-1]),
        'atr': daily_atr,
        'atr_pct': atr_pct,
        'turnover_cr': median_turnover / 10_000_000,
        'last_daily_date': str(history.index[-1].date()),
    }


def market_context(index_intraday: Optional[pd.DataFrame],
                   vix_daily: Optional[pd.DataFrame], at: datetime, cfg: Config) -> dict:
    context = {'market_direction': 'UNKNOWN', 'index_fresh': False, 'vix': None,
               'index_pct': None, 'vix_restricted': False}
    if vix_daily is not None and not vix_daily.empty:
        # VIX may use same-day daily close, depending on feed. Only regime gating, no predictive claim.
        data = normalize_frame(vix_daily)
        if not data.empty:
            v = float(data.Close.iloc[-1])
            if math.isfinite(v) and v > 0:
                context['vix'] = round(v, 2)
                context['vix_restricted'] = v > cfg.vix_cutoff
    if index_intraday is not None and not index_intraday.empty:
        bars = today_5m(index_intraday, at)
        if len(bars) >= 6 and is_fresh(bars, at, cfg.max_data_age_minutes):
            vwap = float(session_vwap(bars).iloc[-1])
            close = float(bars.Close.iloc[-1])
            context['index_fresh'] = True
            context['index_pct'] = round((close / float(bars.Open.iloc[0]) - 1) * 100, 2)
            context['market_direction'] = 'BULLISH' if close > vwap * 1.001 else (
                'BEARISH' if close < vwap * 0.999 else 'NEUTRAL')
    return context


def confirm_signal(today: pd.DataFrame, all_5m: pd.DataFrame, side: str,
                   orb: tuple[float, float], vwap_curve: pd.Series,
                   ema9: pd.Series, rsi_15: float) -> Optional[str]:
    if len(today) < 8 or not math.isfinite(rsi_15):
        return None
    prev = today.iloc[-2]
    last = today.iloc[-1]
    last_vwap = float(vwap_curve.iloc[-1])
    prev_vwap = float(vwap_curve.iloc[-2])
    up = side == 'LONG'
    if up and (last.Close < last_vwap or last.Close < ema9.iloc[-1] or rsi_15 < 48):
        return None
    if not up and (last.Close > last_vwap or last.Close > ema9.iloc[-1] or rsi_15 > 52):
        return None
    bar_rvol = last_bar_relative_volume(all_5m, today.index[-1])
    # No volume confirmation => no breakout alert, not a fabricated positive signal.
    if bar_rvol is None or bar_rvol < 1.15:
        return None
    or_high, or_low = orb
    breakout = or_high * 1.0005 if up else or_low * 0.9995
    if up and prev.Close <= breakout < last.Close and last.Close > last.Open:
        return 'ORB BREAKOUT'
    if not up and prev.Close >= breakout > last.Close and last.Close < last.Open:
        return 'ORB BREAKDOWN'
    # The move must be a *fresh* VWAP cross after the opening range is finalized.
    if up and prev.Close <= prev_vwap and last.Close > last_vwap and last.Close > last.Open:
        return 'VWAP RECLAIM'
    if not up and prev.Close >= prev_vwap and last.Close < last_vwap and last.Close < last.Open:
        return 'VWAP REJECTION'
    # A continuation entry is a renewed cross of the 5m fast EMA, not just a high RSI.
    if len(ema9) > 2 and up and prev.Close <= ema9.iloc[-2] and last.Close > ema9.iloc[-1] \
            and last.Close > or_high:
        return 'PULLBACK CONTINUATION'
    if len(ema9) > 2 and not up and prev.Close >= ema9.iloc[-2] and last.Close < ema9.iloc[-1] \
            and last.Close < or_low:
        return 'PULLBACK CONTINUATION'
    return None


def trade_plan(side: str, entry: float, recent: pd.DataFrame,
               vwap: float, orb: tuple[float, float], daily: dict,
               setup: str, cfg: Config) -> Optional[dict]:
    if len(recent) < 3:
        return None
    bar_atr = float(atr(recent, 10).iloc[-1]) if len(recent) >= 10 else float('nan')
    if not math.isfinite(bar_atr):
        return None
    # Use recent price structure and a 5m ATR buffer; avoid the false zero-risk trick.
    if side == 'LONG':
        structural = float(recent.Low.iloc[-4:-1].min())
        if setup == 'ORB BREAKOUT':
            structural = min(structural, orb[0])
        elif setup == 'VWAP RECLAIM':
            structural = min(structural, vwap)
        stop = structural - 0.15 * bar_atr
        risk = entry - stop
    else:
        structural = float(recent.High.iloc[-4:-1].max())
        if setup == 'ORB BREAKDOWN':
            structural = max(structural, orb[1])
        elif setup == 'VWAP REJECTION':
            structural = max(structural, vwap)
        stop = structural + 0.15 * bar_atr
        risk = stop - entry
    # Don't present stops that are too tight for noise or too wide for intraday use.
    if not (0.12 * daily['atr'] <= risk <= 0.90 * daily['atr']):
        return None
    if side == 'LONG':
        resistance = min([v for v in (daily['prev_high'], daily['high20']) if v > entry], default=None)
        projected_target = entry + max(2.2 * risk, orb[0] - orb[1])
        target = min(projected_target, resistance) if resistance else projected_target
        gross_reward = target - entry
    else:
        support = max([v for v in (daily['prev_low'], daily['low20']) if v < entry], default=None)
        projected_target = entry - max(2.2 * risk, orb[0] - orb[1])
        target = max(projected_target, support) if support else projected_target
        gross_reward = entry - target
    # Two-sided friction: adverse estimated entry + exit costs/slippage.
    round_trip_friction = 2 * entry * (cfg.slippage_bps + cfg.fees_bps) / 10000
    net_risk = risk + round_trip_friction
    net_reward = gross_reward - round_trip_friction
    net_rr = net_reward / net_risk if net_risk > 0 else 0
    if net_rr < cfg.min_rr or not (math.isfinite(net_rr) and stop > 0 and target > 0):
        return None
    risk_budget = cfg.capital * cfg.risk_pct / 100
    theoretical_quantity = min(math.floor(risk_budget / net_risk), math.floor(cfg.capital / entry))
    return {
        'entry': round(entry, 2), 'sl': round(stop, 2), 'target': round(target, 2),
        'gross_risk': round(risk, 2), 'net_rr': round(net_rr, 2),
        'risk_budget': round(risk_budget, 2),
        'paper_qty': theoretical_quantity if side == 'LONG' else None,
        'target_type': ('RESISTANCE-LIMITED' if (side == 'LONG' and resistance is not None and resistance < projected_target)
                        or (side == 'SHORT' and support is not None and support > projected_target) else 'PROJECTED'),
    }


def score_live(side: str, daily: dict, gap: float, current: float, intraday: dict,
               market: dict, setup: str) -> tuple[int, list[str], dict]:
    up = side == 'LONG'
    points = {}
    reasons = []
    trend = (daily['ema20'] > daily['ema50'] > daily['ema200'] and daily['slope20'] > 0)
    downtrend = (daily['ema20'] < daily['ema50'] < daily['ema200'] and daily['slope20'] < 0)
    points['daily_trend'] = 15 if (trend if up else downtrend) else 0
    if points['daily_trend']:
        reasons.append('Daily 20/50/200 EMA alignment')
    md = market['market_direction']
    points['index_regime'] = 10 if md == ('BULLISH' if up else 'BEARISH') else (3 if md == 'NEUTRAL' else 0)
    points['relative_strength'] = 10 if (intraday['session_pct'] - (market['index_pct'] or 0)) * (1 if up else -1) > 0.6 else 0
    rvol = intraday['rvol']
    points['time_matched_volume'] = 15 if rvol >= 2.2 else 12 if rvol >= 1.7 else 8 if rvol >= 1.25 else 4
    if rvol >= 1.5:
        reasons.append(f'{rvol:.2f}x time-matched volume')
    deviation = (current / intraday['vwap'] - 1) * 100 * (1 if up else -1)
    points['vwap'] = 10 if 0 <= deviation <= 1 else 5 if 1 < deviation <= 1.8 else 0
    points['opening_range'] = 10 if setup.startswith('ORB') else 6 if setup.startswith('PULLBACK') else 4
    value = intraday['rsi15']
    points['momentum'] = 10 if (50 <= value <= 69 if up else 31 <= value <= 50) else 5
    points['candle_confirmation'] = 10 if intraday['bar_rvol'] >= 1.8 else 7 if intraday['bar_rvol'] >= 1.3 else 5
    points['gap'] = 5 if 0.3 <= gap * (1 if up else -1) <= 2.5 else 2 if abs(gap) < 3.5 else 0
    points['liquidity'] = 5 if daily['turnover_cr'] >= 10 else 3
    reasons.extend([setup, 'Price confirmed beyond session VWAP', f'15m RSI {value:.0f}'])
    return sum(points.values()), reasons, points


def opening_gap_pct(first_open: float, previous_close: float) -> float:
    if previous_close <= 0:
        raise ValueError('Previous close must be positive')
    return (first_open / previous_close - 1) * 100


def evaluate_live(symbol: str, daily: dict, intraday_frame: pd.DataFrame,
                  market: dict, at: datetime, cfg: Config) -> list[dict]:
    if not market['index_fresh'] or market['vix_restricted']:
        return []
    all_bars = completed_5m(intraday_frame, at)
    today = session_bars(all_bars, at.astimezone(IST).date())
    if len(today) < 8 or not is_fresh(today, at, cfg.max_data_age_minutes):
        return []
    # Refuse snapshots with missing 5m bars; cumulative RVOL must compare identical time windows.
    expected = [datetime.combine(at.astimezone(IST).date(), clocktime(9, 15), IST)
                + timedelta(minutes=5*i) for i in range(len(today))]
    if list(today.index) != expected:
        return []
    orb = opening_range(all_bars, at.astimezone(IST).date())
    if orb is None or orb[0] <= orb[1] or not math.isfinite(daily['atr']):
        return []
    # Daily price vs first trading price: actual opening gap, not change between closes.
    gap = opening_gap_pct(float(today.Open.iloc[0]), daily['prev_close'])
    if abs(gap) > cfg.max_gap_pct:
        return []
    rvol = intraday_rvol(all_bars, at.astimezone(IST).date(), len(today))
    if rvol is None or rvol < cfg.min_rvol:
        return []
    bars15 = resample_15m(all_bars)
    if len(bars15) < 22:
        return []
    rsi15 = float(rsi(bars15.Close).iloc[-1])
    if not math.isfinite(rsi15):
        return []
    vwap_curve = session_vwap(today)
    vwap = float(vwap_curve.iloc[-1])
    if not math.isfinite(vwap):
        return []
    current = float(today.Close.iloc[-1])
    close_all = all_bars.Close
    ema9_all = ema(close_all, 9)
    ema9 = ema9_all.loc[today.index]
    if not math.isfinite(float(ema9.iloc[-1])):
        return []
    last_slot_rvol = last_bar_relative_volume(all_bars, today.index[-1])
    if last_slot_rvol is None:
        return []
    features = dict(vwap=vwap, rvol=rvol, rsi15=rsi15, bar_rvol=last_slot_rvol,
                    session_pct=(current / float(today.Open.iloc[0]) - 1) * 100)
    out = []
    for side in (['LONG', 'SHORT'] if cfg.side == 'both' else [cfg.side.upper()]):
        # Avoid initiating a trade against the broad index or a weak daily trend.
        up = side == 'LONG'
        aligned = (daily['ema20'] > daily['ema50'] > daily['ema200'] and daily['slope20'] > 0) if up else (
            daily['ema20'] < daily['ema50'] < daily['ema200'] and daily['slope20'] < 0)
        if not aligned or market['market_direction'] != ('BULLISH' if up else 'BEARISH'):
            continue
        setup = confirm_signal(today, all_bars, side, orb, vwap_curve, ema9, rsi15)
        if not setup:
            continue
        # Price chasing guard (use the known last-completed close).
        deviation = abs(current - vwap)
        if deviation > 1.2 * daily['atr']:
            continue
        plan = trade_plan(side, current, today, vwap, orb, daily, setup, cfg)
        if plan is None:
            continue
        score, reasons, breakdown = score_live(side, daily, gap, current, features, market, setup)
        if score < cfg.threshold:
            continue
        out.append({
            'symbol': symbol.removesuffix('.NS'), 'side': side, 'status': 'CONFIRMED_CANDLE',
            'score': score, 'setup': setup, 'bar_time_ist': today.index[-1].isoformat(),
            'gap_pct': round(gap, 2), 'session_change_pct': round(features['session_pct'], 2),
            'rvol': round(rvol, 2), 'breakout_bar_rvol': round(last_slot_rvol, 2),
            'vwap': round(vwap, 2), 'orb_high': round(orb[0], 2), 'orb_low': round(orb[1], 2),
            'rsi15': round(rsi15, 1), 'atr_daily': round(daily['atr'], 2),
            'turnover_cr': round(daily['turnover_cr'], 1),
            'score_components': breakdown, 'reasons': reasons, 'market': market['market_direction'],
            **plan,
        })
    return out


def evaluate_watchlist(symbol: str, d: dict, cfg: Config) -> Optional[dict]:
    # End-of-session research is deliberately not described as an executable entry.
    c = d['prev_close']
    direction = 'LONG' if d['ema20'] > d['ema50'] > d['ema200'] and d['slope20'] > 0 else (
        'SHORT' if d['ema20'] < d['ema50'] < d['ema200'] and d['slope20'] < 0 else None)
    if not direction or (cfg.side != 'both' and direction.lower() != cfg.side):
        return None
    is_long = direction == 'LONG'
    if (is_long and not 45 <= d['rsi_daily'] <= 75) or (not is_long and not 25 <= d['rsi_daily'] <= 55):
        return None
    score = 55
    score += 15 if ((c > d['ema20']) if is_long else (c < d['ema20'])) else 0
    score += 10 if ((d['slope20'] > 0.02) if is_long else (d['slope20'] < -0.02)) else 5
    score += 10 if d['turnover_cr'] >= 10 else 5
    score += 10 if 1.2 <= d['atr_pct'] <= 4 else 5
    return {'symbol': symbol.removesuffix('.NS'), 'side': direction,
            'status': 'WATCHLIST_ONLY', 'score': min(score, 100),
            'reference_close': round(c, 2), 'rsi_daily': round(d['rsi_daily'], 1),
            'atr_daily': round(d['atr'], 2), 'turnover_cr': round(d['turnover_cr'], 1),
            'asof_session': d['last_daily_date'],
            'note': 'NO ENTRY: wait for fresh session ORB/VWAP/volume confirmation'}


def detect_mode(at: datetime, requested: str) -> str:
    if requested != 'auto':
        return requested
    t = at.astimezone(IST).time()
    return 'live' if at.weekday() < 5 and clocktime(9, 50) <= t <= clocktime(15, 15) else 'watchlist'


def get_headlines(symbol: str, at: datetime, limit: int = 2) -> list[dict]:
    """Unscored, optional contextual RSS. A headline is not proof of a catalyst."""
    try:
        from urllib.parse import quote_plus
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        url = ('https://news.google.com/rss/search?q=' +
               quote_plus(f'"{symbol}" NSE stock when:1d') + '&hl=en-IN&gl=IN&ceid=IN:en')
        resp = requests.get(url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        result = []
        for item in ET.fromstring(resp.content).findall('./channel/item'):
            title = item.findtext('title') or ''
            published = item.findtext('pubDate') or ''
            stamp = parsedate_to_datetime(published)
            hours = (at - stamp.astimezone(IST)).total_seconds() / 3600
            if 0 <= hours <= 30:
                result.append({'title': title[:160], 'published': stamp.isoformat(),
                               'url': item.findtext('link') or ''})
            if len(result) == limit:
                break
        return result
    except Exception as exc:
        LOG.debug('News unavailable for %s: %s', symbol, exc)
        return []


def save_reports(rows: list[dict], meta: dict, cfg: Config, at: datetime) -> tuple[Path, Path]:
    path = Path(cfg.output_dir)
    path.mkdir(parents=True, exist_ok=True)
    stem = f"scan_{at.strftime('%Y%m%d_%H%M%S')}_{meta['mode']}"
    json_path = path / (stem + '.json')
    csv_path = path / (stem + '.csv')
    payload = {'metadata': meta, 'config': asdict(cfg), 'results': rows}
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    fields = sorted(set().union(*(r.keys() for r in rows))) if rows else ['symbol', 'status']
    with csv_path.open('w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()})
    return json_path, csv_path


def telegram_send(text: str) -> bool:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '8758337375:AAEPZFGzwBMUs9KVWmHjWy1Yiafowt-y7tQ').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '650190617').strip()
    if not token or not chat_id:
        LOG.warning('Telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
        return False
    try:
        result = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                               json={'chat_id': chat_id, 'text': text[:3900],
                                     'parse_mode': 'HTML', 'disable_web_page_preview': True},
                               timeout=12)
        result.raise_for_status()
        if not result.json().get('ok'):
            raise ValueError('Telegram returned ok=false')
        return True
    except Exception as exc:
        # Do NOT log the exception URL: Telegram API URL contains a bot credential.
        LOG.warning('Telegram send failed (%s). Check credentials and connectivity.', type(exc).__name__)
        return False


def send_new_alerts(rows: list[dict], cfg: Config, at: datetime) -> None:
    if not cfg.telegram or not rows:
        return
    db = Path(cfg.alert_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS sent (alert_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)')
        for row in rows:
            if row['status'] != 'CONFIRMED_CANDLE':
                continue
            key = f"{at.date()}|{row['symbol']}|{row['side']}|{row['setup']}"
            if conn.execute('SELECT 1 FROM sent WHERE alert_key=?', (key,)).fetchone():
                continue
            e = lambda value: html.escape(str(value))
            qty = str(row['paper_qty']) if row['paper_qty'] is not None else 'N/A (short margin)' 
            message = (
                f"📡 <b>5m CANDLE CONFIRMED · {e(row['side'])}</b>\n"
                f"<b>{e(row['symbol'])}</b> | Score {row['score']}/100\n"
                f"{e(row['setup'])} · {e(row['bar_time_ist'])}\n"
                f"Indicative entry ₹{row['entry']:,.2f}\n"
                f"Structural SL ₹{row['sl']:,.2f}\n"
                f"Projected target ₹{row['target']:,.2f}\n"
                f"Estimated net R:R {row['net_rr']:.2f}:1\n"
                f"RVOL {row['rvol']:.2f}x | Bar RVOL {row['breakout_bar_rvol']:.2f}x\n"
                f"VWAP ₹{row['vwap']:,.2f} | ORB ₹{row['orb_low']:,.2f}–₹{row['orb_high']:,.2f}\n"
                f"Paper size {qty} | Risk budget ₹{row['risk_budget']:,.2f}\n"
                f"⚠️ Research only. Delayed quotes / slippage / execution not verified."
            )
            if telegram_send(message):
                conn.execute('INSERT INTO sent (alert_key, created_at) VALUES (?, ?)',
                             (key, at.isoformat()))
                conn.commit()
                time.sleep(0.15)


def scan(cfg: Config, at: Optional[datetime] = None) -> list[dict]:
    at = at or now_ist()
    mode = detect_mode(at, cfg.mode)
    if mode == 'live' and (at.weekday() >= 5 or not (clocktime(9, 45) <= at.time() <= CLOSE)):
        LOG.warning('Not during regular market hours. Refusing live signal generation.')
        return []
    started = time.monotonic()
    symbols = resolve_symbols(cfg)
    if not symbols:
        raise ValueError('No valid stock symbols')
    LOG.info('Mode=%s at %s, stocks=%d', mode, at.isoformat(), len(symbols))
    # Download one year so EMA200 is a REAL EMA200. Intraday only after daily pre-filter.
    daily = daily_history_cached(symbols, cfg, at)
    validated = {sym: feat for sym, frame in daily.items()
                 if (feat := daily_features(frame, at, cfg)) is not None}
    LOG.info('Daily qualified: %d/%d; usable history: %d', len(validated), len(symbols), len(daily))
    rows = []
    context = {'market_direction': 'NOT_APPLICABLE', 'index_fresh': False,
               'vix': None, 'vix_restricted': False}
    if mode == 'live' and validated:
        context_data = download(['^NSEI', '^INDIAVIX'], interval='1d', period='5d', batch_size=2)
        vix_frame = context_data.get('^INDIAVIX')
        # Fetch index separately to avoid dropping it from a mixed market symbol batch.
        intraday = download(list(validated) + ['^NSEI'], interval='5m', period='1mo',
                            batch_size=cfg.batch_size)
        context = market_context(intraday.get('^NSEI'), vix_frame, at, cfg)
        if not context['index_fresh']:
            LOG.warning('NIFTY 50 intraday data missing/stale: NO live entries will be emitted')
        if context['vix_restricted']:
            LOG.warning('VIX %.2f > %.2f; live entries disabled', context['vix'], cfg.vix_cutoff)
        for sym, d in validated.items():
            if sym in intraday:
                rows.extend(evaluate_live(sym, d, intraday[sym], context, at, cfg))
    else:
        for sym, d in validated.items():
            result = evaluate_watchlist(sym, d, cfg)
            if result:
                rows.append(result)
    rows.sort(key=lambda r: (r['score'], r.get('rvol', 0)), reverse=True)
    all_rows = rows
    rows = rows[:cfg.top]
    if cfg.news:
        for row in rows[:5]:
            row['headlines_unverified_context_only'] = get_headlines(row['symbol'], at)
    meta = {'asof_ist': at.isoformat(), 'mode': mode, 'universe_count': len(symbols),
            'daily_usable': len(daily), 'daily_qualified': len(validated),
            'matching_signals': len(all_rows), 'shown': len(rows),
            'elapsed_seconds': round(time.monotonic() - started, 1), 'market': context,
            'data_source': 'Yahoo Finance best-effort (not exchange or broker live feed)',
            'caution': 'Research/paper trading only. No order placement or performance guarantee.'}
    json_path, csv_path = save_reports(rows, meta, cfg, at)
    print('\n' + '=' * 100)
    print(f"NSE SCANNER | {meta['mode'].upper()} | {at.strftime('%d %b %Y %H:%M:%S IST')}")
    print(f"Universe {len(symbols)} | daily-qualified {len(validated)} | matches {len(all_rows)}")
    print(f"Index: {context['market_direction']} | VIX: {context['vix'] or 'N/A'}")
    print('=' * 100)
    if rows:
        for row in rows:
            if mode == 'live':
                print(f"{row['symbol']:<17} {row['side']:<5} {row['score']:>3}/100 "
                      f"{row['setup']:<24} Entry {row['entry']:>9.2f} SL {row['sl']:>9.2f} "
                      f"Tgt {row['target']:>9.2f} net R:R {row['net_rr']:.2f}")
            else:
                print(f"{row['symbol']:<17} {row['side']:<5} {row['score']:>3}/100 "
                      f"WATCHLIST ONLY · Ref ₹{row['reference_close']:.2f} · NO ENTRY")
    else:
        print('No confirmed candidates. This is a VALID outcome; do not force trades.')
    print(f'JSON: {json_path}\nCSV:  {csv_path}\n')
    if mode == 'live':
        send_new_alerts(rows, cfg, at)
    return rows


def run_schedule(cfg: Config, interval_minutes: int):
    if interval_minutes < 5:
        raise ValueError('Schedule interval must be at least 5 minutes')
    LOG.info('Schedule starts 09:50 IST, every %d minutes until 15:05 IST on weekdays', interval_minutes)
    last_run = None
    while True:
        t = now_ist()
        # Monotonic wall-clock slot avoids restarting scans more than once per slot.
        minutes_since_start = (t.hour * 60 + t.minute) - (9 * 60 + 50)
        allowed = (t.weekday() < 5 and 0 <= minutes_since_start <= 315
                   and minutes_since_start % interval_minutes == 0)
        key = (t.date(), minutes_since_start)
        if allowed and last_run != key:
            last_run = key
            try:
                scan(Config(**{**asdict(cfg), 'mode': 'live'}), t)
            except Exception:
                LOG.exception('Scheduled scan failed; next slot will retry')
        time.sleep(15)


def arguments() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--universe', choices=['nifty50', 'nifty500', 'nse'], default='nifty500')
    p.add_argument('--symbols-file', help='CSV with Symbol column or text list, overrides universe')
    p.add_argument('--mode', choices=['auto', 'live', 'watchlist'], default='auto')
    p.add_argument('--side', choices=['both', 'long', 'short'], default='both')
    p.add_argument('--threshold', type=int, default=72)
    p.add_argument('--min-rvol', type=float, default=1.15)
    p.add_argument('--min-rr', type=float, default=1.7)
    p.add_argument('--capital', type=float, default=100000)
    p.add_argument('--risk-pct', type=float, default=0.5)
    p.add_argument('--slippage-bps', type=float, default=5)
    p.add_argument('--fees-bps', type=float, default=3)
    p.add_argument('--top', type=int, default=25)
    p.add_argument('--batch-size', type=int, default=15)
    p.add_argument('--telegram', action='store_true', help='Send NEW, confirmed intraday signals only')
    p.add_argument('--news', action='store_true', help='Attach unscored recent RSS headlines to top 5')
    p.add_argument('--output-dir', default='outputs')
    p.add_argument('--alert-db', default='outputs/alert_history.sqlite3')
    p.add_argument('--schedule', action='store_true')
    p.add_argument('--interval-minutes', type=int, default=15)
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(message)s')
    a = arguments()
    if not 0 <= a.threshold <= 100 or not 0 < a.risk_pct <= 2 or a.capital <= 0 or a.min_rr < 1:
        raise SystemExit('Invalid threshold, capital, max paper risk percentage (2%) or min R:R')
    cfg = Config(universe=a.universe, symbols_file=a.symbols_file, mode=a.mode,
                 side=a.side, threshold=a.threshold, min_rvol=a.min_rvol,
                 min_rr=a.min_rr, capital=a.capital, risk_pct=a.risk_pct,
                 slippage_bps=a.slippage_bps, fees_bps=a.fees_bps,
                 top=a.top, batch_size=a.batch_size, telegram=a.telegram,
                 news=a.news, output_dir=a.output_dir, alert_db=a.alert_db)
    if a.schedule:
        run_schedule(cfg, a.interval_minutes)
    else:
        scan(cfg)


if __name__ == '__main__':
    main()
