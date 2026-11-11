import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import scanner as sc


def make_day(day, closes, vols=None):
    idx = pd.DatetimeIndex([datetime.combine(day, time(9, 15), sc.IST) + timedelta(minutes=5*i)
                            for i in range(len(closes))])
    closes = np.array(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({'Open': opens, 'Close': closes,
                         'High': np.maximum(opens, closes) + 0.15,
                         'Low': np.minimum(opens, closes) - 0.15,
                         'Volume': vols if vols is not None else np.full(len(closes), 10000)}, index=idx)


def test_true_gap_is_open_vs_prev_close():
    assert sc.opening_gap_pct(103, 100) == pytest.approx(3.0)
    assert sc.opening_gap_pct(98, 100) == pytest.approx(-2.0)
    with pytest.raises(ValueError):
        sc.opening_gap_pct(100, 0)


def test_partial_candle_excluded_and_no_future_leakage():
    d = make_day(date(2026, 11, 11), [100, 101, 102])
    at = datetime(2026, 11, 11, 9, 25, tzinfo=sc.IST)
    actual = sc.completed_5m(d, at)
    assert list(actual.index) == list(d.index[:1])  # conservative ten-second finalization buffer
    assert sc.today_5m(d, at).shape[0] == 1


def test_orb_requires_six_exact_completed_bars():
    d = make_day(date(2026, 11, 11), [100, 101, 102, 99, 103, 100, 107])
    assert sc.opening_range(d.iloc[:5], date(2026, 11, 11)) is None
    assert sc.opening_range(d, date(2026, 11, 11)) == pytest.approx((103.15, 98.85))
    assert sc.opening_range(d.drop(d.index[2]), date(2026, 11, 11)) is None


def test_rvol_is_time_matched_not_full_daily_volume():
    sessions = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 11)]
    frames = [make_day(day, [100]*12, [100]*12) for day in sessions[:3]]
    frames.append(make_day(sessions[-1], [100]*5, [200]*5))
    d = pd.concat(frames)
    assert sc.intraday_rvol(d, sessions[-1], 5) == pytest.approx(2.0)
    assert sc.last_bar_relative_volume(d, d.index[-1]) == pytest.approx(2.0)


def test_resampling_refuses_incomplete_15m():
    d = make_day(date(2026, 11, 11), [100, 101, 102, 103, 104])
    bars = sc.resample_15m(d)
    assert len(bars) == 1
    assert bars.Close.iloc[0] == 102


def test_daily_ema200_uses_only_completed_previous_sessions():
    dates = pd.bdate_range(end='2026-11-11', periods=230, tz=sc.IST)
    closes = np.linspace(80, 110, len(dates))
    d = pd.DataFrame({'Open': closes, 'High': closes+1, 'Low': closes-1,
                      'Close': closes, 'Volume': [500000]*len(dates)}, index=dates)
    at = datetime(2026, 11, 11, 10, 0, tzinfo=sc.IST)
    a = sc.daily_features(d, at, sc.Config())
    assert a is not None
    assert a['last_daily_date'] == '2026-11-10'
    assert a['prev_close'] == pytest.approx(closes[-2])
    # Changing current/unfinished daily bar must NOT change live scoring data.
    d.loc[dates[-1], 'Close'] = 9999
    d.loc[dates[-1], 'High'] = 9999
    b = sc.daily_features(d, at, sc.Config())
    assert b['ema200'] == pytest.approx(a['ema200'])
    assert b['prev_close'] == pytest.approx(a['prev_close'])


def test_stop_and_target_reject_fake_rr():
    d = make_day(date(2026, 11, 11), [101, 101.3, 101.4, 101.2, 101.5,
                                      101.7, 101.8, 101.7, 101.8, 102.0, 102.1, 102.5])
    daily = {'atr': 2.5, 'prev_high': 106.0, 'high20': 108.0,
             'prev_low': 98.0, 'low20': 95.0}
    plan = sc.trade_plan('LONG', 102.5, d, 101.3, (102, 100), daily,
                         'ORB BREAKOUT', sc.Config(min_rr=1.2))
    assert plan is not None
    assert plan['sl'] < plan['entry'] < plan['target']
    assert plan['net_rr'] < (plan['target']-plan['entry'])/(plan['entry']-plan['sl'])
    strict = sc.trade_plan('LONG', 102.5, d, 101.3, (102, 100), daily,
                           'ORB BREAKOUT', sc.Config(min_rr=5.0))
    assert strict is None


def test_unknown_market_rejects_live_signals():
    d = make_day(date(2026, 11, 11), [100]*20)
    assert sc.evaluate_live('TEST.NS', {'atr': 2}, d,
                            {'index_fresh': False, 'vix_restricted': False},
                            datetime(2026, 11, 11, 11, 0, tzinfo=sc.IST), sc.Config()) == []


def test_score_weights_are_exactly_bounded():
    daily = {'ema20': 110, 'ema50': 105, 'ema200': 100, 'slope20': .03, 'turnover_cr': 30}
    intraday = {'session_pct': 2.0, 'rvol': 2.4, 'vwap': 110,
                'rsi15': 57, 'bar_rvol': 2.5}
    market = {'market_direction': 'BULLISH', 'index_pct': 0.2}
    score, _, breakdown = sc.score_live('LONG', daily, 1.2, 110.5,
                                        intraday, market, 'ORB BREAKOUT')
    assert score == 100
    assert sum(breakdown.values()) == 100


def test_no_hardcoded_telegram_token():
    text = (Path(__file__).parents[1] / 'scanner.py').read_text(encoding='utf-8')
    assert 'TELEGRAM_BOT_TOKEN' in text and 'TELEGRAM_CHAT_ID' in text
    assert not __import__('re').search(r'\d{8,12}:AA[A-Za-z0-9_-]{20,}', text)


def test_confirm_breakout_requires_fresh_cross_and_volume():
    sessions = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
    old = [make_day(day, [100 + 0.02*i for i in range(30)], [1000]*30) for day in sessions]
    today = make_day(date(2026, 11, 11),
                     [100.0, 100.1, 100.0, 100.2, 100.1, 100.15,
                      100.15, 100.25, 100.3, 100.2, 100.7],
                     [2500]*10 + [4000])
    all_data = pd.concat(old+[today])
    orb = sc.opening_range(today, date(2026, 11, 11))
    assert orb is not None
    vwap = sc.session_vwap(today)
    e = sc.ema(all_data.Close, 9).loc[today.index]
    # This is an actual fresh close above opening range, not a mere proximity score.
    signal = sc.confirm_signal(today, all_data, 'LONG', orb, vwap, e, 56)
    assert signal == 'ORB BREAKOUT'
    later = today.copy()
    later.loc[later.index[-2], 'Close'] = 100.6  # already above trigger
    assert sc.confirm_signal(later, pd.concat(old+[later]), 'LONG', orb,
                             sc.session_vwap(later), e, 56) != 'ORB BREAKOUT'


def test_watchlist_report_write_roundtrip(monkeypatch, tmp_path):
    dates = pd.bdate_range(end='2026-11-11', periods=240, tz=sc.IST)
    closing = np.linspace(85, 105, len(dates)) + 0.8*np.sin(np.arange(len(dates))*1.5)
    d = pd.DataFrame({'Open': closing, 'High': closing+1, 'Low': closing-1,
                      'Close': closing, 'Volume': 500000}, index=dates)
    monkeypatch.setattr(sc, 'resolve_symbols', lambda _: ['EXAMPLE.NS'])
    monkeypatch.setattr(sc, 'download', lambda *_a, **_kw: {'EXAMPLE.NS': d})
    cfg = sc.Config(mode='watchlist', output_dir=str(tmp_path),
                    alert_db=str(tmp_path / 'log.db'))
    rows = sc.scan(cfg, datetime(2026, 11, 11, 18, 0, tzinfo=sc.IST))
    assert rows and rows[0]['status'] == 'WATCHLIST_ONLY'
    assert rows[0]['reference_close'] == pytest.approx(round(closing[-1], 2))
    assert list(tmp_path.glob('*.json'))
    assert list(tmp_path.glob('*.csv'))
    import json
    payload = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert payload['metadata']['mode'] == 'watchlist'
    assert payload['results'][0]['symbol'] == 'EXAMPLE'


def test_nse_parser_keeps_only_regular_eq_equities():
    contents = '\ufeffSYMBOL,NAME OF COMPANY,SERIES\nABC,ABC Limited,EQ\nDEF,DEF PLC,BE\nXYZ,XYZ Co,EQ\nABC,ABC Limited,EQ\n'
    assert sc.parse_nse_equity_symbols(contents) == ['ABC', 'XYZ']


def test_nse_universe_download_and_cache(monkeypatch, tmp_path):
    class Response:
        content = ('SYMBOL,SERIES\n' + ''.join(f'ITEM{i},EQ\n' for i in range(1200)) +
                   'EXCLUDED,BE\n').encode()
        def raise_for_status(self):
            pass

    class Session:
        def __init__(self):
            self.headers = {}
        def get(self, *_a, **_kw):
            return Response()

    monkeypatch.setattr(sc.requests, 'Session', Session)
    cfg = sc.Config(universe='nse', output_dir=str(tmp_path))
    symbols = sc.resolve_symbols(cfg)
    assert len(symbols) == 1200
    assert 'EXCLUDED.NS' not in symbols
    assert (tmp_path / 'nse_eq_symbols_cache.csv').exists()


def test_nse_universe_unavailable_does_not_silently_downgrade(monkeypatch, tmp_path):
    class Session:
        def __init__(self):
            self.headers = {}
        def get(self, *_a, **_kw):
            raise sc.requests.ConnectionError('offline')

    monkeypatch.setattr(sc.requests, 'Session', Session)
    with pytest.raises(RuntimeError, match='Full NSE EQ symbols unavailable'):
        sc.resolve_symbols(sc.Config(universe='nse', output_dir=str(tmp_path)))
