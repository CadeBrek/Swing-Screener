# Daily paper trading run

Schedule: weekdays 6:23 pm ET (`CRON_TZ=America/New_York 23 18 * * 1-5`)
Data: this repo's `data/daily/latest/`, built by the "Daily prices" GitHub Action (Yahoo Finance)
Runs: `engines/engine.py` (share book), then `engines/options.py` (calls book)

## Prompt

Run today's session of Cade's $50,000 paper portfolio, which trades the signals of his weekly swing screener, and its calls version (an options book that mirrors every share trade with a ~0.70-delta call). Paper dashboard: https://claude.ai/artifact/BPQJfrJd3xztYfGUF5ZmV9. Screener dashboard: https://claude.ai/artifact/ULtzUMAxM2BaXZmpGbamzU. All trading rules live in the engine code stored on the paper dashboard. Run that code; never make trades by your own judgment or change the rules. Prices and earnings dates come from Cade's GitHub repo CadeBrek/swing-screener; do NOT use Bigdata.com or Alpha Vantage (Cade is out of credits on both). Work in a scratch folder. TODAY = today's date in America/New_York (YYYY-MM-DD).

1. Load. ArtifactData get on the paper dashboard: collection "agent", doc_id "state", and collection "agent", doc_id "engine". Note each version. Write the state document's data object to state.json, and the engine's "text" field to engine.py. If state.last_date >= TODAY, stop: today already ran.

2. Screener files. ArtifactData get on the SCREENER dashboard: collection "datasets", doc_ids "finalists", "screen" and "regime". From each source.url take the 32-character id after /_blob/ and download it with the Artifact tool (action "read", url = screener dashboard, path = that id). Save them as finalists.json, screen.json and regime.json. Keep finalists' full source.url string as FINALISTS_URL.

3. Prices. Call add_repo with owner CadeBrek, repo swing-screener (access push), then `git clone --depth 1 https://github.com/CadeBrek/swing-screener repo`. Read repo/data/daily/latest/status.json.
- If status.date != TODAY, the GitHub Action has not run yet (scheduled 4:40 pm ET, 5:40 pm in daylight time; GitHub can run late). Trigger it: append one line with the current UTC time to repo/data/run-daily, commit "Run daily prices" and push. Then wait (Bash run_in_background with an until-loop that does `git -C repo pull -q` every 30 seconds) up to 25 minutes for status.date == TODAY. If it never arrives, read repo/data/logs-daily.txt, change nothing, message Cade in one sentence that prices were unavailable today, and stop.
- If status.market_open is false, the market was closed today (holiday). Stop without changing anything and without messaging.
- Copy repo/data/daily/latest/prices.json to prices.json (it already includes SPY, VOO, every finalist, every tracked symbol and "VIX"; if "VIX" is missing or null, set it to the value of the regime.json row whose metric is "VIX"). If any symbol in state.positions or finalists.json is missing from it, stop and report which one: the engine needs every one.
- Copy repo/data/daily/latest/earnings.json to earnings.json.

4. Run the share engine: python3 engine.py --state state.json --date TODAY --prices prices.json --finalists finalists.json --screen screen.json --earnings earnings.json --finalists-url FINALISTS_URL --out-state state.new.json --out-rows paper.json
If it errors, fix only your input files (never engine.py) and rerun once. If it still fails, stop and report the error without saving anything.

4b. Run the options book. ArtifactData get on the paper dashboard: collection "agent", doc_id "options_engine" (write its "text" field to options.py) and collection "agent", doc_id "options_state". If options_state exists, note its version and write its data object to options_state.json; if it does not exist yet, this is the book's first day: leave out --ostate below. Then run:
python3 options.py --ostate options_state.json --shares-state state.new.json --date TODAY --prices prices.json --regime regime.json --rows paper.json --out-ostate options_state.new.json
It adds the options rows to paper.json in place. If it errors, fix only your input files (never options.py) and rerun once. If it still fails, skip the options book today (do NOT save options_state), continue with the share book, and mention the error in the report. Never let an options failure block the share book.

5. Save. ArtifactData set on the paper dashboard: collection "agent", doc_id "state", file_path state.new.json, if_version = the version from step 1. If step 4b succeeded: ArtifactData set collection "agent", doc_id "options_state", file_path options_state.new.json, with if_version = its version from 4b (omit if_version only on the first day, when the document did not exist). Upload paper.json with the Artifact tool (action "publish", url = paper dashboard, asset true, file_path paper.json). ArtifactData get collection "datasets" doc_id "paper" (note its version and old source.url), then update it with source {kind:"file", url:<new /_blob/ url>, name:"paper.json"} and updated {at:<ISO time now>, by:"Claude (paper trading run)"}. Then delete the old asset (Artifact action "delete", url = paper dashboard, path = old blob id).

5b. Record for the reports. Write repo/data/paper/equity.json = one row per date from state.new.json's "equity" list: {date, equity, spy_bench, calls_equity} where calls_equity is that date's "equity" in options_state.new.json's "equity" list (null if missing; use the old options_state if 4b failed). Commit "Paper equity TODAY" and push (pull --rebase first if the push is rejected). If this fails, mention it in one line; it does not affect the trading.

6. Report. Both engines print today's actions. If there was any Buy, Sell, or Unwatch from the share engine, any Buy call, Sell call or Roll call from the options book, or a new screen was loaded, send Cade one short message (SendUserMessage): each action in plain words with the price (for calls: strike, expiry and cost next to what the shares cost; on a sell, call P&L next to share P&L), then share-book value and return since start vs the S&P 500, and the calls version's value vs the share book. Under 130 words. Option prices are modeled, and these are simulated paper trades, not advice. If nothing happened, send no message and end with one line of status.
