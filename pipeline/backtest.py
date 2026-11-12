"""Monthly backtest of the live rules (v2) on GitHub Actions.

Same honest setup as the research run (pipeline/research.py): dividend-adjusted prices, point-in-time
S&P 500 membership (top 100 by dollar volume each Monday), 0.05% cost per fill, and the vectorbt replica
of the paper engine (pipeline/engine_sim.py). Data comes from RESEARCH_DATA (an export folder made by
export_data.py in the same job).

What it reports
  * v2 swing book alone, and the v2 BOOK AS IT RUNS: a 30% swing sleeve + 70% S&P, rebalanced monthly
    (the live engine caps new buys at the sleeve instead of rebalancing, so live drift will differ a bit)
  * v1 as it ran, and the S&P 500, for comparison
  * a 36-variant neighbourhood around v2 (gate 70/80/90, finalists 4/8/12, pullback rule on/off,
    no stop vs wide stop) - is v2 still in a good neighbourhood as new data arrives?
  * the historical learning log with v2 definitions (passers at gate 80, strength bands, ranks)
  * windows: full period, 2022 on (v2's out-of-sample test period), last 12 months
Caveats: about 15% of past index members have no Yahoo history (mostly acquired), so results are
somewhat optimistic; costs are a flat 0.05% per fill.
Writes data/backtest/ (summary.json, grid.csv, forward_study.json, equity.csv, screen_history.csv.gz)
and reports/backtest_v2_tearsheet.html (quantstats: the 30/70 book vs the S&P 500).
"""
import itertools, json, os, sys, time
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, DATA, dump
import rules_v2 as rules
import research
from research import load_inputs, members_on, metrics
from backtest_v1 import months_back_rows

t0 = time.time()
SLEEVE = rules.SLEEVE
V2 = {"gate": 80, "top_n": 8, "cap": 99, "pullback": True, "stop": "none"}
V1 = {"gate": 60, "top_n": 8, "cap": 2, "pullback": True, "stop": "atr 2.5/3 (v1)"}
GRID = {"gate": [70, 80, 90], "top_n": [4, 8, 12], "pullback": [True, False], "stop": ["none", "wide 4/4.5"]}
STOPS = {"atr 2.5/3 (v1)": (2.5, 3.0), "wide 4/4.5": (4.0, 4.5), "none": (np.inf, np.inf)}
OOS_START = pd.Timestamp("2022-01-01")


def log(m):
    print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)


def blend(book, spy_v, w=SLEEVE):
    """w in the swing book, 1-w in SPY, rebalanced at each month start."""
    rb, rs = book.pct_change().fillna(0), spy_v.pct_change().fillna(0)
    out = []
    for _, g in rb.groupby(rb.index.to_period("M")):
        cb = (1 + g).cumprod(); cs = (1 + rs.loc[g.index]).cumprod()
        val = w * cb + (1 - w) * cs
        prev = np.r_[1.0, val.values[:-1]]
        out.append(pd.Series(val.values / prev - 1, index=g.index))
    r = pd.concat(out)
    return (1 + r).cumprod() * book.iloc[0]


def win(v, spy_v, a, b=None):
    vv = v[(v.index >= a) & (v.index <= (b if b is not None else v.index[-1]))]
    return metrics(vv, spy_v) if len(vv) > 20 else None


def main():
    mdates, msets, msrc, C, H, L, V, spy, bil, sec, n_ever = load_inputs()
    stocks = list(C.columns); idx = C.index
    log(f"{msrc}; {len(stocks)} stocks with prices; dividend-adjusted: {research.ADJ[0]}")
    Cf = C.ffill(); nbars = C.notna().cumsum()
    ma50 = C.rolling(50, min_periods=1).mean(); ma200 = C.rolling(200, min_periods=1).mean()
    hi52 = H.rolling(252, min_periods=1).max(); avgvol = V.rolling(63, min_periods=1).mean()
    dollar = (C * V).rolling(63, min_periods=20).mean()
    pc = C.shift(1)
    tr = np.fmax(np.fmax((H - L).values, (H - pc).abs().values), (L - pc).abs().values)
    atr = pd.DataFrame(tr, index=idx, columns=stocks).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rets, spyr = {}, {}
    for m in (1, 3, 6, 12):
        rows = months_back_rows(idx, m)
        base = Cf.values[np.clip(rows, 0, None)]; base[rows < 0] = np.nan
        rets[m] = pd.DataFrame((Cf.values / base - 1) * 100, index=idx, columns=stocks)
        sb = spy.values[np.clip(rows, 0, None)].astype(float); sb[rows < 0] = np.nan
        spyr[m] = pd.Series((spy.values / sb - 1) * 100, index=idx)

    start = idx[0] + pd.DateOffset(years=1, months=1)
    wk = pd.Series(idx, index=idx); wk = wk[wk.index >= start]
    weeks = list(pd.DatetimeIndex(wk.groupby(wk.dt.to_period("W")).first().values))
    F = {}
    for d in weeks:
        mem = members_on(d, mdates, msets)
        cols = [s for s in stocks if s in mem]
        ok = (nbars.loc[d, cols] >= 126) & (C.loc[d, cols] > 10) & (avgvol.loc[d, cols] > 1_000_000) & C.loc[d, cols].notna()
        u = dollar.loc[d, cols][ok].dropna().sort_values(ascending=False).index[:100]
        df = pd.DataFrame({"symbol": u, "price": C.loc[d, u].values, "hi52": hi52.loc[d, u].values,
                           "ma50": ma50.loc[d, u].values, "ma200": ma200.loc[d, u].values,
                           "r1m": rets[1].loc[d, u].values, "r3m": rets[3].loc[d, u].values,
                           "r6m": rets[6].loc[d, u].values, "r1y": rets[12].loc[d, u].values,
                           "ysector": [sec.get(s) for s in u]})
        sc = rules.score(df, {m: spyr[m].loc[d] for m in (1, 3, 6, 12)})
        sc["atr"] = atr.loc[d, sc.symbol].values
        F[d] = sc
    log(f"{len(weeks)} weekly screens scored ({weeks[0].date()} .. {weeks[-1].date()})")

    import engine_sim
    test = idx >= weeks[0]
    Ct = C[test].copy(); Ct["SPY"] = spy[test]; Ct["BIL"] = bil[test]
    col = {s: j for j, s in enumerate(Ct.columns)}
    rowpos = {d: i for i, d in enumerate(Ct.index)}
    vals = Ct.values
    spy_v = spy[test] / spy[test].iloc[0] * 50_000

    def run(gate, top_n, cap, pullback, stop):
        entry = np.zeros(Ct.shape, dtype=bool); exit_ = np.zeros(Ct.shape, dtype=bool)
        atr_ref = np.full(Ct.shape, np.nan)
        for w, d in enumerate(weeks):
            i0 = rowpos[d]; i1 = rowpos[weeks[w + 1]] if w + 1 < len(weeks) else len(Ct)
            sc = F[d]
            sc2 = sc.assign(passes=(sc.price > sc.ma200) & (sc.ma50 > sc.ma200) & (sc.rs_pct >= gate))
            for r in sc2[~sc2.passes].itertuples():
                exit_[i0, col[r.symbol]] = True
            for r in rules.pick_finalists(sc2, n=top_n, per_group=cap):
                j = col[r.symbol]; a = r.atr
                if not a or np.isnan(a):
                    continue
                lo, hi = ((r.ma50 * 0.97, r.ma50 * 1.05) if pullback and r.ext_50 > rules.EXT_CUT
                          else ((r.price - a) * 0.99, r.price * 1.02))
                ok = np.where((vals[i0:i1, j] >= lo) & (vals[i0:i1, j] <= hi))[0]
                if len(ok):
                    entry[i0 + ok[0], j] = True; atr_ref[i0 + ok[0], j] = a
        k, kt = STOPS[stop]
        pf = engine_sim.run(Ct, pd.DataFrame(entry, index=Ct.index, columns=Ct.columns),
                            pd.DataFrame(exit_, index=Ct.index, columns=Ct.columns),
                            pd.DataFrame(atr_ref, index=Ct.index, columns=Ct.columns),
                            spy="SPY", bil="BIL", k_stop=k, k_trail=kt, size_k=2.5, max_pos=max(10, top_n))
        tr = pf.trades.records_readable
        tr = tr[~tr["Column"].isin(["SPY", "BIL"])]
        return pf.value(), {"trades": int(len(tr)),
                            "win_rate_pct": round(float((tr["Return"] > 0).mean()) * 100, 1) if len(tr) else None,
                            "avg_trade_pct": round(float(tr["Return"].mean()) * 100, 2) if len(tr) else None}

    last12 = idx[-1] - pd.DateOffset(years=1)
    def windows3(v):
        return {"full": win(v, spy_v, weeks[0]), "since_2022": win(v, spy_v, OOS_START), "last_12m": win(v, spy_v, last12)}

    v2_book, v2_trades = run(cap=99, **{k: V2[k] for k in ("gate", "top_n", "pullback", "stop")})
    v1_book, v1_trades = run(**V1)
    v2_live = blend(v2_book, spy_v)
    log("v2 and v1 run")

    grid = []
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(GRID, combo))
        v, t = (v2_book, v2_trades) if p == {k: V2[k] for k in GRID} else run(cap=99, **p)
        b = blend(v, spy_v)
        m_book, m_blend = windows3(v), windows3(b)
        grid.append({**p, "is_v2": p == {k: V2[k] for k in GRID}, **t,
                     **{f"book_{w}_{k}": x for w in m_book for k, x in (m_book[w] or {}).items()
                        if k in ("cagr_pct", "sharpe", "max_dd_pct")},
                     **{f"blend_{w}_{k}": x for w in m_blend for k, x in (m_blend[w] or {}).items()
                        if k in ("cagr_pct", "sharpe", "max_dd_pct")}})
    grid = pd.DataFrame(grid)
    log(f"{len(grid)}-variant neighbourhood run")

    # learning log with v2 definitions
    rec = []
    for d in weeks:
        sc = F[d]
        fin = {r.symbol: i for i, r in enumerate(rules.pick_finalists(sc), 1)}
        prank = {s: i for i, s in enumerate(sc[sc.passes].symbol, 1)}
        for r in sc.itertuples():
            rec.append(dict(date=d, symbol=r.symbol, passes=bool(r.passes), rs_pct=int(r.rs_pct),
                            trend_gates=bool(r.price > r.ma200 and r.ma50 > r.ma200), ext_50=r.ext_50,
                            pass_rank=prank.get(r.symbol), finalist_rank=fin.get(r.symbol), price=r.price))
    h = pd.DataFrame(rec)
    pos = {d: i for i, d in enumerate(weeks)}
    for hz in (4, 13):
        fut = {d: weeks[pos[d] + hz] for d in weeks if pos[d] + hz < len(weeks)}
        fd = h.date.map(fut); m = fd.notna()
        later = Cf.stack().reindex(pd.MultiIndex.from_arrays([fd[m], h.symbol[m]])).values
        h.loc[m, f"ex_{hz}w"] = ((later / h.loc[m, "price"].values - 1) -
                                 (spy.reindex(fd[m]).values / spy.reindex(h.date[m]).values - 1)) * 100

    def g(mask, label, hz):
        x = h[mask & h[f"ex_{hz}w"].notna()]
        return {"label": label, "n": int(len(x)), "weeks": int(x.date.nunique()),
                "avg_excess_pct": round(float(x[f"ex_{hz}w"].mean()), 2) if len(x) else None,
                "beat_spy_pct": round(float((x[f"ex_{hz}w"] > 0).mean() * 100), 1) if len(x) else None}
    fin = h.finalist_rank.notna()
    study = {}
    for hz in (4, 13):
        rows = [g(fin, "v2 finalists", hz), g(h.passes & ~fin, "Passed v2 gates, not picked", hz),
                g(h.trend_gates & h.rs_pct.between(60, 79), "Strength 60-79 (passed v1, not v2)", hz),
                g(~h.trend_gates, "Failed the trend gates", hz)]
        for lo, hi in ((80, 89), (90, 100)):
            rows.append(g(h.passes & h.rs_pct.between(lo, hi), f"Passers, strength {lo}-{hi}", hz))
        for lab, m in (("< 5%", h.ext_50 < 5), ("5-12%", h.ext_50.between(5, 12)), ("> 12%", h.ext_50 > 12)):
            rows.append(g(h.passes & m, f"Passers {lab} above the 50-day", hz))
        rows += [g(h.finalist_rank.between(1, 4), "Finalists ranked 1-4", hz),
                 g(h.finalist_rank.between(5, 8), "Finalists ranked 5-8", hz)]
        study[f"{hz}w"] = rows
    study["note"] = "stock-weeks overlap, so effective samples are smaller than n; compare rows with each other"

    out = os.path.join(DATA, "backtest")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):              # drop the old v1 outputs
        os.remove(os.path.join(out, f))
    grid.to_csv(os.path.join(out, "grid.csv"), index=False)
    pd.DataFrame({"v2_book_30_70": v2_live, "v2_swing_alone": v2_book, "v1": v1_book, "spy": spy_v}).to_csv(
        os.path.join(out, "equity.csv"))
    h.to_csv(os.path.join(out, "screen_history.csv.gz"), index=False, compression="gzip")
    dump(study, "backtest", "forward_study.json")
    v2_rank = grid.sort_values("blend_full_sharpe", ascending=False).reset_index(drop=True)
    summary = {"run": str(pd.Timestamp.today().date()), "rules_version": "v2", "window": [str(weeks[0].date()), str(idx[-1].date())],
               "data": {"membership": msrc, "dividend_adjusted": research.ADJ[0], "stocks_with_prices": len(stocks)},
               "v2_book_30_70": windows3(v2_live), "v2_swing_alone": {**windows3(v2_book), **v2_trades},
               "v1": {**windows3(v1_book), **v1_trades}, "spy": windows3(spy_v),
               "neighbourhood": {"variants": len(grid),
                                 "v2_rank_by_full_blend_sharpe": int(v2_rank.index[v2_rank.is_v2][0]) + 1,
                                 "share_of_variants_whose_30_70_beats_spy_full_sharpe_pct": round(
                                     float((grid.blend_full_sharpe > windows3(spy_v)["full"]["sharpe"]).mean()) * 100, 1),
                                 "median_blend_full_sharpe": round(float(grid.blend_full_sharpe.median()), 3)},
               "caveats": ["about 15% of past index members lack Yahoo history (mostly acquired): optimistic",
                           "30/70 is rebalanced monthly here; the live engine caps new buys at the sleeve instead",
                           "no waits around earnings (no free historical earnings calendar)"]}
    dump(summary, "backtest", "summary.json")

    import quantstats as qs
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    old = os.path.join(ROOT, "reports", "backtest_v1_tearsheet.html")
    if os.path.exists(old):
        os.remove(old)
    r = v2_live.pct_change().dropna(); r.name = "v2 book (30% swing / 70% S&P)"
    qs.reports.html(r, benchmark=spy_v.pct_change().dropna().rename("S&P 500"),
                    output=os.path.join(ROOT, "reports", "backtest_v2_tearsheet.html"),
                    title="Swing screener v2 book vs S&P 500 (backtest, dividend-adjusted)",
                    download_filename="backtest_v2_tearsheet.html")
    log("done")
    print(json.dumps({k: summary[k] for k in ("v2_book_30_70", "spy", "neighbourhood")}, indent=1, default=str))


if __name__ == "__main__":
    main()
