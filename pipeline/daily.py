"""Daily prices and earnings for the paper-trading run, from free data (Yahoo Finance).

Replaces STEPS 3-4 of the daily task (Bigdata.com tearsheet + events calendar).
Writes data/daily/latest/:
  prices.json    {"SYM": close, ..., "VIX": <weekly regime VIX>}  SPY, VOO, every finalist ever picked,
                 every tracked symbol (covers all open positions)
  earnings.json  {"SYM": "2026-10-28T21:30:00Z"}  earliest earnings event on or after today - 3 days,
                 for this week's finalists
  status.json    {date, data_date, market_open, missing, errors, ok}
market_open comes from the NYSE calendar (false on weekends and holidays: the paper run skips the day).
data_current is false when Yahoo has no bar for today on a trading day: the paper run re-runs this job
and, if it is still stale, tells Cade instead of skipping silently.
"""
import json, os, sys, time, datetime as dt

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
    # Was today a trading day? Ask the official NYSE calendar, never infer it from missing data:
    # Yahoo sometimes drops or delays today's bar, which once made a normal day look like a holiday.
    import pandas_market_calendars as mcal
    market_open = len(mcal.get_calendar("NYSE").schedule(str(TODAY), str(TODAY))) > 0
    for attempt in range(4):
        bars = download(syms, period="1mo")
        if "SPY" not in bars:
            raise SystemExit("no SPY data from Yahoo - stopping")
        data_date = bars["SPY"].index[-1].date()
        if not market_open or data_date == TODAY:
            break
        errors.append(f"attempt {attempt + 1}: Yahoo's latest bar is {data_date}, not today; retrying")
        time.sleep(90)
    data_current = data_date == TODAY

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

    # earnings for this week's AND last week's finalists: the dashboard may still be trading last
    # week's list on a Monday, depending on which job finishes first
    runs = sorted(d for d in (os.listdir(wk) if os.path.isdir(wk) else []) if d[:2] == "20")[-3:]
    fin_syms = sorted({f["symbol"] for f in latest_fin} |
                      {f["symbol"] for d in runs for f in load("weekly", d, "finalists.json", default=[])})
    earnings = {}
    for f in [{"symbol": s} for s in fin_syms]:
        events, _ = earnings_info(f["symbol"])
        if events is None:
            errors.append(f"{f['symbol']}: earnings dates unavailable")
            continue
        utc, _ = first_event_on_or_after(events, TODAY - dt.timedelta(days=3))
        if utc:
            earnings[f["symbol"]] = utc

    status = {"date": str(TODAY), "data_date": str(data_date), "market_open": market_open, "data_current": data_current,
              "finalists_run": load("weekly", "latest", "status.json", default={}).get("run"),
              "missing": missing, "errors": errors, "change_1d": change,
              "ok": market_open and data_current and "SPY" in prices and "VOO" in prices}
    dump(prices, "daily", "latest", "prices.json")
    dump(earnings, "daily", "latest", "earnings.json")
    dump(status, "daily", "latest", "status.json")
    print(json.dumps({k: v for k, v in status.items() if k != "change_1d"}, indent=1))


if __name__ == "__main__":
    main()
