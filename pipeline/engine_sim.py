"""vectorbt simulation of the paper engine's trading rules (engines/engine.py), for backtests.

Each trading day, in this order (same as the live engine):
  1. exits for held stocks: close <= stop | failed the weekly gates | 40 sessions held and not above
     entry | 126 sessions held; otherwise the stop trails up to (highest close - 3 x ATR), never down
  2. sell just enough VOO (here SPY) to fund today's entries
  3. entries: shares = 1% of equity / (price - stop), max 20% of equity, max 10 names;
     stop = price - k x ATR (ATR from that week's screen)
  4. park leftover cash in SPY
Fills at the close with 0.05% cost. Signals (when a stock may be bought) are prepared by the caller.
"""
import numpy as np
import pandas as pd
from numba import njit
import vectorbt as vbt
from vectorbt.portfolio import nb as pnb
from vectorbt.portfolio.enums import NoOrder, SizeType, Direction

RISK, MAX_W, MAX_POS, TIME_STOP, MAX_HOLD, TRAIL_ATR, COST = 0.01, 0.20, 10, 40, 126, 3.0, 0.0005


@njit
def flex_order_func_nb(c, close, entry, exit_, atr_ref, spy_col, bil_col, regime_on, k_stop, k_trail, size_k,
                       risk, max_w, max_pos, time_stop, max_hold, cost, stop, peak, atr_e, entry_px, held,
                       plan_col, plan_size, plan_meta):
    i = c.i
    ncol = close.shape[1]
    park = spy_col if (bil_col < 0 or regime_on[i]) else bil_col     # where idle cash sits today
    other = bil_col if park == spy_col else spy_col
    if c.call_idx == 0:
        # ---- build today's plan --------------------------------------------------------------
        n = 0
        cash = c.last_cash[c.group]
        equity = cash
        for col in range(ncol):
            if c.last_position[col] > 0:
                equity += c.last_position[col] * close[i, col]
        n_open = 0
        proceeds = 0.0
        for col in range(ncol):
            pos = c.last_position[col]
            if col == spy_col or col == bil_col or pos <= 0:
                continue
            px = close[i, col]
            if np.isnan(px):
                n_open += 1
                continue
            held[col] += 1
            if px > peak[col]:
                peak[col] = px
            sell = px <= stop[col] or exit_[i, col] or (held[col] >= time_stop and px <= entry_px[col]) \
                or held[col] >= max_hold
            if sell:
                plan_col[n] = col; plan_size[n] = -pos; n += 1
                proceeds += pos * px * (1 - cost)
            else:
                n_open += 1
                trail = peak[col] - k_trail * atr_e[col]
                if trail > stop[col]:
                    stop[col] = trail
        # move idle cash between SPY and BIL when the market regime flips
        if other >= 0 and c.last_position[other] > 0:
            plan_col[n] = other; plan_size[n] = -c.last_position[other]; n += 1
            proceeds += c.last_position[other] * close[i, other] * (1 - cost)
        # entries (sized on today's equity)
        buy_cols = np.empty(ncol, dtype=np.int64); buy_sh = np.empty(ncol); nb_buy = 0
        need = 0.0
        for col in range(ncol):
            if col == spy_col or col == bil_col or not entry[i, col] or c.last_position[col] > 0 \
                    or n_open + nb_buy >= max_pos:
                continue
            px = close[i, col]; a = atr_ref[i, col]
            if np.isnan(px) or np.isnan(a) or a <= 0:
                continue
            dist = size_k * a                     # sizing distance (1% of equity at risk over it)
            if dist >= px:
                continue
            s0 = px - k_stop * a                  # actual stop (-inf = no stop)
            sh = min(risk * equity / dist, max_w * equity / px)
            buy_cols[nb_buy] = col; buy_sh[nb_buy] = sh; nb_buy += 1
            need += sh * px * (1 + cost)
            stop[col] = s0; peak[col] = px; atr_e[col] = a; entry_px[col] = px; held[col] = 0
        # sell the parking asset to fund entries
        park_pos = c.last_position[park]
        short = need - (cash + proceeds)
        if nb_buy > 0 and short > 0 and park_pos > 0:
            sh = min(park_pos, short / (close[i, park] * (1 - cost)) * 1.001)
            plan_col[n] = park; plan_size[n] = -sh; n += 1
        for k in range(nb_buy):
            plan_col[n] = buy_cols[k]; plan_size[n] = buy_sh[k]; n += 1
        plan_col[n] = park; plan_size[n] = np.inf; n += 1      # inf = put leftover cash in the parking asset
        plan_meta[0] = n; plan_meta[1] = 0
    k = plan_meta[1]
    if k >= plan_meta[0]:
        return -1, NoOrder
    plan_meta[1] = k + 1
    col = plan_col[k]; size = plan_size[k]
    if np.isinf(size):
        cash = c.last_cash[c.group]
        if cash < 100:
            return col, NoOrder
        return col, pnb.order_nb(size=cash * 0.999, size_type=SizeType.Value, direction=Direction.LongOnly,
                                 fees=cost, price=close[i, col])
    return col, pnb.order_nb(size=size, size_type=SizeType.Amount, direction=Direction.LongOnly, fees=cost,
                             price=close[i, col])


def run(close, entry, exit_, atr_ref, spy="SPY", k_stop=2.5, k_trail=TRAIL_ATR, size_k=None, bil=None,
        regime_on=None, init_cash=50_000, max_pos=MAX_POS):
    """close/entry/exit_/atr_ref: DataFrames (dates x symbols; SPY, and BIL if given, included as columns).
    k_stop/k_trail = np.inf for no stop. size_k: ATR multiple used for 1%-risk sizing (default k_stop).
    regime_on: bool array per day; when False idle cash sits in BIL instead of SPY."""
    cols = list(close.columns)
    spy_col = cols.index(spy)
    bil_col = cols.index(bil) if bil else -1
    size_k = k_stop if size_k is None else size_k
    reg = np.ones(len(close), dtype=np.bool_) if regime_on is None else np.asarray(regime_on, dtype=np.bool_)
    n = len(cols)
    C = close.values.astype(np.float64)
    Cv = pd.DataFrame(C, index=close.index, columns=cols).ffill().values   # valuation price for gaps
    pf = vbt.Portfolio.from_order_func(
        pd.DataFrame(Cv, index=close.index, columns=cols), flex_order_func_nb,
        C, entry.values.astype(np.bool_), exit_.values.astype(np.bool_), atr_ref.values.astype(np.float64),
        spy_col, bil_col, reg, float(k_stop), float(k_trail), float(size_k), RISK, MAX_W, int(max_pos),
        TIME_STOP, MAX_HOLD, COST,
        np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, dtype=np.int64),
        np.zeros(2 * n + 6, dtype=np.int64), np.zeros(2 * n + 6), np.zeros(2, dtype=np.int64),
        flexible=True, init_cash=init_cash, cash_sharing=True, group_by=True, freq="1D", max_orders=len(close) * 30)
    return pf
