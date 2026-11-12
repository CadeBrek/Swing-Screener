"""Paper-trading engine for the swing screener (rules v2).  One run = one trading day.

Usage:
  python3 engine.py --state state.json --date YYYY-MM-DD --prices prices.json \
      --finalists finalists.json --screen screen.json --earnings earnings.json \
      --finalists-url /_blob/<id> --out-state state.new.json --out-rows paper.json

prices.json   {"SYM": close, ...}  must include SPY, VOO, every held symbol and every finalist
earnings.json {"SYM": "2026-10-28T21:30:00Z", ...}  next earnings call time (UTC) per finalist
Rules (keep in sync with the dashboard's "How it trades" section):
  Entries   actionable  -> buy if close is within the buy zone (entry_lo*0.99 .. entry_hi*1.02)
            earnings    -> after the report: on the session after the reaction day, buy if
                           close > last close before the report AND close > 50-day
            pullback    -> buy once close <= entry_hi, staying >= entry_lo*0.97, no earnings in 3 days
  Sleeve    v2: the swing trades are a 30% sleeve of the book; the other 70% stays in VOO
  Sizing    1% of the sleeve at risk over a 2.5 x ATR distance: shares = 1% x sleeve / (2.5 x ATR);
            max 20% of the sleeve per name; max 10 names; no new buy if stocks would exceed the sleeve
  Stop      v2 has NO price stop (the backtest showed ATR stops sold good stocks on normal pullbacks).
            "stop0" is kept only as the sizing / R-multiple reference.
  Exits     stock fails the weekly screen gates | 40 sessions held and not above entry | 126 sessions held
  Switch    the first v2 session closes every position opened under v1 ("Switched to v2 rules"), so the
            v1 and v2 track records stay separate (each trade records its rules version)
  Cash      idle cash sits in VOO; VOO is sold to fund entries
  Costs     0.05% slippage on every fill
"""
import argparse, json, datetime as dt

SLIP = 0.0005
RISK = 0.01
MAX_W = 0.20
MAX_POS = 10
TRAIL_ATR = 3.0
STOP_ATR = 2.5
TIME_STOP = 40
RULES = "v2"
SLEEVE = 0.30          # v2: swing sleeve as a share of the whole book
USE_PRICE_STOP = False # v2: no ATR stop exits
MAX_HOLD = 126

ap = argparse.ArgumentParser()
for a in ["state", "date", "prices", "finalists", "screen", "earnings", "finalists-url", "out-state", "out-rows"]:
    ap.add_argument("--" + a, required=a not in ("state",))
A = ap.parse_args()

D = A.date
today = dt.date.fromisoformat(D)
px = {k.upper(): float(v) for k, v in json.load(open(A.prices)).items()}
fin = json.load(open(A.finalists))
screen = {r["symbol"]: r for r in json.load(open(A.screen))}
earn = {k.upper(): v for k, v in json.load(open(A.earnings)).items()}

if A.state:
    S = json.load(open(A.state))
else:
    S = {"inception": D, "start_equity": 50000.0, "cash": 50000.0, "voo_shares": 0.0,
         "spy_bench_shares": 50000.0 / px["SPY"], "positions": [], "watch": [], "trades": [],
         "equity": [], "events": [], "last_date": None, "finalists_url": None, "next_trade_id": 1}

if S["last_date"] and D <= S["last_date"]:
    raise SystemExit(f"already ran for {S['last_date']}; refusing to run {D}")

def ev(sym, action, detail):
    S["events"].append({"date": D, "symbol": sym, "action": action, "detail": detail})

def next_weekday(d):
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d

def reaction_day(iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    et = t - dt.timedelta(hours=4)  # ET (EDT); in winter this is an hour off, which only matters for calls right at the close
    d = et.date()
    if d.weekday() >= 5:      # weekend release -> Monday is the reaction day
        return next_weekday(d)
    if et.hour >= 16:         # after the close -> next session reacts
        return next_weekday(d)
    return d                  # before the open or during the day -> same session reacts

def voo_value():
    return S["voo_shares"] * px["VOO"]

def pos_value():
    return sum(p["shares"] * px[p["symbol"]] for p in S["positions"])

def equity():
    return S["cash"] + voo_value() + pos_value()

if "VIX" in px:  # optional: VIX level, recorded on each new trade for later analysis
    S["vix"] = px.pop("VIX")

# ---- 1. mark to market --------------------------------------------------------------
for p in S["positions"]:
    c = px[p["symbol"]]
    p["days_held"] += 1
    p["hi_close"] = max(p["hi_close"], c)
    p["lo_close"] = min(p["lo_close"], c)

# ---- 2. exits -----------------------------------------------------------------------
switching = S.get("rules_version", "v1") != RULES
keep = []
for p in S["positions"]:
    c = px[p["symbol"]]
    sc = screen.get(p["symbol"])
    reason = None
    if switching:
        reason = f"Switched to {RULES} rules"
    elif USE_PRICE_STOP and c <= p["stop"]:
        reason = "Stop hit"
    elif sc is not None and sc.get("passes") in (False, "false"):
        reason = "Failed weekly screen: " + (sc.get("why_not") or "gates")
    elif p["days_held"] >= MAX_HOLD:
        reason = "Six-month max hold"
    elif p["days_held"] >= TIME_STOP and c <= p["fill"]:
        reason = "Time stop: not working after 8 weeks"
    if reason:
        exit_px = c * (1 - SLIP)
        S["cash"] += p["shares"] * exit_px
        r_mult = (exit_px - p["fill"]) / (p["fill"] - p["stop0"])
        ret = (exit_px / p["fill"] - 1) * 100
        spy_ret = (px["SPY"] / p["spy_at_entry"] - 1) * 100
        S["trades"].append({**{k: p[k] for k in ("id", "symbol", "group", "setup", "entry_date", "fill", "shares",
                                                    "stop0", "atr", "features", "spy_at_entry")},
                            "exit_date": D, "exit_price": round(exit_px, 2), "exit_reason": reason,
                            "rules": p.get("rules", "v1"),
                            "r_multiple": round(r_mult, 2), "ret_pct": round(ret, 2),
                            "spy_ret_pct": round(spy_ret, 2), "excess_pct": round(ret - spy_ret, 2),
                            "days_held": p["days_held"],
                            "mfe_pct": round((p["hi_close"] / p["fill"] - 1) * 100, 2),
                            "mae_pct": round((p["lo_close"] / p["fill"] - 1) * 100, 2),
                            "pnl": round(p["shares"] * (exit_px - p["fill"]), 2)})
        ev(p["symbol"], "Sell", f"{reason}. {p['shares']:.2f} sh at ${exit_px:,.2f} ({ret:+.1f}%, {r_mult:+.2f}R)")
    else:
        keep.append(p)
S["positions"] = keep
if switching:
    S["rules_version"] = RULES
    S["rules_since"] = D
    S["watch"] = []                 # rebuilt from the next v2 screen
    S["finalists_url"] = None
    ev("", "Rules", f"Switched to {RULES}: 30% swing sleeve, strength gate 80, no sector cap, no price stops")

# ---- 3. trail stops (v1 only) ------------------------------------------------------
for p in (S["positions"] if USE_PRICE_STOP else []):
    new = p["hi_close"] - TRAIL_ATR * p["atr"]
    if new > p["stop"]:
        p["stop"] = round(new, 2)

# ---- 4. rebuild the watchlist when a new weekly screen lands -------------------------
held = {p["symbol"] for p in S["positions"]}
if A.finalists_url != S.get("finalists_url"):
    old = {(w["symbol"], w["setup"]): w for w in S["watch"]}
    setup_of = {"Actionable": "actionable", "Wait for earnings": "earnings", "Wait for pullback": "pullback"}
    new_watch = []
    for f in sorted(fin, key=lambda r: r["rank"]):
        if f["symbol"] in held:
            continue
        if f.get("rules_version", "v1") != RULES:
            continue   # only a screen built with the same rules version can feed this engine
        setup = setup_of.get(f["status"])
        if not setup:
            continue
        w = old.get((f["symbol"], setup), {})
        atr = f.get("atr") or None
        new_watch.append({
            "symbol": f["symbol"], "group": f.get("group"), "setup": setup, "rank": f["rank"], "added": w.get("added", D),
            "entry_lo": f["entry_lo"], "entry_hi": f["entry_hi"], "ma50": f["ma50"], "atr": atr,
            "stop_ref": f["stop"], "earnings": f.get("earnings"), "pre_close": w.get("pre_close"),
            "reaction_close": w.get("reaction_close"),
            "features": {k: f.get(k) for k in ("score", "rs_pct", "trend", "ext_50", "off_high", "eps_surprise",
                                                 "target_upside", "sentiment", "days_to_earnings", "status",
                                                 "insider_flag", "insider_buys_90d")},
        })
    dropped = [w["symbol"] for w in S["watch"] if (w["symbol"], w["setup"]) not in {(n["symbol"], n["setup"]) for n in new_watch}]
    for s in dropped:
        ev(s, "Unwatch", "No longer a finalist with that setup in this week's screen")
    S["watch"] = new_watch
    S["finalists_url"] = A.finalists_url
    ev("", "New screen", f"Watchlist rebuilt from {len(new_watch)} finalists not already held")

# ---- 5. entries ---------------------------------------------------------------------
def buy(w, setup_label, why):
    c = px[w["symbol"]]
    fill = c * (1 + SLIP)
    atr = w.get("atr")
    stop = fill - STOP_ATR * atr if atr else w["ma50"] * 0.98
    if stop >= fill:
        stop = fill * 0.92
    eq = equity()
    sleeve = SLEEVE * eq
    rps = fill - stop
    shares = RISK * sleeve / rps
    shares = min(shares, MAX_W * sleeve / fill)
    if len(S["positions"]) >= MAX_POS:
        ev(w["symbol"], "Skip", "Already holding the maximum of 10 names")
        return False
    room = sleeve - pos_value()
    if room < 100:
        ev(w["symbol"], "Skip", f"Swing sleeve full (stocks ${pos_value():,.0f} of ${sleeve:,.0f})")
        return False
    shares = min(shares, room / fill)
    cost = shares * fill
    need = cost - S["cash"]
    if need > 0:
        voo_px = px["VOO"] * (1 - SLIP)
        sell = min(S["voo_shares"], need / voo_px)
        S["voo_shares"] -= sell
        S["cash"] += sell * voo_px
        if S["cash"] + 1e-6 < cost:
            shares = S["cash"] / fill
            cost = shares * fill
    if shares <= 0:
        return False
    S["cash"] -= cost
    tid = S["next_trade_id"]; S["next_trade_id"] += 1
    S["positions"].append({"id": tid, "symbol": w["symbol"], "group": w.get("group"), "setup": setup_label,
                           "entry_date": D, "fill": round(fill, 4), "shares": round(shares, 4),
                           "stop0": round(stop, 2), "stop": round(stop, 2), "atr": atr or round(fill * 0.03, 2),
                           "hi_close": c, "lo_close": c, "days_held": 0, "spy_at_entry": px["SPY"], "rules": RULES,
                           "features": {**w["features"], "vix_regime": S.get("vix")}})
    ev(w["symbol"], "Buy", f"{setup_label}: {shares:.2f} sh at ${fill:,.2f}, risk reference ${stop:,.2f} (no price stop in v2), ${cost:,.0f} ({cost / eq * 100:.1f}% of the book, {cost / sleeve * 100:.1f}% of the sleeve). {why}")
    return True

remaining = []
for w in sorted(S["watch"], key=lambda r: r["rank"]):
    sym = w["symbol"]
    if sym in {p["symbol"] for p in S["positions"]}:
        continue
    c = px.get(sym)
    if c is None:
        remaining.append(w); continue
    if w["setup"] == "actionable":
        if c < w["stop_ref"]:
            ev(sym, "Unwatch", f"Closed ${c:,.2f}, under the planned stop before entry"); continue
        if w["entry_lo"] * 0.99 <= c <= w["entry_hi"] * 1.02:
            if buy(w, "Actionable", "Close inside the buy zone."):
                continue
        remaining.append(w)
    elif w["setup"] == "pullback":
        days = (dt.date.fromisoformat(w["earnings"]) - today).days if w.get("earnings") else 99
        if c < w["entry_lo"] * 0.97:
            ev(sym, "Unwatch", f"Fell through the buy zone to ${c:,.2f}"); continue
        if c <= w["entry_hi"] and 0 <= days <= 3:
            remaining.append(w); continue  # wait until after the report
        if c <= w["entry_hi"]:
            if buy(w, "Pullback", f"Pulled back to ${c:,.2f}, inside the zone near the 50-day."):
                continue
        remaining.append(w)
    elif w["setup"] == "earnings":
        iso = earn.get(sym)
        stored = dt.date.fromisoformat(w["reaction_day"]) if w.get("reaction_day") else None
        if stored and today >= stored:
            rday = stored  # the report has happened; the calendar may already show next quarter's date
        elif iso:
            rday = reaction_day(iso)
        else:
            remaining.append(w); continue
        if today < rday:
            w["pre_close"] = c; w["reaction_day"] = rday.isoformat(); remaining.append(w); continue
        if today == rday:
            w["reaction_close"] = c; w["reaction_day"] = rday.isoformat()
            if w.get("pre_close"):
                ev(sym, "Earnings", f"Reaction day close ${c:,.2f} vs ${w['pre_close']:,.2f} before the report ({(c / w['pre_close'] - 1) * 100:+.1f}%)")
            remaining.append(w); continue
        # session after the reaction day (or later, if a run was missed): decide once
        pre = w.get("pre_close")
        if pre is None:
            ev(sym, "Unwatch", "Missed the close before the report, so the reaction can't be judged"); continue
        if c > pre and c > w["ma50"]:
            if buy(w, "Post-earnings", f"Held the report: ${c:,.2f} vs ${pre:,.2f} before it, above the 50-day."):
                continue
            remaining.append(w)
        else:
            ev(sym, "Unwatch", f"Failed post-earnings check: ${c:,.2f} vs ${pre:,.2f} before the report")
S["watch"] = remaining

# ---- 6. sweep cash into VOO ---------------------------------------------------------
if S["cash"] > 1:
    voo_fill = px["VOO"] * (1 + SLIP)
    add = S["cash"] / voo_fill
    S["voo_shares"] += add
    S["cash"] = 0.0

# ---- 7. record ---------------------------------------------------------------------
eq = equity()
S["equity"].append({"date": D, "equity": round(eq, 2), "spy_bench": round(S["spy_bench_shares"] * px["SPY"], 2),
                    "voo_value": round(voo_value(), 2), "positions_value": round(pos_value(), 2), "cash": round(S["cash"], 2)})
S["last_date"] = D
S["last_prices"] = px
S["events"] = S["events"][-500:]  # keep the state document small
json.dump(S, open(A.out_state, "w"), indent=1)

# ---- 8. rows for the dashboard -------------------------------------------------------
rows = []
for e in S["equity"]:
    rows.append({"kind": "equity", **e})
for p in S["positions"]:
    c = px[p["symbol"]]
    rows.append({"kind": "position", "id": p["id"], "symbol": p["symbol"], "setup": p["setup"], "entry_date": p["entry_date"],
                 "fill": round(p["fill"], 2), "shares": round(p["shares"], 2), "close": c,
                 "value": round(p["shares"] * c, 2), "weight_pct": round(p["shares"] * c / eq * 100, 1),
                 "pnl": round(p["shares"] * (c - p["fill"]), 2), "ret_pct": round((c / p["fill"] - 1) * 100, 2),
                 "stop": p["stop"], "to_stop_pct": round((p["stop"] / c - 1) * 100, 1),
                 "r_open": round((c - p["fill"]) / (p["fill"] - p["stop0"]), 2), "days_held": p["days_held"]})
rows.append({"kind": "position", "id": 0, "symbol": "VOO", "setup": "Idle cash", "entry_date": S["inception"],
             "fill": None, "shares": round(S["voo_shares"], 2), "close": px["VOO"], "value": round(voo_value(), 2),
             "weight_pct": round(voo_value() / eq * 100, 1), "pnl": None, "ret_pct": None, "stop": None,
             "to_stop_pct": None, "r_open": None, "days_held": None})
for w in S["watch"]:
    trig = {"actionable": f"Close between ${w['entry_lo'] * 0.99:,.2f} and ${w['entry_hi'] * 1.02:,.2f}",
            "pullback": f"Close at or under ${w['entry_hi']:,.2f} (no earnings within 3 days)",
            "earnings": "After the report: close above the pre-report close and the 50-day, one session after the reaction"}[w["setup"]]
    rows.append({"kind": "watch", "symbol": w["symbol"], "setup": w["setup"].capitalize(), "rank": w["rank"],
                 "close": px.get(w["symbol"]), "trigger": trig, "earnings": w.get("earnings"),
                 "pre_close": w.get("pre_close"), "added": w["added"]})
for t in S["trades"]:
    f = t.get("features") or {}
    rows.append({"kind": "trade", "id": t["id"], "symbol": t["symbol"], "setup": t["setup"], "entry_date": t["entry_date"],
                 "exit_date": t["exit_date"], "fill": round(t["fill"], 2), "exit_price": t["exit_price"],
                 "ret_pct": t["ret_pct"], "excess_pct": t["excess_pct"], "r_multiple": t["r_multiple"],
                 "days_held": t["days_held"], "exit_reason": t["exit_reason"], "mfe_pct": t["mfe_pct"],
                 "mae_pct": t["mae_pct"], "pnl": t["pnl"], "score": f.get("score"), "rs_pct": f.get("rs_pct"),
                 "ext_50": f.get("ext_50"), "group": t.get("group")})
for e in S["events"][-200:]:
    rows.append({"kind": "event", **e})
json.dump(rows, open(A.out_rows, "w"))

print(f"{D}: equity ${eq:,.2f} | SPY bench ${S['equity'][-1]['spy_bench']:,.2f} | positions {len(S['positions'])} "
      f"| watching {len(S['watch'])} | VOO {S['voo_shares']:.2f} sh | closed trades {len(S['trades'])}")
for e in [e for e in S["events"] if e["date"] == D]:
    print(" -", e["symbol"], e["action"], e["detail"])
