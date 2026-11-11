# Weekly swing trade screen

Schedule: Mondays 5:17 pm ET (`CRON_TZ=America/New_York 17 17 * * 1`)
Connectors: FMP, Bigdata.com, Alpha Vantage, Claude Docs
Rules version: v1 (see `rules.json`)

## Prompt

Refresh Cade's "Swing trade screener" dashboard: https://claude.ai/artifact/ULtzUMAxM2BaXZmpGbamzU (a Dashboard-type artifact). Screen for 1-6 month swing trades, rebuild its data files, update the pick log, and extend the learning log. Use today's date as the run date. Work in a scratch folder; write Python for all math (never compute numbers by hand).

STEP 1 - Universe. Call Bigdata.com bigdata_screen_companies with country US, market_cap_more_than 10000000000, price_more_than 10, volume_more_than 1000000, is_etf false, is_fund false, is_actively_trading true, limit 100. Drop any listing with under 6 months of history (e.g. a recent IPO; check ipo_date in the tearsheet). Keep each rp_entity_id.

STEP 2 - Price data. For EACH stock, call bigdata_company_tearsheet with that rp_entity_id, company_type "Public", sections ["company_overview"]. From price_performance take: current_price, year_high, year_low, price_avg_50, price_avg_200, and change_pct for 1M, 3M, 6M, 1Y. Save to data.csv with columns symbol,sector,price,hi52,lo52,ma50,ma200,r1m,r3m,r6m,r1y (sector = the industry, shortened). Do not use FMP for bulk quotes: Cade's FMP plan blocks most symbols.

STEP 3 - Market regime. Call bigdata_market_tearsheet. From it take SPY 1M/3M/6M/1Y change; VIX level and its 1M change; Momentum (MTUM) and Small-Cap (IJR) 3M excess vs S&P; 10-year Treasury yield; WTI crude price and YTD change; and the 11 SPDR sector ETFs' 3M and 6M change. If SPY's 1Y change or price is missing there, get them from bigdata_etf_tearsheet for SPY (rp_entity_id 672082).
- regime.json = rows {metric, value, unit, read} in this exact order: "S&P 500 (SPY) 3-month" (%), "S&P 500 (SPY) 6-month" (%), "VIX" (unit ""), "Momentum factor vs S&P, 3-month" (%), "Small caps vs S&P, 3-month" (%), "10-year Treasury yield" (%), "WTI crude" ($). The page reads row 1 as SPY 6-month and row 2 as VIX, so keep this order. "read" = a 2-6 word plain description.
- sectors.json = rows {sector, etf, r3m, r6m} with sector names: Energy, Health care, Consumer staples, Technology, Materials, Consumer discretionary, Communication services, Financials, Real estate, Industrials, Utilities.

STEP 4 - Score (exact rules, rules version v1; never change them yourself — rule changes only come from the monthly review with Cade's approval).
- skip1(long, r1m) = ((1+long/100)/(1+r1m/100)-1)*100.
- rs3 = skip1(r3m,r1m) - skip1(SPY3m,SPY1m); rs6 = same with 6M; rs12 = r1y - SPY1y.
- Percentile-rank each of rs3, rs6, rs12 across the universe (share of values <= v, x100).
- rs_score = 0.4*rs6_pct + 0.4*rs3_pct + 0.2*rs12_pct, then rs_pct = percentile rank of rs_score.
- Trend checks (one point each): price > ma200; ma50 > ma200; price > ma50; price >= 0.85*hi52. trend = 0-4.
- off_high = (price/hi52-1)*100; ext_50 = (price/ma50-1)*100; extended = ext_50 > 12.
- PASS only if price > ma200 AND ma50 > ma200 AND rs_pct >= 60. why_not lists the failed gates ("below 200-day", "50-day below 200-day", "RS rank < 60").
- final = 0.5*rs_pct + 0.35*(trend/4*100) + 0.15*max(0, 100 + off_high*4).
- screen.json = all stocks sorted by final desc, rows {symbol, sector, passes (boolean), score (1 decimal or null), rs_pct (int), trend, r3m, r6m, r1y, off_high (1 decimal), why_not ("" when passing)}. Keep exactly these fields: Cade's paper-trading run reads this file.

STEP 5 - Finalists. From PASSING stocks by final score, take the top 8, capped at 2 per group (Energy = refiners, oil & gas, integrated; Healthcare = pharma, biotech, devices, life-science tools, managed care; Tech = software, semis, hardware, networking, cybersecurity, consumer electronics, IT services; Financials = banks, payments, insurance, capital markets, asset managers; Staples; Industrials; Consumer; Materials; Utilities; Real estate; Communication). Assign a group to every universe stock (needed for the learning log).
- Call bigdata_portfolio_tearsheet on the finalists' rp_entity_ids with metrics PRICE, EPS, EPS_ESTIMATE, EPS_SURPRISE, PRICE_TARGET, SENTIMENT. Take eps surprise %, consensus price target, and sentiment, and check each PRICE matches the tearsheet price from STEP 2.
- Call bigdata_events_calendar (corporate_calendar, categories ["earnings-call"], their rp_entity_ids, start today, end +100 days) for each one's next earnings date.
- ATR: Alpha Vantage ATR (interval daily, time_period 14, datatype json) per finalist, ONE CALL AT A TIME at least 2 seconds apart. The free key allows only 25 calls a day. Take the most recent value (the preview's newest date is enough); if the result is saved to a file, read the latest date's ATR from it with python. If ATR is unavailable, use the 50-day fallback below.
- Insiders: after the ATR calls, Alpha Vantage INSIDER_TRANSACTIONS per finalist (symbol, from_date = today minus 90 days), ONE CALL AT A TIME at least 2 seconds apart (ATR + insider = 16 of the 25 daily calls). Parse the rows with python (transcribe the relevant rows into a script if the result is inline); if the result is saved to a file, read it from there. Use only transactions dated within the last 90 days.
  - Count ONLY open-market purchases: acquisitions coded P, or described as a purchase at market price, with a share price above 0. Ignore grants/awards (A), option exercises (M), shares withheld for taxes (F), gifts (G), and any row with a zero price.
  - insider_buys_90d = number of distinct insiders with an open-market purchase.
  - insider_flag (exactly one of these words):
    "Strong buy" = 2 or more distinct insiders bought, OR a CEO, CFO or President bought $100,000 or more in total.
    "Buy" = otherwise, at least 1 insider bought.
    "Caution" = no buys, and 3 or more distinct officers or directors made open-market sales (code S) totalling over $10,000,000, or a CEO/CFO sold more than 25% of their holdings where the data shows holdings after the trade.
    "Neutral" = everything else.
    "Unavailable" = the call failed, was rate-limited or returned nothing usable (including an empty list). Never guess, and never write "Neutral" when the data is missing.
  - insider_note = one plain sentence from the data: who bought or sold, dollar totals, and dates. Examples: "CFO bought $240k on Oct 2; no other open-market buys." / "No open-market buys in 90 days; sales look routine." / "Insider data unavailable this week."
  - The insider read is information only. It does NOT change score, rank, status, entry zone or stop.
- Status: extended -> "Wait for pullback" with entry_lo = ma50 and entry_hi = ma50*1.05. Otherwise, if earnings are within 14 days -> "Wait for earnings". Otherwise -> "Actionable". For both non-extended cases, entry_hi = price and entry_lo = price - ATR.
- stop = entry_hi - 2.5*ATR (fallback: ma50*0.98, stop_basis "2% under 50-day (ATR unavailable)"). stop_pct = (stop/entry_hi-1)*100.
- target_upside = (target/price-1)*100. days_to_earnings = days from today.
- News: for each finalist run one bigdata_search in fast mode, entity filter = its rp_entity_id, category news, the last 30 days, max_chunks 5. Write three short fields in plain English, paraphrased (never copy sentences), and only from the data and articles you retrieved: thesis (why it screens, 1-2 sentences, cite the actual momentum numbers), catch (main risk or reason to wait, 1-2 sentences), invalidation (a concrete price level or event).
- finalists.json = rows sorted by score, each with: symbol, group, sector, rank (1-8), score (1 dp), rs_pct, trend, price, ma50, ma200, hi52, off_high, ext_50, r1m, r3m, r6m, r1y, rs3, rs6, atr, stop, stop_pct, stop_basis, entry_lo, entry_hi, status, earnings (YYYY-MM-DD), days_to_earnings, eps_surprise, target, target_upside, sentiment, insider_flag, insider_buys_90d, insider_note, thesis, catch, invalidation. Round prices to 2 decimals.

STEP 6 - Pick log. Read the dashboard's datasets/picks_log document (ArtifactData get, collection "datasets", doc_id "picks_log") to find its current file url, then download that file with the Artifact tool (action read, url above, path = the 32-character id after /_blob/). For EVERY existing row, set price_now to that symbol's latest price (from this run's data; for symbols outside the universe use the prices fetched in STEP 6b), spy_now = SPY's latest price, and as_of = today. Then append one row per new finalist: {run: today, symbol, rank, status, insider_flag, price_at_pick: price, spy_at_pick: SPY price, price_now: price, spy_now: SPY price, as_of: today}. Never delete old rows.

STEP 6b - Learning log (every week; this is how the screen learns whether its rules work).
- ArtifactData get on the dashboard: collection "agent", doc_id "history_engine". Write its "text" field to history.py and note its "rules_version". Never edit history.py.
- ArtifactData get collection "datasets", doc_id "history"; download its file (Artifact read, path = the blob id) and save it as screen_history.json.
- features.json = one row per universe stock from STEP 4/5: {symbol, rp_id, group, sector, passes (boolean), score (1 dp), rs_pct (int), trend, ext_50 (1 dp), off_high (1 dp), r1m, r3m, r6m, r1y, price, ma50, ma200} (returns 2 dp, prices 2 dp).
- prices.json = {SYMBOL: latest price} for every symbol in the universe AND every distinct symbol in screen_history.json. Use this run's prices; for history symbols outside the universe, make ONE bigdata_portfolio_tearsheet call with their rp_id values from the history and metrics ["PRICE"].
- mkdir out, then run: python3 history.py --history screen_history.json --run TODAY --features features.json --finalists finalists.json --regime regime.json --prices prices.json --spy <SPY latest price> --rules-version <rules_version> --out-history out/screen_history.json --out-scorecard out/scorecard.json
- If it errors, fix only your input files and rerun once. If it still fails (or says the run already exists), skip this step, leave the old history and scorecard in place, and say so in the report. If out/screen_history.json is over 7 MB, mention that it needs splitting soon.

STEP 7 - Publish. Upload each of finalists.json, screen.json, regime.json, sectors.json, picks_log.json, out/screen_history.json and out/scorecard.json with the Artifact tool (action publish, url above, asset true, file_path, one file per call). Then make ONE ArtifactData batch call of "update" writes to collection "datasets", doc_ids finalists, screen, regime, sectors, picks_log, history, scorecard (leave out any whose file failed), each setting source {kind:"file", url:<the new /_blob/ url>, name:<file name>} and updated {at:<ISO time now>, by:"Claude (weekly screen)"}. Read each document first to get its version for if_version. Do NOT change files/index.html, dash/meta, agent/history_engine, or the page design. After the writes, delete the assets you replaced (the old /_blob/ ids of those datasets) with Artifact action delete, url above, path = asset id, but only if no dataset still points at them.

STEP 8 - Report. Send Cade a short message (SendUserMessage) with: the regime in one line; the finalists with each one's status; which are actionable now; any finalist with a "Buy", "Strong buy" or "Caution" insider flag, in a few words each (or "no notable insider activity"; say so if insider data was unavailable); any finalist that dropped out since last week (an open idea to reconsider exiting), and any logged pick whose weekly close fell below its stop; the pick log's average return vs the S&P 500 so far; and one line from the scorecard: if the "Finalists" row has 30 or more checked at 4 weeks, give its average vs the S&P next to "Passed, not picked"; otherwise say how many weeks are logged and that it's too early to judge. Under 180 words, plain prose. Note that this is a screen, not financial advice. If any step fails (a rate limit, a tool error), finish what you can, leave the old data for the failed dataset in place, and say what failed.
