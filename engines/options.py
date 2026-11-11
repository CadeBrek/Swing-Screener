"""Options book for the paper portfolio.  Runs right after engine.py, once per trading day.

Every share trade the engine opens gets a twin: a ~0.70-delta call with 90-120 days to expiry,
sized to the same delta exposure (contracts = shares / (100 x delta)). The twin is closed the
day the shares are sold. Money not in calls sits in VOO, exactly like the share book, so the two
books are directly comparable.

Prices are MODELED (no option quotes on Cade's data plans):
  Black-Scholes, no dividends. Volatility = 1.15 x realized vol estimated from the position's
  14-day ATR:  sigma = 1.15 * (ATR / 1.6 / price) * sqrt(252), clamped to 12%..100%.
  The vol is fixed at entry (and reset at each roll), so earnings IV crush and skew are NOT modeled.
  Rate = 10-year Treasury yield from the screener's regime.json.
  Strikes on a realistic grid; expiries on monthly third Fridays.
  Fills: buy at mid + half-spread, sell at mid - half-spread; half-spread = max($0.05, 0.75% of mid).
  Commission $0.65 per contract per side. Fractional contracts allowed (paper only).
  Roll: when 21 days or fewer remain and the shares are still held, sell and buy a fresh
  0.70-delta 90-120 day call.

Usage:
  python3 options.py --ostate options_state.json --shares-state state.new.json --date YYYY-MM-DD \
      --prices prices.json --regime regime.json --rows paper.json --out-ostate options_state.new.json
  (--ostate may be omitted on the first run; the book then starts from the share book's value today.)
  Appends rows of kind opt_equity / opt_position / opt_trade / opt_event to --rows in place.
"""
import argparse, json, math, datetime as dt

TARGET_DELTA = 0.70
DTE_MIN, DTE_MAX, DTE_AIM = 90, 120, 105
ROLL_DTE = 21
HALF_SPREAD_PCT, HALF_SPREAD_MIN = 0.0075, 0.05
COMMISSION = 0.65
VOL_MULT, ATR_TO_SD = 1.15, 1.6
VOL_MIN, VOL_MAX = 0.12, 1.00
VOO_SLIP = 0.0005


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, r, sig):
    """Black-Scholes call price and delta (no dividends)."""
    if T <= 0:
        return max(S - K, 0.0), (1.0 if S > K else 0.0)
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2), ncdf(d1)


def strike_step(S):
    for lim, step in ((25, 0.5), (50, 1), (100, 2.5), (250, 5), (1000, 10)):
        if S < lim:
            return step
    return 25


def third_friday(y, m):
    d = dt.date(y, m, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def pick_expiry(today):
    """Monthly expiry inside 90-120 days, nearest 105. Monthlies can straddle the window
    (e.g. 86 and 121 days); then take the one nearest the window, preferring the longer one."""
    fridays = []
    y, m = today.year, today.month
    for _ in range(8):
        fridays.append(third_friday(y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    inside = [f for f in fridays if DTE_MIN <= (f - today).days <= DTE_MAX]
    if inside:
        return min(inside, key=lambda f: abs((f - today).days - DTE_AIM))
    def gap(f):
        d = (f - today).days
        return (DTE_MIN - d if d < DTE_MIN else d - DTE_MAX, d < DTE_MIN)
    return min(fridays, key=gap)


def vol_from_atr(atr, price):
    sig = VOL_MULT * (atr / ATR_TO_SD / price) * math.sqrt(252)
    return min(VOL_MAX, max(VOL_MIN, sig))


def pick_call(S, today, r, sig):
    exp = pick_expiry(today)
    T = (exp - today).days / 365
    step = strike_step(S)
    best = None
    k = math.floor(S * 0.5 / step) * step
    while k <= S * 1.2:
        if k > 0:
            p, d = bs_call(S, k, T, r, sig)
            if best is None or abs(d - TARGET_DELTA) < abs(best[2] - TARGET_DELTA):
                best = (k, p, d)
        k = round(k + step, 4)
    K, mid, delta = best
    return {"strike": K, "expiry": exp.isoformat(), "mid": mid, "delta": delta}


def half_spread(mid):
    return max(HALF_SPREAD_MIN, HALF_SPREAD_PCT * mid)


ap = argparse.ArgumentParser()
for a in ["ostate", "shares-state", "date", "prices", "regime", "rows", "out-ostate"]:
    ap.add_argument("--" + a, required=a != "ostate")
A = ap.parse_args()

D = A.date
today = dt.date.fromisoformat(D)
px = {k.upper(): float(v) for k, v in json.load(open(A.prices)).items() if k.upper() != "VIX"}
SH = json.load(open(A.shares_state))
reg = json.load(open(A.regime))
ten = next((float(x["value"]) for x in reg if "10-year" in x["metric"]), 4.0)
r = ten / 100

if SH.get("last_date") != D:
    raise SystemExit(f"share state is for {SH.get('last_date')}, not {D}; run engine.py first")

sh_eq = SH["equity"][-1]
if A.ostate:
    O = json.load(open(A.ostate))
    if O["last_date"] and D <= O["last_date"]:
        raise SystemExit(f"options book already ran for {O['last_date']}; refusing to run {D}")
else:
    start = sh_eq["equity"]
    O = {"inception": D, "start_equity": start, "voo_shares": start / (px["VOO"] * (1 + VOO_SLIP)),
         "calls": [], "pairs": [], "equity": [], "events": [], "last_date": None}
    O["events"].append({"date": D, "symbol": "", "action": "Start",
                        "detail": f"Options book opened with ${start:,.2f}, the share book's value today"})

ev_today = []


def ev(sym, action, detail):
    e = {"date": D, "symbol": sym, "action": action, "detail": detail}
    O["events"].append(e)
    ev_today.append(e)


def voo_sell(amount):
    p = px["VOO"] * (1 - VOO_SLIP)
    O["voo_shares"] -= amount / p


def voo_buy(amount):
    p = px["VOO"] * (1 + VOO_SLIP)
    O["voo_shares"] += amount / p


def mid_now(c):
    T = (dt.date.fromisoformat(c["expiry"]) - today).days / 365
    return bs_call(px[c["symbol"]], c["strike"], T, r, c["iv"])


def sell_leg(c):
    """Sell the current leg at the modeled bid; returns realized P&L of this leg."""
    mid, _ = mid_now(c)
    bid = max(0.0, mid - half_spread(mid))
    proceeds = bid * 100 * c["contracts"] - COMMISSION * math.ceil(c["contracts"])
    voo_buy(proceeds)
    pnl = proceeds - c["leg_cost"]
    return pnl, bid


def buy_leg(c, S, sig, contracts_from_shares):
    pick = pick_call(S, today, r, sig)
    ask = pick["mid"] + half_spread(pick["mid"])
    contracts = round(contracts_from_shares / (100 * pick["delta"]), 2)
    cost = ask * 100 * contracts + COMMISSION * math.ceil(contracts)
    voo_sell(cost)
    c.update({"strike": pick["strike"], "expiry": pick["expiry"], "iv": round(sig, 4),
              "delta_at_buy": round(pick["delta"], 3), "contracts": contracts, "leg_fill": round(ask, 4),
              "leg_cost": round(cost, 2), "leg_date": D})
    return cost


share_pos = {p["id"]: p for p in SH["positions"]}
share_trades = {t["id"]: t for t in SH["trades"]}

# ---- 1. close calls whose shares were sold ------------------------------------------
keep = []
for c in O["calls"]:
    if c["trade_id"] in share_pos:
        keep.append(c); continue
    t = share_trades.get(c["trade_id"])
    pnl, bid = sell_leg(c)
    total = c["realized"] + pnl
    sh_basis = c["share_basis"]
    exit_px = t["exit_price"] if t else px[c["symbol"]]
    sh_pnl = c["shares"] * (exit_px - sh_basis)
    O["pairs"].append({
        "id": c["trade_id"], "symbol": c["symbol"], "setup": c["setup"], "late_start": c["late_start"],
        "entry_date": c["opened"], "exit_date": D, "exit_reason": t["exit_reason"] if t else "Shares closed",
        "days_held": (today - dt.date.fromisoformat(c["opened"])).days,
        "strike": c["strike"], "expiry": c["expiry"], "contracts": c["contracts"], "rolls": c["rolls"],
        "iv_at_entry": c["iv_first"], "call_cost": c["first_cost"], "call_pnl": round(total, 2),
        "call_ret_pct": round(total / c["first_cost"] * 100, 2),
        "share_capital": round(c["shares"] * sh_basis, 2), "share_pnl": round(sh_pnl, 2),
        "share_ret_pct": round((exit_px / sh_basis - 1) * 100, 2),
        "share_risk": c["share_risk"],
        "share_r": round(sh_pnl / c["share_risk"], 2) if c["share_risk"] else None,
        "call_r": round(total / c["share_risk"], 2) if c["share_risk"] else None,
        "pnl_diff": round(total - sh_pnl, 2),
        "capital_saved": round(c["shares"] * sh_basis - c["first_cost"], 2),
    })
    ev(c["symbol"], "Sell call", f"Shares sold, so the {c['strike']:g} call ({c['expiry']}) was sold at ${bid:,.2f}. "
                                 f"Call P&L ${total:+,.0f} vs shares ${sh_pnl:+,.0f}")
O["calls"] = keep

# ---- 2. roll calls near expiry ------------------------------------------------------
for c in O["calls"]:
    dte = (dt.date.fromisoformat(c["expiry"]) - today).days
    if dte > ROLL_DTE:
        continue
    pnl, bid = sell_leg(c)
    c["realized"] += pnl
    old = f"{c['strike']:g} {c['expiry']}"
    p = share_pos[c["trade_id"]]
    sig = vol_from_atr(p["atr"], px[c["symbol"]])
    buy_leg(c, px[c["symbol"]], sig, c["shares"])
    c["rolls"] += 1
    ev(c["symbol"], "Roll call", f"{dte} days left: sold {old} at ${bid:,.2f}, bought {c['strike']:g} {c['expiry']} "
                                 f"at ${c['leg_fill']:,.2f} ({c['contracts']:.2f} contracts)")

# ---- 3. open calls for new share positions -------------------------------------------
have = {c["trade_id"] for c in O["calls"]}
for p in SH["positions"]:
    if p["id"] in have:
        continue
    S = px[p["symbol"]]
    late = p["entry_date"] != D
    basis = p["fill"] if not late else S
    sig = vol_from_atr(p["atr"], S)
    risk = (p["fill"] - p["stop0"]) * p["shares"] if not late else (S - p["stop"]) * p["shares"]
    c = {"trade_id": p["id"], "symbol": p["symbol"], "setup": p["setup"], "opened": D, "late_start": late,
         "shares": p["shares"], "share_basis": round(basis, 4), "share_risk": round(max(risk, 0.01), 2),
         "realized": 0.0, "rolls": 0, "iv_first": round(sig, 4)}
    cost = buy_leg(c, S, sig, p["shares"])
    c["first_cost"] = round(cost, 2)
    O["calls"].append(c)
    ev(p["symbol"], "Buy call", f"{c['contracts']:.2f} x {c['strike']:g} call, exp {c['expiry']}, at ${c['leg_fill']:,.2f} "
                                f"(delta {c['delta_at_buy']:.2f}, modeled IV {sig * 100:.0f}%): ${cost:,.0f} vs "
                                f"${p['shares'] * basis:,.0f} for the shares" + (" (late start: shares were already held)" if late else ""))

# ---- 4. record ---------------------------------------------------------------------
calls_value = 0.0
open_rows = []
for c in O["calls"]:
    mid, delta = mid_now(c)
    val = mid * 100 * c["contracts"]
    calls_value += val
    S = px[c["symbol"]]
    open_pnl = c["realized"] + val - c["leg_cost"]
    open_rows.append({"kind": "opt_position", "id": c["trade_id"], "symbol": c["symbol"], "setup": c["setup"],
                      "opened": c["opened"], "strike": c["strike"], "expiry": c["expiry"],
                      "dte": (dt.date.fromisoformat(c["expiry"]) - today).days, "contracts": c["contracts"],
                      "delta": round(delta, 2), "iv": c["iv"], "mid": round(mid, 2), "value": round(val, 2),
                      "cost": c["first_cost"], "pnl": round(open_pnl, 2),
                      "ret_pct": round(open_pnl / c["first_cost"] * 100, 2),
                      "share_value": round(c["shares"] * S, 2),
                      "share_pnl": round(c["shares"] * (S - c["share_basis"]), 2),
                      "share_ret_pct": round((S / c["share_basis"] - 1) * 100, 2),
                      "rolls": c["rolls"], "late_start": c["late_start"]})
voo_val = O["voo_shares"] * px["VOO"]
eq = voo_val + calls_value
O["equity"].append({"date": D, "equity": round(eq, 2), "calls_value": round(calls_value, 2),
                    "voo_value": round(voo_val, 2), "shares_equity": sh_eq["equity"], "spy_bench": sh_eq["spy_bench"]})
O["last_date"] = D
O["events"] = O["events"][-500:]
json.dump(O, open(A.out_ostate, "w"), indent=1)

rows = json.load(open(A.rows))
rows = [x for x in rows if not str(x.get("kind", "")).startswith("opt_")]
rows += [{"kind": "opt_equity", **e} for e in O["equity"]]
rows += open_rows
rows += [{"kind": "opt_trade", **p} for p in O["pairs"]]
rows += [{"kind": "opt_event", **e} for e in O["events"][-200:]]
json.dump(rows, open(A.rows, "w"))

print(f"{D}: options book ${eq:,.2f} (calls ${calls_value:,.2f}, VOO ${voo_val:,.2f}) | share book ${sh_eq['equity']:,.2f} "
      f"| open calls {len(O['calls'])} | closed pairs {len(O['pairs'])}")
for e in ev_today:
    print(" -", e["symbol"], e["action"], e["detail"])
