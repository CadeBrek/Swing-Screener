"""Macro backdrop from OpenBB (runs on GitHub Actions before the weekly screen).

Free without any key (Federal Reserve data via OpenBB):
  Treasury yield curve (3-month, 2-year, 10-year, 30-year), 2s10s and 3m10y spreads, effective fed funds rate.
With a free FRED key saved as the GitHub secret FRED_API_KEY (optional):
  CPI inflation (year over year), unemployment rate, high-yield credit spread.
Writes data/macro/latest.json and data/macro/<date>.json. Information only: never changes the screen.
"""
import json, os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import today_et, dump

TODAY = today_et()
errors = []


def last_row(df):
    df = df.dropna(how="all")
    return df.index[-1], df.iloc[-1]


def main():
    from openbb import obb
    out = {"date": str(TODAY), "source": "OpenBB (Federal Reserve; FRED when a key is set)"}

    try:
        tr = obb.fixedincome.government.treasury_rates(
            start_date=str(TODAY - dt.timedelta(days=400)), provider="federal_reserve").to_df()
        when, row = last_row(tr)
        pick = {k: row.get(k) for k in ("month_3", "year_2", "year_10", "year_30")}
        y = {k: (round(float(v) * 100, 2) if v is not None and abs(float(v)) < 1 else round(float(v), 2))
             for k, v in pick.items() if v is not None}  # OpenBB returns decimals (0.0425); show percent
        out["treasury_yields_pct"] = y
        out["yields_as_of"] = str(getattr(when, "date", lambda: when)())
        if "year_10" in y and "year_2" in y:
            out["spread_2s10s_pct"] = round(y["year_10"] - y["year_2"], 2)
        if "year_10" in y and "month_3" in y:
            out["spread_3m10y_pct"] = round(y["year_10"] - y["month_3"], 2)
            out["curve_read"] = "inverted (3m above 10y)" if y["year_10"] < y["month_3"] else "normal (10y above 3m)"
        # a year ago, for direction
        yr = tr[tr.index <= (when - dt.timedelta(days=365))]
        if len(yr) and "year_10" in yr:
            v = float(yr["year_10"].dropna().iloc[-1]); v = v * 100 if abs(v) < 1 else v
            out["ten_year_change_1y_pct"] = round(y["year_10"] - v, 2)
    except Exception as e:
        errors.append(f"treasury curve: {type(e).__name__}: {e}"[:200])

    try:
        ff = obb.fixedincome.rate.effr(start_date=str(TODAY - dt.timedelta(days=30)), provider="federal_reserve").to_df()
        when, row = last_row(ff)
        v = float(row.get("rate"))
        out["fed_funds_effective_pct"] = round(v * 100 if v < 1 else v, 2)
    except Exception as e:
        errors.append(f"fed funds: {type(e).__name__}: {e}"[:200])

    key = os.environ.get("FRED_API_KEY")
    if key:
        try:
            obb.user.credentials.fred_api_key = key
            fr = obb.economy.fred_series(symbol="CPIAUCSL,UNRATE,BAMLH0A0HYM2",
                                         start_date=str(TODAY - dt.timedelta(days=500)), provider="fred").to_df()
            cpi = fr["CPIAUCSL"].dropna()
            out["cpi_yoy_pct"] = round((cpi.iloc[-1] / cpi.iloc[-13] - 1) * 100, 2)
            out["cpi_as_of"] = str(cpi.index[-1])[:10]
            out["unemployment_pct"] = round(float(fr["UNRATE"].dropna().iloc[-1]), 1)
            out["high_yield_spread_pct"] = round(float(fr["BAMLH0A0HYM2"].dropna().iloc[-1]), 2)
        except Exception as e:
            errors.append(f"FRED: {type(e).__name__}: {e}"[:200])
    else:
        out["fred"] = "not set up (add a free FRED key as the GitHub secret FRED_API_KEY for CPI, unemployment, credit spreads)"

    out["errors"] = errors
    dump(out, "macro", "latest.json")
    dump(out, "macro", f"{TODAY}.json")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
