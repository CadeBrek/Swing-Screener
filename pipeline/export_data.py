"""Export research inputs (prices, point-in-time membership, sectors) to data/export/ for offline research.

The workflow commits the result to the separate branch `research-data`, so main stays small.
Files: close/high/low/volume.parquet (dates x tickers, float32), membership.json, sectors.json, info.json
"""
import json, os, sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, download
from backtest_v1 import sectors, SECOND_CLASS
import research

OUT = os.environ.get("EXPORT_DIR") or os.path.join(DATA, "export")


def main():
    mdates, msets, src = research.membership()
    start = pd.Timestamp.today() - pd.DateOffset(years=research.YEARS + 2)
    ever = set().union(*[s for d, s in zip(mdates, msets) if d >= start - pd.DateOffset(years=1)])
    ever = {t for t in ever if t not in SECOND_CLASS}
    bars = download(ever | {"SPY", "BIL"}, period=f"{research.YEARS + 2}y", adj=True)
    idx = bars["SPY"].index
    os.makedirs(OUT, exist_ok=True)
    for f in ("Close", "High", "Low", "Volume", "Adj Close"):
        df = pd.DataFrame({s: d[f] for s, d in bars.items() if f in d}).reindex(idx).astype("float32")
        df.to_parquet(os.path.join(OUT, f"{f.lower().replace(' ', '')}.parquet"), compression="zstd")
    keep = [(str(d.date()), sorted(s)) for d, s in zip(mdates, msets) if d >= start - pd.DateOffset(years=2)]
    json.dump({"source": src, "snapshots": keep}, open(os.path.join(OUT, "membership.json"), "w"))
    json.dump(sectors(sorted(s for s in ever if s in bars)), open(os.path.join(OUT, "sectors.json"), "w"))
    json.dump({"tickers_in_index": len(ever), "with_prices": len(bars) - 2, "start": str(idx[0].date()),
               "end": str(idx[-1].date())}, open(os.path.join(OUT, "info.json"), "w"), indent=1)
    print(open(os.path.join(OUT, "info.json")).read(), src)


if __name__ == "__main__":
    main()
