# Swing Screener

A weekly momentum screen for 1–6 month swing trades, a $50k paper portfolio that trades its signals, a parallel calls book, and a learning log that checks whether the screen's rules actually work.

Built and run with Claude. The code here is the source of truth for the rules; the live dashboards read and write the data.

## Where the data comes from

GitHub Actions run the free-data scripts in `pipeline/` on GitHub's servers, so they use no Claude or connector credits:

| Job | When (ET) | Script | Writes |
|---|---|---|---|
| Daily prices | weekdays ~4:40 pm | `pipeline/daily.py` | `data/daily/latest/` (closing prices, earnings dates) |
| Weekly screen data | Mondays ~4:45 pm | `pipeline/weekly.py` | `data/weekly/<date>/` and `latest/` (universe, scores, finalists, ATR stops, insider flags) |
| Validation | on demand | `pipeline/weekly.py --asof=<date>` | `data/validation/` (rebuild an old screen to compare sources) |

Sources: Yahoo Finance (via `yfinance`) and SEC EDGAR. Claude's scheduled tasks then read those files, write the news summaries, run the engines and update the dashboards. To re-run a job by hand, add a line to `data/run-daily`, `data/run-weekly` or `data/run-validate` and push.

## How it fits together

```
Weekly screen (Mon)  ──►  finalists / screen / regime  ──►  Daily paper run (weekdays)
        │                                                        │
        ▼                                                        ▼
  learning log (history.py)                         share book (engine.py)
        │                                           calls book (options.py)
        └──────────────►  Monthly review  ◄──────────────────────┘
                          (report only; proposes rule changes)
```

| Piece | File | What it does |
|---|---|---|
| Screen rules | `tasks/weekly-swing-trade-screen.md` | Universe, scoring, gates, finalists, stops, insider flag |
| Share engine | `engines/engine.py` | One trading day of the $50k paper book |
| Calls engine | `engines/options.py` | Mirrors each share trade with a ~0.70-delta call (modeled Black-Scholes prices) |
| Learning log | `engines/history.py` | Logs every stock every week with forward 4/8/13/26-week returns |
| Rules version | `rules.json` | Current version and changelog |
| Scheduled tasks | `tasks/*.md` | The exact prompts each run follows |

## Dashboards

- Screener: https://claude.ai/artifact/ULtzUMAxM2BaXZmpGbamzU
- Paper portfolio: https://claude.ai/artifact/BPQJfrJd3xztYfGUF5ZmV9

## Changing the rules

1. The monthly review proposes a change with the numbers behind it.
2. Make the change on a branch and open a pull request. The diff shows exactly what moved.
3. Bump the version in `rules.json` (v1 → v2) with a changelog entry so results stay comparable across versions.
4. Merge, then sync the new code to the dashboard engines.

Paper trades are simulated and option prices are modeled. Nothing here is financial advice.
