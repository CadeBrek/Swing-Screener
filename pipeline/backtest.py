"""Backtest of the screener's v1 rules over ~10 years (runs on GitHub Actions).

Two parts, both using the exact v1 scoring code in rules_v1.py:

1. Historical learning log: every Monday, score the universe and record each stock's inputs and its
   forward 4- and 13-week return vs the S&P 500 - the same table the weekly learning log builds live,
   so the monthly review's threshold questions (strength gate 60 vs 70/80, pullback cut 8/12/15%, does
   score order matter) can be answered now instead of in a year.

2. vectorbt simulation of the live paper engine's rules (pipeline/engine_sim.py), plus a grid of variants:
   - each Monday the finalists form a watchlist for the week; Actionable ones are bought when the close is
     in their buy zone (price - ATR .. price, +-1-2%); extended ones wait for a pullback to the 50-day
   - shares = 1% of equity at risk, max 20% of equity, max 10 names; stop k x ATR below entry, trailing to
     highest close - 3 x ATR; exits on stop, failing the weekly gates, 40 sessions not above entry, or 126
     sessions; idle cash in SPY (the live book uses VOO); 0.05% cost per fill
   Not modelled: waits around earnings (no free historical earnings calendar).

Known bias: the universe is today's S&P 500 members (top 100 each week by dollar volume), so companies
that shrank or left the index are missing. That flatters results; treat them as optimistic.

Writes data/backtest/ (summary.json, grid.csv, forward_study.json, equity.csv) and
reports/backtest_v1_tearsheet.html (quantstats, baseline rules vs SPY).
"""
import io, json, os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, DATA, dump, load, download, yahoo
import rules_v1 as rules

YEARS = 10
UNIVERSE_SIZE = 100
SECOND_CLASS = {"GOOG", "FOX", "NWS", "BRK-A", "BF-A", "UHAL-B"}
GATES, STOPS, EXT_CUTS = [50, 60, 70, 80], [2.0, 2.5, 3.0], [8, 12, 15]
BASE = (rules.RS_GATE, rules.STOP_ATR, rules.EXT_CUT)
t0 = time.time()


def log(msg):
    print(f"[{time.time() - t0:6.0f}s] {msg}", flush=True)


def sp500():
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers={"User-Agent": "Mozilla/5.0 (swing-screener)"}, timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    return [s for s in (x.replace(".", "-").upper() for x in t["Symbol"]) if s not in SECOND_CLASS]


def sectors(symbols):
    meta = load("meta.json", default={})
    need = [s for s in symbols if s not in meta]

    def one(s):
        try:
            i = yf.Ticker(yahoo(s)).info
            return s, {"sector": i.get("sector"), "industry": i.get("industry")}
        except Exception:
            return s, {}
    with ThreadPoolExecutor(8) as ex:
        for s, m in ex.map(one, need):
            if m.get("sector"):
                meta[s] = m
    dump(meta, "meta.json")
    return {s: meta.get(s, {}).get("sector") for s in symbols}


def months_back_rows(index, months):
    """For each date, the row of the last date on or before (date - months); -1 if none."""
    targets = index - pd.DateOffset(months=months)
    return index.searchsorted(targets, side="right") - 1


def main():
    syms = sp500()
    log(f"S&P 500 list: {len(syms)}")
    bars = download(set(syms) | {"SPY"}, period=f"{YEARS + 2}y")
    syms = [s for s in syms if s in bars]
    log(f"price history for {len(syms)} stocks")
    idx = bars["SPY"].index
    C = pd.DataFrame({s: bars[s]["Close"] for s in syms}).reindex(idx)
    H = pd.DataFrame({s: bars[s]["High"] for s in syms}).reindex(idx)
    L = pd.DataFrame({s: bars[s]["Low"] for s in syms}).reindex(idx)
    V = pd.DataFrame({s: bars[s]["Volume"] for s in syms}).reindex(idx)
    spyc = bars["SPY"]["Close"]
    sec = sectors(syms)

    # ---- features on every day, vectorized (same definitions as the live screen) ----------------
    Cf = C.ffill()
    nbars = C.notna().cumsum()
    ma50 = C.rolling(50, min_periods=1).mean()
    ma200 = C.rolling(200, min_periods=1).mean()
    hi52 = H.rolling(252, min_periods=1).max()
    avgvol = V.rolling(63, min_periods=1).mean()
    dollar = (C * V).rolling(63, min_periods=20).mean()
    pc = C.shift(1)
    tr = pd.concat([H - L, (H - pc).abs(), (L - pc).abs()]).groupby(level=0).max().reindex(idx)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rets = {}
    for m in (1, 3, 6, 12):
        rows = months_back_rows(idx, m)
        base = Cf.values[np.clip(rows, 0, None)]
        base[rows < 0] = np.nan
        rets[m] = pd.DataFrame((Cf.values / base - 1) * 100, index=idx, columns=syms)
        sb = spyc.values[np.clip(rows, 0, None)].astype(float); sb[rows < 0] = np.nan
        rets[("SPY", m)] = pd.Series((spyc.values / sb - 1) * 100, index=idx)

    # ---- Mondays (first trading day of each week) over the test window --------------------------
    start = idx[0] + pd.DateOffset(years=1, months=1)
    wk = pd.Series(idx, index=idx)
    weekly = wk[wk.index >= start].groupby(wk[wk.index >= start].dt.to_period("W")).first()
    dates = list(pd.DatetimeIndex(weekly.values))
    log(f"{len(dates)} weekly screens from {dates[0].date()} to {dates[-1].date()}")

    records = []      # historical learning log
    by_date = {}      # date -> scored frame (for the portfolio sims)
    for d in dates:
        ok = (nbars.loc[d] >= 126) & (C.loc[d] > rules_min_price()) & (avgvol.loc[d] > 1_000_000) & C.loc[d].notna()
        uni = dollar.loc[d][ok].dropna().sort_values(ascending=False).index[:UNIVERSE_SIZE]
        df = pd.DataFrame({"symbol": uni, "price": C.loc[d, uni].values, "hi52": hi52.loc[d, uni].values,
                           "ma50": ma50.loc[d, uni].values, "ma200": ma200.loc[d, uni].values,
                           "r1m": rets[1].loc[d, uni].values, "r3m": rets[3].loc[d, uni].values,
                           "r6m": rets[6].loc[d, uni].values, "r1y": rets[12].loc[d, uni].values,
                           "ysector": [sec.get(s) for s in uni]})
        spy = {m: rets[("SPY", m)].loc[d] for m in (1, 3, 6, 12)}
        sc = rules.score(df, spy)
        sc["pass_rank"] = np.nan
        sc.loc[sc.passes, "pass_rank"] = np.arange(1, int(sc.passes.sum()) + 1)
        fin = {r.symbol: i for i, r in enumerate(rules.pick_finalists(sc), 1)}
        sc["finalist_rank"] = sc.symbol.map(fin)
        sc["atr"] = atr.loc[d, sc.symbol].values
        by_date[d] = sc
        for r in sc.itertuples():
            records.append(dict(date=d, symbol=r.symbol, group=r.group, passes=bool(r.passes), rs_pct=int(r.rs_pct),
                                trend_gates=bool(r.price > r.ma200 and r.ma50 > r.ma200),
                                trend=int(r.trend), ext_50=r.ext_50, off_high=r.off_high, score=r.final,
                                pass_rank=r.pass_rank, finalist_rank=r.finalist_rank, price=r.price))
    log("weekly screens scored")

    # forward returns vs SPY, 4 and 13 weeks later (from the screen dates, like the live log)
    hist = pd.DataFrame(records)
    pos = {d: i for i, d in enumerate(dates)}
    for h in (4, 13):
        fut = {d: dates[pos[d] + h] for d in dates if pos[d] + h < len(dates)}
        hist[f"fdate_{h}"] = hist.date.map(fut)
        m = hist[f"fdate_{h}"].notna()
        fp = Cf.stack().rename("p")
        key = list(zip(hist.loc[m, f"fdate_{h}"], hist.loc[m, "symbol"]))
        later = fp.reindex(pd.MultiIndex.from_tuples(key)).values
        hist.loc[m, f"ret_{h}w"] = (later / hist.loc[m, "price"].values - 1) * 100
        hist.loc[m, f"spy_{h}w"] = (spyc.reindex(hist.loc[m, f"fdate_{h}"]).values /
                                    spyc.reindex(hist.loc[m, "date"]).values - 1) * 100
        hist[f"ex_{h}w"] = hist[f"ret_{h}w"] - hist[f"spy_{h}w"]
    study = forward_study(hist)
    log("forward study done")

    # ---- vectorbt: baseline + grid -----------------------------------------------------------
    import vectorbt as vbt
    ever = sorted({s for sc in by_date.values() for s in sc.symbol})
    C = C[ever].assign(SPY=spyc)
    atr = atr[ever].assign(SPY=np.nan)
    grid, base_pf = [], None
    for g in GATES:
        for k in STOPS:
            for cut in EXT_CUTS:
                pf = simulate(vbt, C, atr, by_date, dates, g, k, cut)
                st = pf_stats(pf)
                grid.append({"rs_gate": g, "stop_atr": k, "pullback_cut": cut, **st})
                if (g, k, cut) == BASE:
                    base_pf = pf
                log(f"gate {g} stop {k} cut {cut}: {st['cagr_pct']}%/yr, maxDD {st['max_drawdown_pct']}%, trades {st['trades']}")
    grid = pd.DataFrame(grid)
    test = (C.index >= dates[0])
    spy_ret = spyc[test].pct_change().dropna()
    spy_stats = {"total_return_pct": round((spyc[test].iloc[-1] / spyc[test].iloc[0] - 1) * 100, 1),
                 "cagr_pct": round(((spyc[test].iloc[-1] / spyc[test].iloc[0]) ** (252 / test.sum()) - 1) * 100, 1),
                 "max_drawdown_pct": round(((spyc[test] / spyc[test].cummax()) - 1).min() * 100, 1),
                 "sharpe": round(float(spy_ret.mean() / spy_ret.std() * np.sqrt(252)), 2)}

    os.makedirs(os.path.join(DATA, "backtest"), exist_ok=True)
    grid.to_csv(os.path.join(DATA, "backtest", "grid.csv"), index=False)
    eq = base_pf.value()
    pd.DataFrame({"strategy": eq, "spy": spyc[test] / spyc[test].iloc[0] * eq.iloc[0]}, index=eq.index).to_csv(
        os.path.join(DATA, "backtest", "equity.csv"))
    hist.drop(columns=[c for c in hist if c.startswith("fdate")]).to_csv(
        os.path.join(DATA, "backtest", "screen_history.csv.gz"), index=False, compression="gzip")
    dump(study, "backtest", "forward_study.json")
    summary = {"run": str(dt.date.today()), "window": [str(dates[0].date()), str(dates[-1].date())],
               "weeks": len(dates), "universe": "today's S&P 500 members, top 100 by 63-day dollar volume each week",
               "rules_version": rules.RULES_VERSION, "baseline": dict(zip(["rs_gate", "stop_atr", "pullback_cut"], BASE)),
               "baseline_stats": grid[(grid.rs_gate == BASE[0]) & (grid.stop_atr == BASE[1]) &
                                      (grid.pullback_cut == BASE[2])].iloc[0].to_dict(),
               "spy_stats": spy_stats,
               "best_by_sharpe": grid.sort_values("sharpe", ascending=False).head(5).to_dict("records"),
               "caveats": ["survivorship bias: today's index members only, so results are optimistic",
                           "simplified trading plan: no earnings waits, pullback-zone entries or time stops; idle cash earns 0",
                           "36 rule variants were tried: the best one is partly luck (data snooping); prefer variants that "
                           "win across neighbouring settings and in the live learning log"]}
    dump(summary, "backtest", "summary.json")

    import quantstats as qs
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    r = base_pf.returns(); r.name = "Screener v1 (backtest)"
    qs.reports.html(r, benchmark=spy_ret.rename("S&P 500"), output=os.path.join(ROOT, "reports", "backtest_v1_tearsheet.html"),
                    title="Swing screener v1 rules: 10-year backtest vs S&P 500 (simplified, optimistic)",
                    download_filename="backtest_v1_tearsheet.html")
    log("done")
    print(json.dumps({k: summary[k] for k in ("window", "baseline_stats", "spy_stats")}, indent=1, default=str))


def rules_min_price():
    return 10


def simulate(vbt, C, atr, by_date, dates, gate, k, cut):
    """Engine-style signals for one rule variant, then the engine replica in engine_sim."""
    import engine_sim
    test = C.index >= dates[0]
    Ct = C[test]
    entry = np.zeros(Ct.shape, dtype=bool)
    exit_ = np.zeros(Ct.shape, dtype=bool)
    atr_ref = np.full(Ct.shape, np.nan)
    col = {s: j for j, s in enumerate(Ct.columns)}
    rowpos = {d: i for i, d in enumerate(Ct.index)}
    vals = Ct.values
    for w, d in enumerate(dates):
        i0 = rowpos[d]
        i1 = rowpos[dates[w + 1]] if w + 1 < len(dates) else len(Ct)
        sc = by_date[d]
        passes = (sc.price > sc.ma200) & (sc.ma50 > sc.ma200) & (sc.rs_pct >= gate)
        sc2 = sc.assign(passes=passes)
        for r in sc2[~sc2.passes].itertuples():
            exit_[i0, col[r.symbol]] = True        # fails this week's gates: sell if held
        for r in rules.pick_finalists(sc2):
            j = col[r.symbol]; a = r.atr
            if not a or np.isnan(a):
                continue
            if r.ext_50 > cut:                        # wait for a pullback toward the 50-day
                lo, hi = r.ma50 * 0.97, r.ma50 * 1.05
            else:                                     # actionable: buy zone price - ATR .. price
                lo, hi = (r.price - a) * 0.99, r.price * 1.02
            win = vals[i0:i1, j]
            ok = np.where((win >= lo) & (win <= hi))[0]
            if len(ok):
                entry[i0 + ok[0], j] = True; atr_ref[i0 + ok[0], j] = a
    E = pd.DataFrame(entry, index=Ct.index, columns=Ct.columns)
    X = pd.DataFrame(exit_, index=Ct.index, columns=Ct.columns)
    A = pd.DataFrame(atr_ref, index=Ct.index, columns=Ct.columns)
    return engine_sim.run(Ct, E, X, A, spy="SPY", k_stop=k)


def pf_stats(pf):
    v = pf.value()
    yrs = len(v) / 252
    rr = pf.returns()
    tr = pf.trades.records_readable
    tr = tr[tr["Column"] != "SPY"]
    held = pf.assets().drop(columns="SPY") if "SPY" in pf.assets() else pf.assets()
    return {"total_return_pct": round((v.iloc[-1] / v.iloc[0] - 1) * 100, 1),
            "cagr_pct": round(((v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1) * 100, 1),
            "max_drawdown_pct": round(float(((v / v.cummax()) - 1).min()) * 100, 1),
            "sharpe": round(float(rr.mean() / rr.std() * np.sqrt(252)), 2) if rr.std() > 0 else None,
            "trades": int(len(tr)), "win_rate_pct": round(float((tr["Return"] > 0).mean()) * 100, 1) if len(tr) else None,
            "avg_trade_pct": round(float(tr["Return"].mean()) * 100, 2) if len(tr) else None,
            "avg_names_held": round(float((held > 0).sum(axis=1).mean()), 1)}


def forward_study(h):
    """The monthly review's comparisons, on 10 years of weekly screens."""
    out = {}

    def g(mask, label, hz):
        x = h[mask & h[f"ex_{hz}w"].notna()]
        return {"label": label, "n": int(len(x)), "weeks": int(x.date.nunique()), "stocks": int(x.symbol.nunique()),
                "avg_excess_pct": round(float(x[f"ex_{hz}w"].mean()), 2) if len(x) else None,
                "median_excess_pct": round(float(x[f"ex_{hz}w"].median()), 2) if len(x) else None,
                "beat_spy_pct": round(float((x[f"ex_{hz}w"] > 0).mean() * 100), 1) if len(x) else None}
    fin = h.finalist_rank.notna()
    for hz in (4, 13):
        rows = [g(fin, "Finalists", hz), g(h.passes & ~fin, "Passed, not picked", hz), g(~h.passes, "Failed the gates", hz)]
        for lo, hi in ((60, 69), (70, 79), (80, 89), (90, 100)):
            rows.append(g(h.passes & h.rs_pct.between(lo, hi), f"Passers, strength {lo}-{hi}", hz))
        rows.append(g(h.trend_gates & h.rs_pct.between(50, 59), "Strength 50-59 (would pass only at gate 50)", hz))
        for lab, m in (("ext < 5%", h.ext_50 < 5), ("ext 5-8%", h.ext_50.between(5, 8)), ("ext 8-12%", h.ext_50.between(8, 12)),
                       ("ext 12-15%", h.ext_50.between(12, 15)), ("ext > 15%", h.ext_50 > 15)):
            rows.append(g(h.passes & m, f"Passers, {lab} above 50-day", hz))
        rows += [g(h.finalist_rank.between(1, 4), "Finalists ranked 1-4", hz),
                 g(h.finalist_rank.between(5, 8), "Finalists ranked 5-8", hz),
                 g(h.pass_rank.between(1, 8), "Passers ranked 1-8 by score", hz),
                 g(h.pass_rank.between(9, 16), "Passers ranked 9-16 by score", hz)]
        out[f"{hz}w"] = rows
    out["note"] = ("Each row is stock-weeks; weekly rows of the same stock overlap, so the effective sample is smaller "
                   "than n. Survivorship bias lifts every row about equally, so compare rows with each other rather "
                   "than reading any one row as an expected return.")
    return out


if __name__ == "__main__":
    main()
