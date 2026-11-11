"""Shared helpers for the free-data pipeline (runs on GitHub Actions).

Data sources: Yahoo Finance via yfinance (prices, earnings, targets) and SEC EDGAR (Form 4).
"""
import json, os, time, datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def today_et():
    return dt.datetime.now(ET).date()


def path(*parts):
    p = os.path.join(DATA, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def dump(obj, *parts):
    with open(path(*parts), "w") as f:
        json.dump(obj, f, indent=1, default=str)


def load(*parts, default=None):
    p = os.path.join(DATA, *parts)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)


def read_list(*parts):
    p = os.path.join(DATA, *parts)
    if not os.path.exists(p):
        return []
    return [l.strip().upper() for l in open(p) if l.strip() and not l.startswith("#")]


def yahoo(sym):
    """Our symbols use '-' for share classes (BRK-B), which is what Yahoo expects too."""
    return sym.replace(".", "-")


def download(symbols, period="2y", tries=3, adj=False):
    """Daily OHLCV for many symbols. Returns {SYM: DataFrame[Open, High, Low, Close, Volume]}, unadjusted
    (Close is split-adjusted but excludes dividends). adj=True adds "Adj Close" (dividends reinvested)."""
    symbols = sorted({yahoo(s) for s in symbols})
    out = {}
    for chunk_start in range(0, len(symbols), 100):
        chunk = symbols[chunk_start:chunk_start + 100]
        for attempt in range(tries):
            try:
                df = yf.download(chunk, period=period, interval="1d", auto_adjust=False,
                                 group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # network hiccup
                if attempt == tries - 1:
                    raise
                time.sleep(5 * (attempt + 1))
        for s in chunk:
            try:
                d = df[s] if isinstance(df.columns, pd.MultiIndex) else df
                cols = ["Open", "High", "Low", "Close", "Volume"] + (["Adj Close"] if adj and "Adj Close" in d else [])
                d = d[cols].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(d):
                d.index = pd.to_datetime(d.index).tz_localize(None)
                out[s] = d
    return out


def ret_since(close, months, asof=None):
    """% change from the last close on or before (asof - months) to the last close."""
    asof = asof or close.index[-1]
    start = asof - pd.DateOffset(months=months)
    past = close[close.index <= start]
    if past.empty:
        return None
    return round((close.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


def wilder_atr(d, n=14):
    """14-day Average True Range with Wilder smoothing (same method as Alpha Vantage's ATR)."""
    h, l, c = d["High"], d["Low"], d["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).iloc[1:]
    if len(tr) < n:
        return None
    atr = tr.iloc[:n].mean()
    for v in tr.iloc[n:]:
        atr = (atr * (n - 1) + v) / n
    return round(float(atr), 2)


def earnings_info(sym):
    """Yahoo's earnings calendar for one symbol.

    Returns (events, last_surprise_pct): events = [(utc_iso, et_date), ...] oldest first;
    last_surprise_pct = EPS surprise % of the most recent reported quarter (or None).
    Returns (None, None) if the lookup failed, so callers can tell 'failed' from 'no events'.
    """
    try:
        ed = yf.Ticker(yahoo(sym)).get_earnings_dates(limit=12)
    except Exception:
        return None, None
    if ed is None or ed.empty:
        return None, None
    events, surprise = [], None
    for ts, row in ed.sort_index().iterrows():
        when = pd.Timestamp(ts)
        if when.tzinfo is None:
            when = when.tz_localize(ET)
        events.append((when.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), when.tz_convert(ET).date()))
        if not pd.isna(row.get("Reported EPS")) and not pd.isna(row.get("Surprise(%)")):
            surprise = round(float(row["Surprise(%)"]), 1)
    return events, surprise


def first_event_on_or_after(events, day):
    for utc, d in events or []:
        if d >= day:
            return utc, d
    return None, None
