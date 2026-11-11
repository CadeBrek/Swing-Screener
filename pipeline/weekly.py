"""Weekly swing screen, data half (rules version v1), from free data.

Reproduces STEPS 1-5 of the weekly task with Yahoo Finance + SEC EDGAR instead of Bigdata.com and
Alpha Vantage. The judgment half (news thesis / catch / invalidation, publishing to the dashboard,
the learning log and the report) stays with the Claude scheduled task, which reads these files.

Writes data/weekly/<YYYY-MM-DD>/ and a copy in data/weekly/latest/:
  data.csv, screen.json, regime.json, sectors.json, finalists.json (news fields left blank),
  features.json, prices_all.json, status.json
"""
import io, json, os, shutil, sys, datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (DATA, today_et, path, dump, load, read_list, download, ret_since, wilder_atr,
                    earnings_info, first_event_on_or_after, yahoo)
from insiders import insider_read
import rules_v1 as rules

UNIVERSE_SIZE = 100
MIN_CAP, MIN_PRICE, MIN_VOL, MIN_BARS = 10e9, 10, 1_000_000, 126
SECTOR_ETFS = [("Energy", "XLE"), ("Health care", "XLV"), ("Consumer staples", "XLP"), ("Technology", "XLK"),
               ("Materials", "XLB"), ("Consumer discretionary", "XLY"), ("Communication services", "XLC"),
               ("Financials", "XLF"), ("Real estate", "XLRE"), ("Industrials", "XLI"), ("Utilities", "XLU")]
MARKET = ["SPY", "VOO", "MTUM", "IJR", "^VIX", "^TNX", "CL=F"] + [e for _, e in SECTOR_ETFS]

ASOF = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--asof=")), None)
SECOND_CLASS = {"GOOG", "FOX", "NWS", "BRK-A", "BF-A", "UHAL-B"}  # keep GOOGL, FOXA, NWSA, BRK-B ...
TODAY = dt.date.fromisoformat(ASOF) if ASOF else today_et()
OUT = f"validation/asof-{ASOF}" if ASOF else "weekly"   # an as-of run never touches the live files
errors = []


def sp500():
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                            headers={"User-Agent": "Mozilla/5.0 (swing-screener)"}, timeout=30).text
        t = pd.read_html(io.StringIO(html))[0]
        syms = [s.replace(".", "-").upper() for s in t["Symbol"]]
        syms = [s for s in syms if s not in SECOND_CLASS]  # one listing per company
        if len(syms) > 400:
            return syms
    except Exception as e:
        errors.append(f"S&P 500 list: {e}")
    return read_list("tracked.txt")  # fallback: every stock ever screened


def market_cap(s):
    try:
        fi = yf.Ticker(yahoo(s)).fast_info
        return s, float(fi["marketCap"])
    except Exception:
        return s, None


def info(s):
    try:
        return s, yf.Ticker(yahoo(s)).info
    except Exception:
        return s, {}


def r2(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)


def main():
    tracked = read_list("tracked.txt")
    for d in sorted(os.listdir(os.path.join(DATA, "weekly"))) if os.path.isdir(os.path.join(DATA, "weekly")) else []:
        if d != "latest":  # every stock in an earlier universe or finalist list keeps getting a price
            for f in ("features.json", "finalists.json"):
                for row in load("weekly", d, f, default=[]):
                    tracked.append(row["symbol"])
    candidates = sorted(set(sp500()))
    bars = download(set(candidates) | set(tracked) | set(MARKET))
    if ASOF:  # rebuild the screen exactly as it would have looked at that day's close
        cut = pd.Timestamp(ASOF)
        bars = {s: d[d.index <= cut] for s, d in bars.items() if (d.index <= cut).any()}
    if "SPY" not in bars:
        raise SystemExit("no SPY data from Yahoo - stopping")
    asof = bars["SPY"].index[-1]

    # ---- STEP 1: universe -----------------------------------------------------------------
    elig = []
    for s in candidates:
        d = bars.get(yahoo(s))
        if d is None or len(d) < MIN_BARS or d.index[-1] != asof:
            continue
        if d["Close"].iloc[-1] <= MIN_PRICE or d["Volume"].tail(63).mean() <= MIN_VOL:
            continue
        elig.append(s)
    with ThreadPoolExecutor(8) as ex:
        caps = dict(ex.map(market_cap, elig))
    ranked = sorted([s for s in elig if caps.get(s) and caps[s] > MIN_CAP], key=lambda s: -caps[s])
    universe = ranked[:UNIVERSE_SIZE]
    if len(universe) < 80:
        errors.append(f"universe only has {len(universe)} stocks")

    meta = load("meta.json", default={})
    need = [s for s in universe if s not in meta]
    with ThreadPoolExecutor(8) as ex:
        for s, inf in ex.map(info, need):
            if inf.get("sector"):
                meta[s] = {"sector": inf.get("sector"), "industry": inf.get("industry")}
    dump(meta, "meta.json")

    # ---- STEP 2: price data ---------------------------------------------------------------
    rows = []
    for s in universe:
        d = bars[yahoo(s)]; c = d["Close"]; last = d.tail(252)
        m = meta.get(s, {})
        rows.append(dict(symbol=s, sector=(m.get("industry") or m.get("sector") or "").split(" - ")[0],
                         ysector=m.get("sector"), price=r2(c.iloc[-1]),
                         hi52=r2(last["High"].max()), lo52=r2(last["Low"].min()),
                         ma50=r2(c.tail(50).mean()), ma200=r2(c.tail(200).mean()),
                         r1m=ret_since(c, 1), r3m=ret_since(c, 3), r6m=ret_since(c, 6), r1y=ret_since(c, 12)))
    df = pd.DataFrame(rows)
    df.drop(columns=["ysector"]).to_csv(path(OUT, str(TODAY), "data.csv"), index=False)

    # ---- STEP 3: regime ---------------------------------------------------------------------
    def R(sym, months):
        d = bars.get(sym)
        return ret_since(d["Close"], months) if d is not None else None
    spy = {m: R("SPY", m) for m in (1, 3, 6, 12)}
    vix_d = bars.get("^VIX"); tnx = bars.get("^TNX"); cl = bars.get("CL=F")
    vix = r2(vix_d["Close"].iloc[-1]) if vix_d is not None else None
    vix_1m = R("^VIX", 1)
    tnx_v = float(tnx["Close"].iloc[-1]) if tnx is not None else None
    if tnx_v is not None and tnx_v > 20:
        tnx_v /= 10
    wti = r2(cl["Close"].iloc[-1]) if cl is not None else None
    wti_ytd = None
    if cl is not None:
        prior = cl["Close"][cl.index < pd.Timestamp(TODAY.year, 1, 1)]
        if len(prior):
            wti_ytd = round((cl["Close"].iloc[-1] / prior.iloc[-1] - 1) * 100, 1)
    mom = round(R("MTUM", 3) - spy[3], 2) if R("MTUM", 3) is not None else None
    small = round(R("IJR", 3) - spy[3], 2) if R("IJR", 3) is not None else None

    regime = [
        {"metric": "S&P 500 (SPY) 3-month", "value": spy[3], "unit": "%", "read": ""},
        {"metric": "S&P 500 (SPY) 6-month", "value": spy[6], "unit": "%", "read": ""},
        {"metric": "VIX", "value": vix, "unit": "", "read": ""},
        {"metric": "Momentum factor vs S&P, 3-month", "value": mom, "unit": "%", "read": ""},
        {"metric": "Small caps vs S&P, 3-month", "value": small, "unit": "%", "read": ""},
        {"metric": "10-year Treasury yield", "value": r2(tnx_v), "unit": "%", "read": ""},
        {"metric": "WTI crude", "value": wti, "unit": "$", "read": ""},
    ]
    regime_extra = {"spy_1m": spy[1], "spy_1y": spy[12], "vix_1m_change": vix_1m, "wti_ytd": wti_ytd,
                    "spy_price": r2(bars["SPY"]["Close"].iloc[-1])}
    sectors = [{"sector": n, "etf": e, "r3m": R(e, 3), "r6m": R(e, 6)} for n, e in SECTOR_ETFS]

    # ---- STEP 4: score (exact v1 rules, shared with the backtest) -------------------------------
    df = rules.score(df, spy)

    screen = [{"symbol": r.symbol, "sector": r.sector, "passes": bool(r.passes), "score": round(r.final, 1),
               "rs_pct": int(r.rs_pct), "trend": int(r.trend), "r3m": r2(r.r3m), "r6m": r2(r.r6m), "r1y": r2(r.r1y),
               "off_high": round(r.off_high, 1), "why_not": r.why_not} for r in df.itertuples()]
    features = [{"symbol": r.symbol, "rp_id": r.symbol, "group": r.group, "sector": r.sector, "passes": bool(r.passes),
                 "score": round(r.final, 1), "rs_pct": int(r.rs_pct), "trend": int(r.trend),
                 "ext_50": round(r.ext_50, 1), "off_high": round(r.off_high, 1), "r1m": r2(r.r1m), "r3m": r2(r.r3m),
                 "r6m": r2(r.r6m), "r1y": r2(r.r1y), "price": r2(r.price), "ma50": r2(r.ma50), "ma200": r2(r.ma200)}
                for r in df.itertuples()]

    # ---- STEP 5: finalists ----------------------------------------------------------------------
    picks = rules.pick_finalists(df)
    finalists = []
    for rank, r in enumerate(picks, 1):
        s = r.symbol
        inf = info(s)[1]
        events, surprise = earnings_info(s)
        if events is None:
            errors.append(f"{s}: earnings dates unavailable")
        _, nxt = first_event_on_or_after(events, TODAY)
        dte = (nxt - TODAY).days if nxt else None
        atr = wilder_atr(bars[yahoo(s)])
        ins = insider_read(s, TODAY)
        if "_error" in ins:
            errors.append(f"{s} insiders: {ins.pop('_error')}")
        target = inf.get("targetMeanPrice")
        rec = inf.get("recommendationMean")
        extended = r.ext_50 > rules.EXT_CUT
        if extended:
            status, entry_lo, entry_hi = "Wait for pullback", r.ma50, r.ma50 * 1.05
        else:
            status = "Wait for earnings" if dte is not None and 0 <= dte <= 14 else "Actionable"
            entry_hi = r.price
            entry_lo = r.price - atr if atr else r.price
        if atr:
            stop, basis = entry_hi - rules.STOP_ATR * atr, "2.5 × ATR below entry"
        else:
            stop, basis = r.ma50 * 0.98, "2% under 50-day (ATR unavailable)"
            errors.append(f"{s}: ATR unavailable, used 50-day fallback")
        finalists.append({
            "symbol": s, "group": r.group, "sector": r.sector, "rank": rank, "score": round(r.final, 1),
            "rs_pct": int(r.rs_pct), "trend": int(r.trend), "price": r2(r.price), "ma50": r2(r.ma50),
            "ma200": r2(r.ma200), "hi52": r2(r.hi52), "off_high": round(r.off_high, 1), "ext_50": round(r.ext_50, 1),
            "r1m": r2(r.r1m), "r3m": r2(r.r3m), "r6m": r2(r.r6m), "r1y": r2(r.r1y), "rs3": round(r.rs3, 1),
            "rs6": round(r.rs6, 1), "atr": atr, "stop": r2(stop), "stop_pct": round((stop / entry_hi - 1) * 100, 1),
            "stop_basis": basis, "entry_lo": r2(entry_lo), "entry_hi": r2(entry_hi), "status": status,
            "earnings": str(nxt) if nxt else None, "days_to_earnings": dte, "eps_surprise": surprise,
            "target": r2(target), "target_upside": round((target / r.price - 1) * 100, 1) if target else None,
            "sentiment": None, "analyst_rec": r2(rec), **ins,
            "thesis": "", "catch": "", "invalidation": "",
        })

    prices_all = {s.replace("^", ""): r2(d["Close"].iloc[-1]) for s, d in bars.items() if d.index[-1] == asof}
    prices_all["VIX"] = vix
    status = {"run": str(TODAY), "data_date": str(asof.date()), "rules_version": rules.RULES_VERSION,
              "source": "Yahoo Finance (yfinance) + SEC EDGAR", "universe": len(universe),
              "passing": int(df.passes.sum()), "finalists": len(finalists), "regime_extra": regime_extra,
              "errors": errors, "ok": str(asof.date()) == str(TODAY) and len(universe) >= 80 and len(finalists) > 0}

    out = os.path.join(DATA, OUT, str(TODAY))
    for name, obj in [("screen.json", screen), ("regime.json", regime), ("sectors.json", sectors),
                      ("finalists.json", finalists), ("features.json", features), ("prices_all.json", prices_all),
                      ("status.json", status)]:
        dump(obj, OUT, str(TODAY), name)
    if not ASOF:
        latest = os.path.join(DATA, "weekly", "latest")
        shutil.rmtree(latest, ignore_errors=True)
        shutil.copytree(out, latest)
    print(json.dumps(status, indent=1))


if __name__ == "__main__":
    main()
