"""The screener's v1 rules in one place, shared by the live weekly screen and the backtest.

Changing anything here is a rules change: bump RULES_VERSION and add a rules.json changelog entry.
"""
import pandas as pd

RULES_VERSION = "v1"
RS_GATE = 60          # pass only if strength rank >= 60
EXT_CUT = 12          # "Wait for pullback" if more than 12% above the 50-day
STOP_ATR = 2.5        # initial stop = entry - 2.5 x ATR
TOP_N, PER_GROUP = 8, 2

GROUP_OF_SECTOR = {"Energy": "Energy", "Healthcare": "Healthcare", "Technology": "Tech",
                   "Financial Services": "Financials", "Consumer Defensive": "Staples", "Industrials": "Industrials",
                   "Consumer Cyclical": "Consumer", "Basic Materials": "Materials", "Utilities": "Utilities",
                   "Real Estate": "Real estate", "Communication Services": "Communication"}


def skip1(long, r1m):
    """Return over the long window, skipping the most recent month."""
    return ((1 + long / 100) / (1 + r1m / 100) - 1) * 100


def pct_rank(series):
    """Share of values <= v, x100 (ties share the higher rank)."""
    vals = series.tolist()
    return series.apply(lambda v: sum(1 for x in vals if x <= v) / len(vals) * 100)


def score(df, spy, rs_gate=RS_GATE):
    """df columns: symbol, price, hi52, ma50, ma200, r1m, r3m, r6m, r1y (+ ysector).
    spy: {1: r1m, 3: r3m, 6: r6m, 12: r1y} for SPY. Returns a new frame sorted by final score."""
    df = df.dropna(subset=["r1m", "r3m", "r6m", "r1y", "ma50", "ma200"]).reset_index(drop=True).copy()
    df["rs3"] = df.apply(lambda r: skip1(r.r3m, r.r1m), axis=1) - skip1(spy[3], spy[1])
    df["rs6"] = df.apply(lambda r: skip1(r.r6m, r.r1m), axis=1) - skip1(spy[6], spy[1])
    df["rs12"] = df["r1y"] - spy[12]
    df["rs_score"] = 0.4 * pct_rank(df["rs6"]) + 0.4 * pct_rank(df["rs3"]) + 0.2 * pct_rank(df["rs12"])
    df["rs_pct"] = pct_rank(df["rs_score"]).round().astype(int)
    df["trend"] = ((df.price > df.ma200).astype(int) + (df.ma50 > df.ma200).astype(int) +
                   (df.price > df.ma50).astype(int) + (df.price >= 0.85 * df.hi52).astype(int))
    df["off_high"] = (df.price / df.hi52 - 1) * 100
    df["ext_50"] = (df.price / df.ma50 - 1) * 100

    def why(r):
        w = []
        if not r.price > r.ma200: w.append("below 200-day")
        if not r.ma50 > r.ma200: w.append("50-day below 200-day")
        if not r.rs_pct >= rs_gate: w.append(f"RS rank < {rs_gate}")
        return ", ".join(w)
    df["why_not"] = df.apply(why, axis=1)
    df["passes"] = df["why_not"] == ""
    df["final"] = 0.5 * df.rs_pct + 0.35 * (df.trend / 4 * 100) + 0.15 * (100 + df.off_high * 4).clip(lower=0)
    if "ysector" in df:
        df["group"] = df["ysector"].map(GROUP_OF_SECTOR).fillna("Other")
    return df.sort_values("final", ascending=False).reset_index(drop=True)


def pick_finalists(df, n=TOP_N, per_group=PER_GROUP):
    """Top n passing stocks by final score, at most per_group from one group. Returns row tuples."""
    picks, count = [], {}
    for r in df[df.passes].itertuples():
        if count.get(r.group, 0) >= per_group:
            continue
        picks.append(r); count[r.group] = count.get(r.group, 0) + 1
        if len(picks) == n:
            break
    return picks
