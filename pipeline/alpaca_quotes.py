"""Real option quotes from Alpaca for the calls book's open calls, next to the model's price.

Data only: nothing is traded. Runs in the daily GitHub job after the close.
Needs the GitHub secrets ALPACA_API_KEY and ALPACA_SECRET_KEY (a free Alpaca account's paper-trading
keys work); without them it records that and exits cleanly.

Reads  data/paper/calls_open.json  [{symbol, strike, expiry, iv, contracts, opened}] (written by the
       daily Claude run), data/daily/latest/prices.json, data/weekly/latest/regime.json (10-year yield)
Writes data/options/latest.json and appends data/options/history.csv:
       market bid/ask/mid, Alpaca's implied vol and delta, the model's mid (Black-Scholes with the
       book's fixed vol), and the gap between them. The monthly review uses this to judge how far the
       modeled calls book is from reality.
Free plan feed = "indicative" (quotes derived from the real market, trades delayed); good enough to
check the model, not for exact fills.
"""
import csv, json, math, os, sys, datetime as dt
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, today_et, dump, load

URL = "https://data.alpaca.markets/v1beta1/options/snapshots"
TODAY = today_et()


def occ(sym, expiry, strike, kind="C"):
    """OCC option symbol, e.g. AAPL270219C00340000."""
    e = dt.date.fromisoformat(expiry)
    return f"{sym.replace('-', '').replace('.', '')}{e:%y%m%d}{kind}{round(strike * 1000):08d}"


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, r, sig):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / (sig * math.sqrt(T))
    return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d1 - sig * math.sqrt(T))


def main():
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    calls = load("paper", "calls_open.json", default=[])
    out = {"date": str(TODAY), "source": "Alpaca options snapshots (indicative feed)", "quotes": [], "errors": []}
    if not key or not secret:
        out["status"] = "Alpaca keys not set: add ALPACA_API_KEY and ALPACA_SECRET_KEY as GitHub secrets"
        dump(out, "options", "latest.json"); print(out["status"]); return
    if not calls:
        out["status"] = "no open calls"
        dump(out, "options", "latest.json"); print(out["status"]); return

    prices = load("daily", "latest", "prices.json", default={})
    reg = load("weekly", "latest", "regime.json", default=[])
    r = next((g["value"] for g in reg if g["metric"] == "10-year Treasury yield"), 4.0) / 100
    syms = {occ(c["symbol"], c["expiry"], c["strike"]): c for c in calls}
    try:
        resp = requests.get(URL, params={"symbols": ",".join(syms), "feed": "indicative", "limit": 1000},
                            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, timeout=30)
        resp.raise_for_status()
        snaps = resp.json().get("snapshots", {}) or {}
    except Exception as e:
        out["status"] = "request failed"
        out["errors"].append(f"{type(e).__name__}: {e}"[:300])
        dump(out, "options", "latest.json"); print(out); return

    rows = []
    for osym, c in syms.items():
        s = snaps.get(osym)
        S = prices.get(c["symbol"])
        T = (dt.date.fromisoformat(c["expiry"]) - TODAY).days / 365
        model = round(bs_call(S, c["strike"], T, r, c["iv"]), 2) if S and c.get("iv") else None
        if not s:
            out["errors"].append(f"{osym}: no quote (strike may not be listed)")
            rows.append({"date": str(TODAY), "contract": osym, "symbol": c["symbol"], "strike": c["strike"],
                         "expiry": c["expiry"], "underlying": S, "model_mid": model, "status": "no quote"})
            continue
        q = s.get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        mid = round((bid + ask) / 2, 2) if bid and ask else (s.get("latestTrade") or {}).get("p")
        g = s.get("greeks") or {}
        row = {"date": str(TODAY), "contract": osym, "symbol": c["symbol"], "strike": c["strike"], "expiry": c["expiry"],
               "underlying": S, "bid": bid, "ask": ask, "market_mid": mid,
               "spread_pct": round((ask - bid) / mid * 100, 2) if bid and ask and mid else None,
               "market_iv": s.get("impliedVolatility"), "market_delta": g.get("delta"),
               "model_iv": c.get("iv"), "model_mid": model,
               "model_vs_market_pct": round((model / mid - 1) * 100, 1) if model and mid else None,
               "contracts": c.get("contracts"), "status": "ok"}
        rows.append(row)
    out["quotes"] = rows
    ok = [x for x in rows if x.get("model_vs_market_pct") is not None]
    if ok:
        out["avg_model_vs_market_pct"] = round(sum(x["model_vs_market_pct"] for x in ok) / len(ok), 1)
        out["avg_abs_model_gap_pct"] = round(sum(abs(x["model_vs_market_pct"]) for x in ok) / len(ok), 1)
    out["status"] = f"{len(ok)} of {len(rows)} calls priced"
    dump(out, "options", "latest.json")
    path = os.path.join(DATA, "options", "history.csv")
    cols = ["date", "contract", "symbol", "strike", "expiry", "underlying", "bid", "ask", "market_mid", "spread_pct",
            "market_iv", "market_delta", "model_iv", "model_mid", "model_vs_market_pct", "contracts", "status"]
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(json.dumps({k: v for k, v in out.items() if k != "quotes"}, indent=1))


if __name__ == "__main__":
    main()
