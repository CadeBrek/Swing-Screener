# Daily paper trading run

Schedule: weekdays 6:23 pm ET (`CRON_TZ=America/New_York 23 18 * * 1-5`)
Connectors: FMP, Bigdata.com, Alpha Vantage, Claude Docs
Runs: `engines/engine.py` (share book), then `engines/options.py` (calls book)

## Prompt

Run today's session of Cade's $50,000 paper portfolio, which trades the signals of his weekly swing screener, and its calls version (an options book that mirrors every share trade with a ~0.70-delta call). Paper dashboard: https://claude.ai/artifact/BPQJfrJd3xztYfGUF5ZmV9. Screener dashboard: https://claude.ai/artifact/ULtzUMAxM2BaXZmpGbamzU. All trading rules live in the engine code stored on the paper dashboard. Run that code; never make trades by your own judgment or change the rules. Work in a scratch folder. TODAY = today's date in America/New_York (YYYY-MM-DD).

1. Load. ArtifactData get on the paper dashboard: collection "agent", doc_id "state", and collection "agent", doc_id "engine". Note each version. Write the state document's data object to state.json, and the engine's "text" field to engine.py. If state.last_date >= TODAY, stop: today already ran.

2. Screener files. ArtifactData get on the SCREENER dashboard: collection "datasets", doc_ids "finalists", "screen" and "regime". From each source.url take the 32-character id after /_blob/ and download it with the Artifact tool (action "read", url = screener dashboard, path = that id). Save them as finalists.json, screen.json and regime.json. Keep finalists' full source.url string as FINALISTS_URL.

3. Prices. Symbols = every symbol in state.positions, every finalist symbol, plus VOO and SPY. Look up rp ids in state.rp_ids; for any missing one, call Bigdata.com find_securities with the ticker and add the id to state.rp_ids in state.json. Make ONE bigdata_portfolio_tearsheet call with all the ids and metrics ["PRICE", "PRICE_CHANGE_1D"]. Market-closed check: if SPY's price equals state.last_prices.SPY and its 1-day change is 0, the market was closed today (holiday). Then stop without changing anything and without messaging. Write prices.json = {"SYM": price, ...} for every symbol, plus "VIX" = the value of the regime.json row whose metric is "VIX". If any held or finalist symbol has no price, stop and report which one. The engine needs every one.

4. Earnings. One bigdata_events_calendar call: calendar_type corporate_calendar, categories ["earnings-call"], rp_entity_ids = the finalists' ids, start_date = TODAY minus 7 days, end_date = TODAY plus 100 days. Write earnings.json = {"SYM": event_datetime} using each symbol's earliest event on or after TODAY minus 3 days.

5. Run the share engine: python3 engine.py --state state.json --date TODAY --prices prices.json --finalists finalists.json --screen screen.json --earnings earnings.json --finalists-url FINALISTS_URL --out-state state.new.json --out-rows paper.json
If it errors, fix only your input files (never engine.py) and rerun once. If it still fails, stop and report the error without saving anything.

5b. Run the options book. ArtifactData get on the paper dashboard: collection "agent", doc_id "options_engine" (write its "text" field to options.py) and collection "agent", doc_id "options_state". If options_state exists, note its version and write its data object to options_state.json; if it does not exist yet, this is the book's first day: leave out --ostate below. Then run:
python3 options.py --ostate options_state.json --shares-state state.new.json --date TODAY --prices prices.json --regime regime.json --rows paper.json --out-ostate options_state.new.json
It adds the options rows to paper.json in place. If it errors, fix only your input files (never options.py) and rerun once. If it still fails, skip the options book today (do NOT save options_state), continue with the share book, and mention the error in the report. Never let an options failure block the share book.

6. Save. ArtifactData set on the paper dashboard: collection "agent", doc_id "state", file_path state.new.json, if_version = the version from step 1. If step 5b succeeded: ArtifactData set collection "agent", doc_id "options_state", file_path options_state.new.json, with if_version = its version from 5b (omit if_version only on the first day, when the document did not exist). Upload paper.json with the Artifact tool (action "publish", url = paper dashboard, asset true, file_path paper.json). ArtifactData get collection "datasets" doc_id "paper" (note its version and old source.url), then update it with source {kind:"file", url:<new /_blob/ url>, name:"paper.json"} and updated {at:<ISO time now>, by:"Claude (paper trading run)"}. Then delete the old asset (Artifact action "delete", url = paper dashboard, path = old blob id).

7. Report. Both engines print today's actions. If there was any Buy, Sell, or Unwatch from the share engine, any Buy call, Sell call or Roll call from the options book, or a new screen was loaded, send Cade one short message (SendUserMessage): each action in plain words with the price (for calls: strike, expiry and cost next to what the shares cost; on a sell, call P&L next to share P&L), then share-book value and return since start vs the S&P 500, and the calls version's value vs the share book. Under 130 words. Option prices are modeled, and these are simulated paper trades, not advice. If nothing happened, send no message and end with one line of status.
