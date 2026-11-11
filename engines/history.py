"""Swing screener learning log. Appends this week's full screen to the history,
updates forward returns of earlier rows, and rebuilds the scorecard.
Usage: python3 history.py --history screen_history.json (omit on first run)
  --run YYYY-MM-DD --features features.json --finalists finalists.json --regime regime.json
  --prices prices.json --spy SPY_PRICE --rules-version v1
  --out-history screen_history.json --out-scorecard scorecard.json
features.json: one row per universe stock {symbol, rp_id, group, sector, passes, score, rs_pct,
  trend, ext_50, off_high, r1m, r3m, r6m, r1y, price, ma50, ma200}
prices.json: {SYMBOL: latest price} for every symbol in the history (old and new).
"""
import json, argparse, datetime as dt
HORIZONS = [4, 8, 13, 26]           # weeks
TOL = 3                             # days of slack around each checkpoint

ap = argparse.ArgumentParser()
for a in ['history','run','features','finalists','regime','prices','spy','rules_version','out_history','out_scorecard']:
    ap.add_argument('--' + a.replace('_','-'), dest=a)
A = ap.parse_args()
run = dt.date.fromisoformat(A.run); spy = float(A.spy)
hist = json.load(open(A.history)) if A.history else []
prices = {k: float(v) for k, v in json.load(open(A.prices)).items()}
if any(r['run'] == A.run for r in hist):
    raise SystemExit('history already has run ' + A.run)

# 1. update earlier rows with this week's price
missing = sorted({r['symbol'] for r in hist if r['symbol'] not in prices})
for r in hist:
    p = prices.get(r['symbol'])
    if p is None: continue
    days = (run - dt.date.fromisoformat(r['run'])).days
    r['last_price'] = p; r['last_spy'] = spy; r['last_date'] = A.run
    r['min_close'] = min(r.get('min_close') or r['price'], p)
    if r.get('stop') is not None:
        r['stop_hit'] = bool(r.get('stop_hit')) or p < r['stop']
    for h in HORIZONS:
        k = f'ret_{h}w'
        if r.get(k) is None and days >= 7 * h - TOL:
            r[k] = round((p / r['price'] - 1) * 100, 2)
            r[f'spy_{h}w'] = round((spy / r['spy_price'] - 1) * 100, 2) if r.get('spy_price') else None

# 2. append this week's screen
feats = json.load(open(A.features))
fins = {f['symbol']: f for f in json.load(open(A.finalists))}
reg = {g['metric']: g['value'] for g in json.load(open(A.regime))}
for f in feats:
    s = f['symbol']; F = fins.get(s, {})
    row = {k: f.get(k) for k in ['symbol','rp_id','group','sector','score','rs_pct','trend','ext_50','off_high','r1m','r3m','r6m','r1y','price','ma50','ma200']}
    row.update(run=A.run, rules_version=A.rules_version, passes=bool(f['passes']) if not isinstance(f['passes'], str) else f['passes'] == 'true',
               finalist=s in fins, rank=F.get('rank'), status=F.get('status'), insider_flag=F.get('insider_flag'),
               atr=F.get('atr'), stop=F.get('stop'), entry_hi=F.get('entry_hi'), target_upside=F.get('target_upside'),
               eps_surprise=F.get('eps_surprise'), days_to_earnings=F.get('days_to_earnings'),
               spy_price=spy, vix=reg.get('VIX'), spy_3m=reg.get('S&P 500 (SPY) 3-month'),
               mom_3m=reg.get('Momentum factor vs S&P, 3-month'),
               last_price=row['price'], last_spy=spy, last_date=A.run, min_close=row['price'], stop_hit=False if s in fins else None)
    for h in HORIZONS: row[f'ret_{h}w'] = None; row[f'spy_{h}w'] = None
    hist.append(row)
json.dump(hist, open(A.out_history, 'w'), separators=(',', ':'))

# 3. scorecard
def stats(rows, h):
    m = [r for r in rows if r.get(f'ret_{h}w') is not None and r.get(f'spy_{h}w') is not None]
    if not m: return 0, None, None
    ex = [r[f'ret_{h}w'] - r[f'spy_{h}w'] for r in m]
    return len(m), round(sum(ex) / len(ex), 2), round(100 * sum(e > 0 for e in ex) / len(ex))
P = [r for r in hist if r.get('passes')]
FN = [r for r in hist if r.get('finalist')]
cohorts = [
 ('Picked vs not', 'Finalists', FN),
 ('Picked vs not', 'Passed, not picked', [r for r in P if not r.get('finalist')]),
 ('Picked vs not', 'Failed the gates', [r for r in hist if r.get('passes') is False]),
 ('Strength rank (passers)', 'Strength rank 60-79', [r for r in P if r.get('rs_pct') is not None and r['rs_pct'] < 80]),
 ('Strength rank (passers)', 'Strength rank 80-100', [r for r in P if r.get('rs_pct') is not None and r['rs_pct'] >= 80]),
 ('Stretch above 50-day (passers)', 'Under 5% above 50-day', [r for r in P if r.get('ext_50') is not None and r['ext_50'] < 5]),
 ('Stretch above 50-day (passers)', '5-12% above 50-day', [r for r in P if r.get('ext_50') is not None and 5 <= r['ext_50'] <= 12]),
 ('Stretch above 50-day (passers)', 'Over 12% above 50-day', [r for r in P if r.get('ext_50') is not None and r['ext_50'] > 12]),
 ('Finalist status', 'Actionable', [r for r in FN if r.get('status') == 'Actionable']),
 ('Finalist status', 'Wait for earnings', [r for r in FN if r.get('status') == 'Wait for earnings']),
 ('Finalist status', 'Wait for pullback', [r for r in FN if r.get('status') == 'Wait for pullback']),
 ('Insider flag (finalists)', 'Insider Buy or Strong buy', [r for r in FN if r.get('insider_flag') in ('Buy', 'Strong buy')]),
 ('Insider flag (finalists)', 'Insider Neutral', [r for r in FN if r.get('insider_flag') == 'Neutral']),
 ('Insider flag (finalists)', 'Insider Caution', [r for r in FN if r.get('insider_flag') == 'Caution']),
 ('Market backdrop (finalists)', 'Picked with VIX under 20', [r for r in FN if r.get('vix') is not None and r['vix'] < 20]),
 ('Market backdrop (finalists)', 'Picked with VIX 20 or higher', [r for r in FN if r.get('vix') is not None and r['vix'] >= 20]),
]
weeks = sorted({r['run'] for r in hist})
card = []
for sec, name, rows in cohorts:
    n4, e4, b4 = stats(rows, 4); n13, e13, b13 = stats(rows, 13)
    st = [r for r in rows if r.get('stop') is not None]
    card.append(dict(cohort=name, section=sec, observations=len(rows), n_4w=n4, excess_4w=e4, beat_4w=b4,
                     n_13w=n13, excess_13w=e13, beat_13w=b13,
                     stop_hits=sum(1 for r in st if r.get('stop_hit')) if st else None,
                     rules_version=A.rules_version, weeks_logged=len(weeks), first_week=weeks[0], as_of=A.run))
json.dump(card, open(A.out_scorecard, 'w'), indent=1)
print('history rows', len(hist), 'weeks', len(weeks), 'no price for', missing)
for c in card: print(c['cohort'], c['observations'], c['n_4w'], c['excess_4w'], c['n_13w'], c['stop_hits'])
