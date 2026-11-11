"""quantstats tearsheet for the $50k paper book vs the S&P 500 (runs on GitHub Actions).

Reads data/paper/equity.json (written by the daily Claude run after each session):
  [{"date", "equity", "spy_bench", "calls_equity"}, ...]
Writes reports/paper_tearsheet.html (open it in a browser) and data/paper/metrics.json.
"""
import json, os, sys
import pandas as pd
import quantstats as qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    rows = json.load(open(os.path.join(ROOT, "data", "paper", "equity.json")))
    df = pd.DataFrame(rows).drop_duplicates("date", keep="last")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    shares = df["equity"].pct_change().dropna()
    spy = df["spy_bench"].pct_change().dropna().rename("S&P 500")
    shares.name = "Paper book (shares)"
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    days = len(shares)
    metrics = {"as_of": str(df.index[-1].date()), "trading_days": days,
               "shares_return_pct": round((df["equity"].iloc[-1] / df["equity"].iloc[0] - 1) * 100, 2),
               "spy_return_pct": round((df["spy_bench"].iloc[-1] / df["spy_bench"].iloc[0] - 1) * 100, 2),
               "shares_max_drawdown_pct": round(qs.stats.max_drawdown(shares) * 100, 2),
               "spy_max_drawdown_pct": round(qs.stats.max_drawdown(spy) * 100, 2),
               "note": "Under ~60 trading days, ratios like Sharpe are noise; read the returns and drawdowns."}
    calls = df["calls_equity"].dropna()
    if len(calls) > 1:
        metrics["calls_return_pct"] = round((calls.iloc[-1] / calls.iloc[0] - 1) * 100, 2)
        metrics["calls_since"] = str(calls.index[0].date())
    if days >= 20:
        metrics["shares_sharpe"] = round(float(qs.stats.sharpe(shares)), 2)
        metrics["spy_sharpe"] = round(float(qs.stats.sharpe(spy)), 2)
    json.dump(metrics, open(os.path.join(ROOT, "data", "paper", "metrics.json"), "w"), indent=1)
    qs.reports.html(shares, benchmark=spy, output=os.path.join(ROOT, "reports", "paper_tearsheet.html"),
                    title="Swing screener paper book vs S&P 500 (simulated)", download_filename="paper_tearsheet.html")
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main()
