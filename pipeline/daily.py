"""Daily prices and earnings for the paper-trading run, from free data (Yahoo Finance).

Replaces STEPS 3-4 of the daily task (Bigdata.com tearsheet + events calendar).
Writes data/daily/latest/:
  prices.json    {"SYM": close, ..., "VIX": <weekly regime VIX>}  SPY, VOO, every finalist ever picked,
                 every tracked symbol (covers all open positions)
  earnings.json  {"SYM": "2026-10-28T21:30:00Z"}  earliest earnings event on or after today - 3 days,
                 for this week's finalists
  status.json    {date, data_date, market_open, missing, errors, ok}
market_open is false when Yahoo has no bar for today (weekend or market holiday): the paper run skips the day.
"""
import json, os, sys, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, today_et, dump, load, read_list, download, earnings_info, first_event_on_or_after

TODAY = today_et()


def main():
    errors = []
    latest_fin = load("weekly", "latest", "finalists.json", default=[])
    syms = {"SPY", "VOO"} | {f["symbol"] for f in latest_fin} | set(read_list("tracked.txt"))
    wk = os.path.join(DATA, "weekly")
    for d in (os.listdir(wk) if os.path.isdir(wk) else []):
        syms |= {f["symbol"] for f in load("weekly", d, "finalists.json", default=[])}
    bars = download(syms, period="1mo")
    if "SPY" not in bars:
        raise SystemExit("no SPY data from Yahoo - stopping")
    data_date = bars["SPY"].index[-1].date()
    market_open = data_date == TODAY

    prices, change = {}, {}
    for s, d in bars.items():
        if d.index[-1].date() != data_date:
            continue  # stale symbol (halted / delisted): leave it out so the run reports it
        prices[s] = round(float(d["Close"].iloc[-1]), 2)
        if len(d) > 1:
            change[s] = round((d["Close"].iloc[-1] / d["Close"].iloc[-2] - 1) * 100, 2)
    reg = load("weekly", "latest", "regime.json", default=[])
    vix = next((r["value"] for r in reg if r["metric"] == "VIX"), None)
    if vix is not None:
        prices["VIX"] = vix
    missing = sorted(s for s in syms if s not in prices)

    earnings = {}
    for f in latest_fin:
        events, _ = earnings_info(f["symbol"])
        if events is None:
            errors.append(f"{f['symbol']}: earnings dates unavailable")
            continue
        utc, _ = first_event_on_or_after(events, TODAY - dt.timedelta(days=3))
        if utc:
            earnings[f["symbol"]] = utc

    status = {"date": str(TODAY), "data_date": str(data_date), "market_open": market_open,
              "finalists_run": load("weekly", "latest", "status.json", default={}).get("run"),
              "missing": missing, "errors": errors, "change_1d": change,
              "ok": market_open and "SPY" in prices and "VOO" in prices}
    dump(prices, "daily", "latest", "prices.json")
    dump(earnings, "daily", "latest", "earnings.json")
    dump(status, "daily", "latest", "status.json")
    print(json.dumps({k: v for k, v in status.items() if k != "change_1d"}, indent=1))


if __name__ == "__main__":
    main()
