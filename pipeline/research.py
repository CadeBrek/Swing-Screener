"""Strategy research: why v1 lags the S&P, and which changes hold up out of sample (GitHub Actions).

Design rules (decided before looking at results):
  * In-sample  = first test date .. 2021-12-31: used to choose.  Out-of-sample = 2022-01-01 .. today:
    looked at once, only for the variants chosen in-sample, plus benchmarks.
  * Goal = beat the S&P risk-adjusted (Sharpe), not raw return.
  * A variant is chosen by its "robust Sharpe": the median in-sample Sharpe of itself and every variant
    one setting away, so a lucky spike surrounded by poor neighbours loses.
  * Point-in-time S&P 500 membership (fja05680/sp500 dataset; fallback: rebuilt from Wikipedia's change
    log), so stocks are only eligible while they were in the index. Prices exist only for tickers Yahoo
    still carries, so some removed companies are missing: results remain somewhat optimistic.

Families tested
  A. Engine-style (the live paper engine's rules, via engine_sim): strength gate, number of finalists,
     sector cap, pullback rule, stop width, market-trend filter (SPY vs its 200-day; cash to T-bills/BIL).
  B. Rotation (classic momentum): rank by the v1 strength score, hold the top N that pass the trend gates,
     rebalance monthly or weekly, equal or inverse-volatility weights, optional trend filter and a
     turnover buffer (keep holdings while they stay in the top 2N).
  Benchmarks: SPY buy-and-hold; SPY with the same 200-day trend filter (SPY/BIL); equal-weight universe.
Writes data/research/ (results.json, grid_engine.csv, grid_rotation.csv, ablation.csv, equity.csv) and
reports/research_*.html (quantstats).
"""
import io, json, os, sys, time, bisect, datetime as dt
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, DATA, dump, download
import rules_v1 as rules
from backtest import months_back_rows, sectors, SECOND_CLASS

IS_END = pd.Timestamp("2021-12-31")
ADJ = [False]   # set when dividend-adjusted prices are used
YEARS = 11
t0 = time.time()
ALIAS = {"FB": "META", "ANTM": "ELV", "FISV": "FI", "BRK.B": "BRK-B", "BF.B": "BF-B", "RTN": "RTX", "UTX": "RTX",
         "CBS": "PARA", "VIAC": "PARA", "WLTW": "WTW", "ABC": "COR", "PKI": "RVTY", "FLT": "CPAY", "HFC": "DINO",
         "KORS": "CPRI", "COG": "CTRA", "TMK": "GL", "SYMC": "GEN", "NLOK": "GEN", "DISCA": "WBD", "GOOG": "GOOGL"}


def log(m):
    print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)


def norm(t):
    t = t.strip().upper()
    t = ALIAS.get(t, t)
    return t.replace(".", "-")


# ---------------------------------------------------------------- point-in-time membership
def membership():
    """Returns (sorted list of dates, list of sets, source)."""
    try:
        files = requests.get("https://api.github.com/repos/fja05680/sp500/contents", timeout=30).json()
        f = max((x for x in files if x["name"].startswith("S&P 500 Historical Components") and x["name"].endswith(".csv")),
                key=lambda x: x["name"])
        df = pd.read_csv(io.StringIO(requests.get(f["download_url"], timeout=60).text))
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        sets = [{norm(t) for t in str(x).split(",") if t.strip()} for x in df["tickers"]]
        return list(df["date"]), sets, f"fja05680/sp500 ({f['name']})"
    except Exception as e:
        log(f"fja05680 dataset unavailable ({e}); rebuilding from Wikipedia's change log")
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers={"User-Agent": "Mozilla/5.0 (swing-screener)"}, timeout=30).text
    tabs = pd.read_html(io.StringIO(html))
    cur = {norm(s) for s in tabs[0]["Symbol"]}
    ch = tabs[1]
    ch.columns = ["_".join(c for c in col if "Unnamed" not in c) if isinstance(col, tuple) else col for col in ch.columns]
    dcol = [c for c in ch.columns if "Date" in c][0]
    acol = [c for c in ch.columns if "Added" in c and "Ticker" in c][0]
    rcol = [c for c in ch.columns if "Removed" in c and "Ticker" in c][0]
    ch["d"] = pd.to_datetime(ch[dcol], errors="coerce")
    ch = ch.dropna(subset=["d"]).sort_values("d", ascending=False)
    dates, sets, s = [pd.Timestamp.today().normalize()], [set(cur)], set(cur)
    for d, g in ch.groupby("d", sort=False):
        for _, r in g.iterrows():
            if isinstance(r[acol], str): s.discard(norm(r[acol]))
            if isinstance(r[rcol], str): s.add(norm(r[rcol]))
        dates.append(d - pd.Timedelta(days=1)); sets.append(set(s))
    order = np.argsort(dates)
    return [dates[i] for i in order], [sets[i] for i in order], "Wikipedia change log (rebuilt)"


def members_on(d, mdates, msets):
    k = bisect.bisect_right(mdates, d) - 1
    return msets[max(k, 0)]


def load_inputs():
    """From an export folder (RESEARCH_DATA=path, made by export_data.py) or straight from the web."""
    src = os.environ.get("RESEARCH_DATA")
    if src:
        rd = lambda f: pd.read_parquet(os.path.join(src, f"{f}.parquet")).astype("float64")
        Cx, Hx, Lx, Vx = rd("close"), rd("high"), rd("low"), rd("volume")
        if os.path.exists(os.path.join(src, "adjclose.parquet")):
            # total-return prices: dividends reinvested (matters for SPY and for cash in BIL);
            # highs and lows scaled by the same factor so ATR stays consistent
            A = rd("adjclose").reindex(columns=Cx.columns)
            f = (A / Cx).where(A.notna(), 1.0)
            Cx, Hx, Lx = Cx * f, Hx * f, Lx * f
            ADJ[0] = True
        mem = json.load(open(os.path.join(src, "membership.json")))
        mdates = [pd.Timestamp(d) for d, _ in mem["snapshots"]]
        msets = [set(t) for _, t in mem["snapshots"]]
        sec = json.load(open(os.path.join(src, "sectors.json")))
        msrc = mem["source"]
    else:
        mdates, msets, msrc = membership()
        start = pd.Timestamp.today() - pd.DateOffset(years=YEARS + 2)
        ever = set().union(*[s for d, s in zip(mdates, msets) if d >= start - pd.DateOffset(years=1)])
        bars = download({t for t in ever if t not in SECOND_CLASS} | {"SPY", "BIL"}, period=f"{YEARS + 2}y")
        idx = bars["SPY"].index
        Cx, Hx, Lx, Vx = [pd.DataFrame({k: d[f] for k, d in bars.items()}).reindex(idx) for f in ("Close", "High", "Low", "Volume")]
        sec = None
    Cx = Cx.dropna(subset=["SPY"])
    idx = Cx.index
    start = idx[-1] - pd.DateOffset(years=YEARS + 2)
    ever = set().union(*[s for d, s in zip(mdates, msets) if d >= start - pd.DateOffset(years=1)])
    ever = {t for t in ever if t not in SECOND_CLASS}
    stocks = sorted(t for t in ever if t in Cx.columns and Cx[t].notna().any())
    if sec is None:
        sec = sectors(stocks)
    spy = Cx["SPY"]; bil = Cx["BIL"].reindex(idx).ffill()
    pick = lambda X: X.reindex(idx)[stocks]
    return mdates, msets, msrc, pick(Cx), pick(Hx), pick(Lx), pick(Vx), spy, bil, sec, len(ever)


# ---------------------------------------------------------------- metrics
def metrics(v, spy_v):
    v = v.dropna(); spy_v = spy_v.reindex(v.index).ffill()
    r = v.pct_change().dropna(); rs = spy_v.pct_change().reindex(r.index).fillna(0)
    yrs = len(r) / 252
    dd = (v / v.cummax() - 1).min()
    down = r[r < 0].std() * np.sqrt(252)
    beta = float(np.cov(r, rs)[0, 1] / rs.var()) if rs.var() > 0 else None
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else None
    return {"cagr_pct": round(cagr * 100, 2), "vol_pct": round(r.std() * np.sqrt(252) * 100, 2),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 3) if r.std() > 0 else None,
            "sortino": round(float(r.mean() * 252 / down), 3) if down > 0 else None,
            "max_dd_pct": round(float(dd) * 100, 2), "calmar": round(float(cagr / -dd), 3) if dd < 0 else None,
            "beta": round(beta, 2) if beta is not None else None,
            "alpha_pct": round(float((r.mean() - (beta or 0) * rs.mean()) * 252 * 100), 2)}


def windows(v, spy_v, start):
    out = {}
    for name, a, b in (("in_sample", start, IS_END), ("out_of_sample", IS_END + pd.Timedelta(days=1), v.index[-1]),
                       ("full", start, v.index[-1])):
        vv = v[(v.index >= a) & (v.index <= b)]
        out[name] = metrics(vv, spy_v) if len(vv) > 20 else None
    return out


# ---------------------------------------------------------------- main
def main():
    mdates, msets, msrc, C, H, L, V, spy, bil, sec, n_ever = load_inputs()
    stocks = list(C.columns)
    idx = C.index
    log(f"membership: {msrc}; {n_ever} tickers in the index during the window, {len(stocks)} with prices")

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
    spy200 = spy.rolling(200).mean()
    trend_up = (spy > spy200)

    start = idx[0] + pd.DateOffset(years=1, months=1)
    wk = pd.Series(idx, index=idx); wk = wk[wk.index >= start]
    weeks = list(pd.DatetimeIndex(wk.groupby(wk.dt.to_period("W")).first().values))
    months = list(pd.DatetimeIndex(wk.groupby(wk.dt.to_period("M")).first().values))
    log(f"{len(weeks)} weeks, {len(months)} months, test window {weeks[0].date()} .. {weeks[-1].date()}")

    def frame(d, top=None):
        mem = members_on(d, mdates, msets)
        cols = [s for s in stocks if s in mem]
        ok = (nbars.loc[d, cols] >= 126) & (C.loc[d, cols] > 10) & (avgvol.loc[d, cols] > 1_000_000) & C.loc[d, cols].notna()
        u = dollar.loc[d, cols][ok].dropna().sort_values(ascending=False).index
        if top:
            u = u[:top]
        df = pd.DataFrame({"symbol": u, "price": C.loc[d, u].values, "hi52": hi52.loc[d, u].values,
                           "ma50": ma50.loc[d, u].values, "ma200": ma200.loc[d, u].values,
                           "r1m": rets[1].loc[d, u].values, "r3m": rets[3].loc[d, u].values,
                           "r6m": rets[6].loc[d, u].values, "r1y": rets[12].loc[d, u].values,
                           "ysector": [sec.get(s) for s in u]})
        sc = rules.score(df, {m: spyr[m].loc[d] for m in (1, 3, 6, 12)})
        sc["atr"] = atr.loc[d, sc.symbol].values
        return sc
    coverage = []; sec_set = set(stocks)
    F100 = {}; FALL = {}
    for d in sorted(set(weeks) | set(months)):   # month starts are not always Mondays
        F100[d] = frame(d, 100)
        FALL[d] = frame(d)
        mem = members_on(d, mdates, msets)
        coverage.append(len([s for s in mem if s in sec_set]) / max(len(mem), 1))
    log(f"scored every week; average share of index members with price data: {np.mean(coverage):.0%}")

    test = idx >= weeks[0]
    spy_v = spy[test] / spy[test].iloc[0] * 50_000
    results = {"dividend_adjusted": ADJ[0], "membership_source": msrc, "coverage_pct": round(float(np.mean(coverage)) * 100, 1),
               "window": [str(weeks[0].date()), str(idx[-1].date())], "in_sample_end": str(IS_END.date())}

    # ---------------- benchmarks
    timing_on = trend_up.reindex(idx).ffill()
    m_on = pd.Series(np.nan, index=idx, dtype=float)
    for d in months:
        m_on[d] = 1.0 if bool(timing_on[d]) else 0.0
    m_on = m_on.ffill().fillna(1.0).astype(bool)
    rs_spy = spy.pct_change().fillna(0); rs_bil = bil.pct_change().fillna(0)
    turn = m_on.astype(int).diff().abs().fillna(0) * 0.0005
    timing_v = ((1 + np.where(m_on.shift(1, fill_value=True), rs_spy, rs_bil) - turn).cumprod())[test]
    timing_v = timing_v / timing_v.iloc[0] * 50_000
    bench = {"SPY buy & hold": spy_v, "SPY with 200-day trend filter (monthly, BIL when off)": timing_v}

    # ---------------- family A: engine-style
    import engine_sim
    Ct = C[test].copy()
    Ct["SPY"] = spy[test]; Ct["BIL"] = bil[test]
    col = {s: j for j, s in enumerate(Ct.columns)}
    rowpos = {d: i for i, d in enumerate(Ct.index)}
    vals = Ct.values
    regime_daily = np.zeros(len(Ct), dtype=bool)
    for w, d in enumerate(weeks):
        i0 = rowpos[d]; i1 = rowpos[weeks[w + 1]] if w + 1 < len(weeks) else len(Ct)
        regime_daily[i0:i1] = bool(trend_up[d])
    STOPS = {"atr 2.5/3 (v1)": (2.5, 3.0), "wide 4/4.5": (4.0, 4.5), "none": (np.inf, np.inf)}

    def engine_variant(gate, top_n, cap, pullback, stop, regime):
        entry = np.zeros(Ct.shape, dtype=bool); exit_ = np.zeros(Ct.shape, dtype=bool)
        atr_ref = np.full(Ct.shape, np.nan)
        for w, d in enumerate(weeks):
            i0 = rowpos[d]; i1 = rowpos[weeks[w + 1]] if w + 1 < len(weeks) else len(Ct)
            sc = F100[d]
            passes = (sc.price > sc.ma200) & (sc.ma50 > sc.ma200) & (sc.rs_pct >= gate)
            sc2 = sc.assign(passes=passes)
            off = regime and not trend_up[d]
            for r in sc2.itertuples():
                if off or not r.passes:
                    exit_[i0, col[r.symbol]] = True
            if off:
                continue
            for r in rules.pick_finalists(sc2, n=top_n, per_group=cap):
                j = col[r.symbol]; a = r.atr
                if not a or np.isnan(a):
                    continue
                if pullback and r.ext_50 > rules.EXT_CUT:
                    lo, hi = r.ma50 * 0.97, r.ma50 * 1.05
                else:
                    lo, hi = (r.price - a) * 0.99, r.price * 1.02
                win = vals[i0:i1, j]
                ok = np.where((win >= lo) & (win <= hi))[0]
                if len(ok):
                    entry[i0 + ok[0], j] = True; atr_ref[i0 + ok[0], j] = a
        if regime:  # also sell anything still held on a regime-off Monday (stocks that left the universe)
            for d in weeks:
                if not trend_up[d]:
                    exit_[rowpos[d], :col["SPY"]] = True
        k, kt = STOPS[stop]
        pf = engine_sim.run(Ct, pd.DataFrame(entry, index=Ct.index, columns=Ct.columns),
                            pd.DataFrame(exit_, index=Ct.index, columns=Ct.columns),
                            pd.DataFrame(atr_ref, index=Ct.index, columns=Ct.columns),
                            spy="SPY", bil="BIL", k_stop=k, k_trail=kt, size_k=2.5,
                            regime_on=regime_daily if regime else None, max_pos=10)
        return pf.value()

    A_PARAMS = {"gate": [60, 80, 90], "top_n": [4, 8], "cap": [2, 99], "pullback": [True, False],
                "stop": list(STOPS), "regime": [False, True]}
    BASE_A = {"gate": 60, "top_n": 8, "cap": 2, "pullback": True, "stop": "atr 2.5/3 (v1)", "regime": False}
    gridA, curves = [], {}
    import itertools
    for combo in itertools.product(*A_PARAMS.values()):
        p = dict(zip(A_PARAMS, combo))
        v = engine_variant(**p)
        key = "A|" + "|".join(f"{k}={p[k]}" for k in A_PARAMS)
        curves[key] = v
        m = windows(v, spy_v, weeks[0])
        gridA.append({**p, "key": key, **{f"is_{k}": x for k, x in (m["in_sample"] or {}).items()},
                      **{f"oos_{k}": x for k, x in (m["out_of_sample"] or {}).items()},
                      **{f"full_{k}": x for k, x in (m["full"] or {}).items()}})
    gridA = pd.DataFrame(gridA)
    log(f"family A: {len(gridA)} engine-style variants")

    # ablation: v1 with one thing changed at a time (diagnosis)
    ablation = []
    def find(params):
        q = gridA
        for k, x in params.items():
            q = q[q[k] == x]
        return q.iloc[0]
    base_row = find(BASE_A)
    for k, opts in A_PARAMS.items():
        for x in opts:
            if x == BASE_A[k]:
                continue
            r = find({**BASE_A, k: x})
            ablation.append({"change": f"{k}: {BASE_A[k]} -> {x}", "is_sharpe": r.is_sharpe, "is_cagr": r.is_cagr_pct,
                             "is_max_dd": r.is_max_dd_pct, "full_sharpe": r.full_sharpe, "full_cagr": r.full_cagr_pct})
    ablation = pd.DataFrame(ablation)

    # ---------------- family B: rotation
    import vectorbt as vbt
    names = stocks + ["SPY", "BIL"]
    Cr = Ct[names] if set(names) <= set(Ct.columns) else Ct
    j_of = {s: j for j, s in enumerate(Cr.columns)}

    def rotation(universe, n, weight, regime, buffer, freq):
        dates = months if freq == "M" else weeks
        W = np.full(Cr.shape, np.nan)
        held = []
        for d in dates:
            i = rowpos[d]
            w = np.zeros(Cr.shape[1])
            if regime and not trend_up[d]:
                w[j_of["BIL"]] = 1.0; held = []
            else:
                sc = (F100 if universe == "top100" else FALL)[d]
                g = sc[(sc.price > sc.ma200) & (sc.ma50 > sc.ma200)].sort_values("rs_score", ascending=False)
                ranked = list(g.symbol)
                if buffer:
                    keep = [s for s in held if s in ranked[:2 * n]]
                    pick = keep + [s for s in ranked if s not in keep][: max(0, n - len(keep))]
                else:
                    pick = ranked[:n]
                held = pick
                if pick:
                    if weight == "eq":
                        ws = np.ones(len(pick)) / n
                    else:
                        iv = g.set_index("symbol").loc[pick]
                        inv = 1 / (iv.atr / iv.price).clip(lower=1e-4)
                        ws = (inv / inv.sum()).values * len(pick) / n
                        ws = np.minimum(ws, 2 / n)
                    for s, x in zip(pick, ws):
                        w[j_of[s]] = x
                w[j_of["SPY"]] = max(0.0, 1 - w.sum())   # unfilled slots stay in the market
            W[i] = w
        pf = vbt.Portfolio.from_orders(Cr.ffill(), size=pd.DataFrame(W, index=Cr.index, columns=Cr.columns),
                                       size_type="targetpercent", fees=0.0005, init_cash=50_000, cash_sharing=True,
                                       group_by=True, call_seq="auto", freq="1D")
        return pf.value()

    B_PARAMS = {"universe": ["top100", "all"], "n": [5, 10, 20], "weight": ["eq", "ivol"], "regime": [False, True],
                "buffer": [False, True], "freq": ["M", "W"]}
    gridB = []
    for combo in itertools.product(*B_PARAMS.values()):
        p = dict(zip(B_PARAMS, combo))
        v = rotation(**p)
        key = "B|" + "|".join(f"{k}={p[k]}" for k in B_PARAMS)
        curves[key] = v
        m = windows(v, spy_v, weeks[0])
        gridB.append({**p, "key": key, **{f"is_{k}": x for k, x in (m["in_sample"] or {}).items()},
                      **{f"oos_{k}": x for k, x in (m["out_of_sample"] or {}).items()},
                      **{f"full_{k}": x for k, x in (m["full"] or {}).items()}})
    gridB = pd.DataFrame(gridB)
    log(f"family B: {len(gridB)} rotation variants")

    # ---------------- choose in-sample by robust Sharpe, then look out of sample once
    def robust(grid, params):
        rs = []
        for _, r in grid.iterrows():
            nb = [r.is_sharpe]
            for k, opts in params.items():
                for x in opts:
                    if x == r[k]:
                        continue
                    q = grid
                    for kk in params:
                        q = q[q[kk] == (x if kk == k else r[kk])]
                    if len(q):
                        nb.append(q.iloc[0].is_sharpe)
            rs.append(float(np.nanmedian(nb)))
        grid = grid.copy(); grid["robust_is_sharpe"] = rs
        return grid
    gridA = robust(gridA, A_PARAMS); gridB = robust(gridB, B_PARAMS)
    pickA = gridA.sort_values("robust_is_sharpe", ascending=False).iloc[0]
    pickB = gridB.sort_values("robust_is_sharpe", ascending=False).iloc[0]

    def summ(name, v):
        return {"name": name, **windows(v, spy_v, weeks[0])}
    results["benchmarks"] = [summ(k, v) for k, v in bench.items()]
    results["v1_baseline"] = summ("v1 as live (engine-style baseline)", curves[base_row.key])
    results["chosen"] = [summ("A (engine-style) " + pickA.key[2:], curves[pickA.key]),
                         summ("B (rotation) " + pickB.key[2:], curves[pickB.key])]
    results["chosen_params"] = {"A": {k: pickA[k] for k in A_PARAMS}, "B": {k: pickB[k] for k in B_PARAMS}}
    for g, lab in ((gridA, "A"), (gridB, "B")):
        ok = g.dropna(subset=["is_sharpe", "oos_sharpe"])
        results[f"is_oos_rank_corr_{lab}"] = round(float(ok.is_sharpe.rank().corr(ok.oos_sharpe.rank())), 3)
        results[f"top5_in_sample_{lab}"] = ok.sort_values("robust_is_sharpe", ascending=False).head(5)[
            ["key", "is_sharpe", "robust_is_sharpe", "oos_sharpe", "oos_cagr_pct", "oos_max_dd_pct"]].to_dict("records")
        results[f"share_beating_spy_oos_{lab}"] = round(float((ok.oos_sharpe > results["benchmarks"][0]["out_of_sample"]["sharpe"]).mean()) * 100, 1)

    os.makedirs(os.path.join(DATA, "research"), exist_ok=True)
    gridA.to_csv(os.path.join(DATA, "research", "grid_engine.csv"), index=False)
    gridB.to_csv(os.path.join(DATA, "research", "grid_rotation.csv"), index=False)
    ablation.to_csv(os.path.join(DATA, "research", "ablation.csv"), index=False)
    eq = pd.DataFrame({"spy": spy_v, "spy_trend_filter": timing_v, "v1": curves[base_row.key],
                       "chosen_A": curves[pickA.key], "chosen_B": curves[pickB.key]})
    eq.to_csv(os.path.join(DATA, "research", "equity.csv"))
    dump(results, "research", "results.json")

    import quantstats as qs
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    for lab, key in (("A", pickA.key), ("B", pickB.key)):
        r = curves[key].pct_change().dropna(); r.name = f"Candidate {lab}"
        b = spy_v.pct_change().dropna().rename("S&P 500")
        oos = r.index > IS_END
        qs.reports.html(r[oos], benchmark=b[b.index > IS_END], output=os.path.join(ROOT, "reports", f"research_{lab}_out_of_sample.html"),
                        title=f"Candidate {lab}, out of sample (2022 on) vs S&P 500", download_filename=f"research_{lab}_oos.html")
    log("done")
    print(json.dumps({k: results[k] for k in ("benchmarks", "v1_baseline", "chosen", "chosen_params")}, indent=1, default=str))


if __name__ == "__main__":
    main()
