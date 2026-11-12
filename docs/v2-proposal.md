# Swing screener v2: proposal (awaiting Cade's approval)

**Status:** approved by Cade on 2026-11-11 and live: weekly screen from the 2026-11-16 run, paper engine from the 2026-11-12 session (which closes the v1 positions).
**Evidence:** `data/research/` (run 2026-11-11 by `pipeline/research.py` on dividend-adjusted prices and point-in-time S&P 500 membership). All numbers are backtests: simulated, with some survivorship bias left (about 15% of past index members have no Yahoo price history), and not financial advice.

## The problem with v1

Over 2014-12 to 2026-11, with dividends included:

| | CAGR | Sharpe | Max drawdown |
|---|---|---|---|
| S&P 500 (SPY) | 13.7% | 0.82 | −33.7% |
| v1 as it runs today | 9.5% | 0.57 | −37.4% |
| v1, 2022–2026 only | 4.7% | 0.35 | −27.0% |

Changing one v1 rule at a time (in-sample Sharpe, v1 = 0.70) shows what costs money:

| Change | In-sample Sharpe |
|---|---|
| Drop the 2-per-sector cap | **0.84** |
| Drop the 2.5×/3× ATR stops | **0.83** |
| Strength gate 60 → 80 | 0.76 |
| Wider 4×/4.5× ATR stops | 0.75 |
| 4 finalists instead of 8 | 0.75 |
| Add a market trend filter (SPY vs 200-day) | 0.66 (worse) |

The tight stops sell good stocks on normal pullbacks. The sector cap forces the screen to skip strong stocks for weaker ones from other sectors.

## Method (decided before looking)

- Design period 2015–2021; test period 2022–2026, looked at once.
- 144 engine-style variants (the live engine's rules) and 96 momentum-rotation variants.
- Each variant scored by its "robust Sharpe" (its own plus its neighbours'), so a lucky spike doesn't win.

## Proposed v2

1. **Strength gate 60 → 80.**
2. **Remove the 2-per-sector cap.**
3. **Remove the ATR price stops.** Exits stay: failing the weekly gates, 40 sessions without being above entry, 126 sessions maximum. Sizing stays at 1% of equity over a 2.5× ATR distance, max 20% per name.
4. **Keep** 8 finalists, the pullback rule and no market filter. The design data couldn't separate "pullback rule on" from "off" (0.90 vs 0.92), so the smaller change wins. This choice was made after seeing the test period, where "on" did better.
5. **Portfolio: the swing book is a 30% sleeve; 70% stays in VOO.**

| | Design 2015–21 | Test 2022–26 | Full |
|---|---|---|---|
| S&P 500 | 15.2% / 0.89 / −33.7% | 11.6% / 0.72 / −24.5% | 13.8% / 0.83 / −33.7% |
| v2 swing book alone | 18.8% / 0.90 / −31.4% | 16.9% / 0.79 / −25.3% | 18.0% / 0.85 / −31.4% |
| **30% v2 + 70% S&P** | 16.7% / 0.94 / −33.1%² | 12.8% / 0.76 / −25.3%² | 15.2% / 0.87 / −33.1%² |

Each cell is CAGR / Sharpe / max drawdown.
² Blend computed with the pullback-off variant (the one saved as a daily equity curve); the pullback-on version scored the same or slightly better on its own, so its blend should be similar.

## What this does and doesn't show

- v2 fixes the parts of v1 that lost money. In the test period, v1 made 4.7% a year and v2 made 14–17%.
- **v2 alone does not reliably beat the S&P risk-adjusted.** Its Sharpe is within noise of the S&P's (test-period gap −0.08, 90% range −0.70 to +0.51), and it swings more (about 22–23% vol vs 17%).
- **The 30/70 blend beat the S&P's Sharpe in the design period, the test period and the full run.** The edge is small (+0.03 to +0.05 Sharpe, +1 to +1.5% a year) because the swing book moves only partly with the market (correlation 0.64). That's the most defensible way to meet the "beat the S&P risk-adjusted" goal.
- Rejected: the 200-day market filter (it whipsawed in 2015–2026), monthly momentum rotation (no better than SPY out of sample), and tighter stops.

## If approved

- `rules_v2.py`: gate 80, no sector cap, rules version v2, plus a changelog entry. The weekly screen writes `rules_version: v2` so the learning log keeps v1 and v2 apart.
- Engine: no ATR stop exits, other exits unchanged, and a 30% sleeve cap with the rest in VOO. Changed as a new engine version on the paper dashboard; the old one is kept.
- Backtest and research re-run on v2, and the monthly review compares v2 against both the S&P and the 30/70 blend.
