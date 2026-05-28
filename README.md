# UpBottom

UpBottom is a research tool for finding bottom-divergence reversal structures in stock OHLCV data. It is not a trading bot, backtest engine, or investment advice system.

```text
download OHLCV data -> merge/dedupe local cache -> scan bottom divergence -> export CSV -> optional chart validation -> push Discord alerts
```

## Model

The core scanner lives in `ad_structure_v05_core.py`.

The bottom-divergence definition uses MACD DIF:

- For every golden cross, compute the minimum DIF value between that golden cross and the previous death cross.
- A bottom divergence exists when the second golden-cross close is lower than the first golden-cross close, while the second DIF minimum is higher than the first DIF minimum.
- Filters: price must be at least 5% lower, the current DIF minimum must exceed the previous DIF minimum by at least 5% of the previous DIF minimum's absolute value, current golden-cross DIF must be below zero, and DIF must not cross above zero between the two golden crosses.

Structure points:

- `GA`: first golden cross.
- `GB`: second golden cross.
- `B`: lowest close between `GA` and `GB`.
- `BM`: highest close between `GA` and `GB`.
- `突破BM`: first close after `B` that breaks above `BM_price`.
- `CM`: first close after `突破BM` that is higher than the previous 4 closes and next 4 closes.
- `C`: first close after `CM` that is lower than the previous 4 closes and next 4 closes.
- `D`: first close above `CM_price` before structure failure.
- `B_FAIL`: after `B`, close falls below `B_price * 0.95`.
- `C_FAIL`: after `C`, close falls below `C_price * 0.95`.

All structure comparisons use `close`.

Corporate action guard:

- Twelve Data requests explicitly use `adjust=splits`.
- Twelve Data daily/weekly/monthly stock prices are split-adjusted, but intraday data is not adjusted by the provider.
- To protect 4h scans, the scanner skips bottom-divergence candidates when the MACD warmup/window contains an adjacent close jump of `8x` or more.
- This guard is intended for reverse splits/splits and other corporate-action discontinuities, not ordinary volatility.
- For off-hours maintenance, `--repair-split-jumps` checks local 1day data for `8x` split-adjustment jumps after the normal incremental fetch. If any are found, only affected symbols are fully refreshed from `--start`.

## Files

- `fetch_sp500_2026_and_mark.py`: main data/update/scan pipeline. Despite the historical filename, it now supports both the default S&P 500 universe and custom stock lists. PNG charts are optional manual validation output.
- `ad_structure_v05_core.py`: scanner and structure evaluator.
- `discord_signal_push.py`: pushes BM-break alerts to Discord with local dedupe cache.
- `waterline_signal.py`: independent waterline entry-signal scanner. It does not import or modify bottom-divergence recognition logic.
- `waterline_strategy.py`: independent waterline strategy backtest. It consumes `waterline_signal.py` entries and manages anchors/exits.
- `credentials.example.py`: local credential template.
- `requirements.txt`: cloud/local Python dependencies.
- `symbols.example.csv`: example custom stock universe file.
- `us_1610_stock_universe.csv`: source universe used to build the US 1610 stock list.
- `symbols_us_1610.csv`: pipeline-ready US 1610 list with symbol metadata.
- `build_us_1610_symbols.py`: rebuilds `symbols_us_1610.csv` from the source universe and Nasdaq metadata.

Ignored local/runtime files:

- `credentials.py`
- `data/`
- `outputs/`

## Cloud Setup

Use Python 3.11+.

```bash
cd /root/UpBottom
python -m pip install -r requirements.txt
```

Copy your private `credentials.py` into the project root on the cloud machine:

```python
TWELVE_DATA_API_KEY = "your_twelve_data_api_key"
DISCORD_WEBHOOK_URL = "your_discord_webhook_url"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

`credentials.py` is git-ignored.

The code lives in `/root/UpBottom` by default. Runtime data and outputs live separately under `/data/UpBottom` by default. The default dataset name is `stocks_2025_10`, and the default start date is `2025-10-01`. Runtime outputs go under:

```text
/data/UpBottom/data/stocks_2025_10/
/data/UpBottom/outputs/stocks_2025_10/
```

If you intentionally want a different runtime data/output root, set:

```bash
export UPBOTTOM_RUNTIME_ROOT="/your/data/path/UpBottom"
```

You can also separate different universes with:

```bash
export UPBOTTOM_DATASET="my_universe_2025_10"
```

## Stock Universe

Default universe is S&P 500. No extra argument is needed. In other words, `--universe-source` defaults to `sp500`, so the normal run command should not include any universe argument:

```bash
python fetch_sp500_2026_and_mark.py
```

The default S&P 500 universe does not use Twelve Data's paid ETF composition endpoint. It first reuses the local metadata cache. If the cache is missing or `--refresh-metadata` is passed, it downloads the public State Street SPY holdings xlsx and parses the tickers. If that source fails, it falls back to the tested DataHub raw S&P 500 CSV.

You can provide a custom list:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols.csv
```

The checked-in US 1610 universe is ready to use:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols_us_1610.csv --start 2025-10-01 --overlap-days 10 --workers 2
```

To rebuild that list from `us_1610_stock_universe.csv` and fresh Nasdaq metadata:

```bash
python build_us_1610_symbols.py
```

Plain text format:

```text
AAPL
MSFT
BRK.B
```

CSV format:

```csv
symbol,source_symbol,english_name,chinese_name,sector,sub_industry
AAPL,AAPL,Apple Inc.,苹果,Information Technology,"Technology Hardware, Storage & Peripherals"
BRK-B,BRK.B,Berkshire Hathaway,伯克希尔,Financials,Multi-Sector Holdings
```

`source_symbol` is the provider symbol used for downloading. `symbol` is the safe local ID used in filenames and Discord alerts. If `symbol` is omitted, the script derives one automatically.

The State Street SPY holdings source is an ETF-holdings proxy, not an official S&P Dow Jones constituent feed. It can include cash/non-stock rows, which the script filters out, and it may differ from the official index because of fund reporting cadence or ETF construction. Twelve Data remains the default market data provider for OHLCV bars only.

For the default S&P 500-compatible universe, metadata is cached in:

```text
/data/UpBottom/outputs/stocks_2025_10/sp500_metadata.csv
```

For custom lists, metadata is cached in:

```text
/data/UpBottom/outputs/stocks_2025_10/stock_metadata.csv
```

## Data Cache

The pipeline is incremental by default.

For every symbol/timeframe:

```text
read local CSV
-> re-fetch from N days before the last local bar
-> merge by datetime
-> newer fetched row overwrites old local row
-> write clean CSV back to disk
-> scan from local organized data
```

Default overlap is 10 calendar days:

```bash
python fetch_sp500_2026_and_mark.py --overlap-days 10
```

This fills recent gaps and refreshes revised latest bars without re-downloading the full history every run.

For the initial default S&P 500-compatible cache from `2025-10-01`, run once without `--skip-fetch`:

```bash
python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2
```

This command uses S&P 500 because `--universe-source sp500` is the default.

For a custom universe:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols.csv --start 2025-10-01 --overlap-days 10 --workers 2
```

To download only selected intervals, pass `--fetch-timeframes`:

```bash
python fetch_sp500_2026_and_mark.py --start 2024-01-01 --fetch-timeframes 1day --workers 2
```

Off-hours split-adjustment maintenance:

```bash
python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 --repair-split-jumps
```

This does not call Twelve Data's paid `/splits_calendar` endpoint. It checks the already-downloaded 1day cache for adjacent close jumps of `8x` or more, then fully refreshes only affected symbols. Repair details are written to:

```text
/data/UpBottom/outputs/stocks_2025_10/split_jump_repairs.csv
```

Fast local rescan without downloading:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch
```

## Chart Validation

Batch/cloud runs do not render charts by default. This keeps the scheduled pipeline focused on data, scan results, and alerts.

When you want to manually inspect detected structures, run a local rescan with chart rendering enabled:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
```

Charts use Chinese labels such as `突破BM` and `底背离`. On a minimal Linux cloud image, install a CJK font before rendering if labels appear as boxes:

```bash
sudo apt-get update
sudo apt-get install -y fonts-noto-cjk
```

If no Chinese font is found, the script still renders charts but prints `chart_font_warning=...`.

Charts are written to:

```text
/data/UpBottom/outputs/stocks_2025_10/charts/
```

The scan CSV always writes `/data/UpBottom/outputs/stocks_2025_10/ad_signals.csv`. Its `chart_file` column is populated only when `--render-charts` is used.

## Waterline Strategy

Waterline is a separate research strategy from the bottom-divergence scanner. Keep signal detection and strategy execution separate:

- `waterline_signal.py` finds entry candidates only.
- `waterline_strategy.py` consumes those candidates and manages holding, anchor updates, and exits.
- Neither file changes `ad_structure_v05_core.py` or the bottom-divergence scan pipeline.

Required local data layout:

```text
/data/UpBottom/data/stocks_2025_10/1day/{SYMBOL}_1day_indicators.csv
/data/UpBottom/data/stocks_2025_10/1min/{SYMBOL}_1min_indicators.csv
/data/UpBottom/data/stocks_2025_10/1h/{SYMBOL}_1h_indicators.csv
```

All CSVs use the same columns as the existing OHLCV cache:

```csv
datetime,open,high,low,close,volume
```

The recommended research flow is on-demand, so you do not need to download 1min/1h data for the whole universe:

```text
daily data -> find signal-day candidates
candidate D+1 only -> download 1min bars -> confirm entry
confirmed entries only -> download later 1min and 1h bars -> backtest exit/anchors
```

`waterline_on_demand.py` processes symbols concurrently with `--workers`, but each symbol is handled in chronological order. If a later signal falls inside the same symbol's active holding period, that signal is skipped. This keeps per-symbol trades non-overlapping while still allowing multiple symbols to run in parallel.

Run the on-demand pipeline:

```bash
python waterline_on_demand.py \
  --symbols-file symbols_us_1610.csv \
  --daily-dir /data/UpBottom/data/us_1610_2024_01_daily/1day \
  --data-root /data/UpBottom/data/waterline_on_demand \
  --output-dir /data/UpBottom/outputs/waterline_on_demand \
  --start 2024-01-01
```

For a smaller first pass, limit the symbol set:

```bash
python waterline_on_demand.py \
  --symbols AAPL MSFT NVDA \
  --daily-dir /data/UpBottom/data/us_1610_2024_01_daily/1day \
  --data-root /data/UpBottom/data/waterline_on_demand \
  --output-dir /data/UpBottom/outputs/waterline_on_demand_smoke \
  --start 2025-01-01 \
  --end 2026-07-01 \
  --workers 2
```

This writes:

```text
waterline_candidates.csv
waterline_entries.csv
waterline_trades.csv
waterline_half_year_summary.csv
```

### Waterline Entry Signal

Signal day `D`:

```text
close_D > open_D
close_D - low_D > 1.2 * (high_D - close_D)
volume_D >= mean(volume of previous 10 trading days) * 2
```

Trade day is the next trading day `D+1`. The first version uses full-day minute confirmation and buys at the trade-day close:

```text
waterline = signal_day.close
above_ratio = count(D+1 minute close > waterline) / count(D+1 minute bars)
entry if above_ratio >= 0.8
entry_price = D+1 daily close
```

Run:

```bash
python waterline_signal.py --symbols MU
```

For a custom universe:

```bash
python waterline_signal.py --symbols-file symbols_us_1610.csv
```

Output:

```text
/data/UpBottom/outputs/stocks_2025_10/waterline_entries.csv
```

Important columns:

```text
symbol, signal_date, signal_close, volume_ratio, trade_date,
minute_above_ratio, entry_time, entry_price
```

### Waterline Strategy

Initial anchor:

```text
anchor_price = signal_day.close
```

Daily exit rule after entry:

```text
below_ratio = count(day minute close < anchor_price) / count(day minute bars)
exit if below_ratio >= 0.5
exit_price = that day's daily close
```

Anchor upgrade uses 1h closes:

```text
advance_pct = 0.15
define_bars = 15
confirm_bars = 30
```

When 15 consecutive 1h bars define a candidate box:

```text
box_high = max(close over the 15 bars)
box_low = min(close over the 15 bars)
```

The box is eligible only when:

```text
box_high >= current_anchor * 1.15
```

Then the next 30 consecutive 1h closes must all stay inside:

```text
box_low <= close_1h <= box_high
```

If confirmed:

```text
anchor_price = box_low
```

The implementation treats anchors as one-way structural stops: a confirmed `box_low` raises the anchor only when it is above the current anchor. It does not lower an existing anchor.

If a confirmation close breaks below `box_low`, the candidate box fails. If it closes above `box_high`, the stock is treated as continuing higher; the old anchor remains and the strategy keeps looking for a new eligible platform.

Each row in `waterline_entries.csv` is backtested independently in the first version. Position netting and portfolio-level capital allocation are intentionally out of scope.

Run after generating entries:

```bash
python waterline_strategy.py
```

Output:

```text
/data/UpBottom/outputs/stocks_2025_10/waterline_trades.csv
```

Important columns:

```text
symbol, trade_date, entry_price, initial_anchor, final_anchor,
anchor_updates, exit_date, exit_price, exit_below_ratio, return_pct, status
```

## Discord Alerts

After the scan, push alerts:

```bash
python discord_signal_push.py --timeframe 4h
python discord_signal_push.py --timeframe 1day
```

Candidate rule:

```text
BM break exists
and not B_FAIL
and not C_FAIL
and not STRUCTURE_FAILED
```

Push dedupe cache:

```text
/data/UpBottom/outputs/stocks_2025_10/discord_push_cache.json
```

Cache key:

```text
symbol | timeframe | golden_A_time | golden_B_time | BM_break_time
```

Alert messages include stock ID, English name, sector/sub-industry, structure points, and a Chinese progress label:

```text
突破后到高点过程中
到高点后回踩进行中
回踩结束但是没有二次突破
已经二次突破
```

Chinese stock names are not included in Discord alerts.

Preview without sending:

```bash
python discord_signal_push.py --timeframe 4h --dry-run
```

Large alert batches are split into Discord chunks. Runtime logs include:

```text
discord_chunks=N
discord_sending chunk=1/N alerts=M bytes=B symbols=...
discord_sent chunk=1/N http_status=204 pushed_so_far=M cache=...
discord_post_failed attempt=1/5 ...
```

Each successfully sent chunk is written to the dedupe cache immediately. If a later chunk fails after retries, the next run only retries the unsent alerts.

Force resend:

```bash
python discord_signal_push.py --timeframe 4h --force
```

Clear the Discord dedupe cache without sending alerts:

```bash
python discord_signal_push.py --clear-cache
```

## Full Automation

Recommended crontab uses New York time so market close timing automatically follows daylight saving time.

Edit crontab:

```bash
crontab -e
```

Example:

```cron
TZ=America/New_York
# 4h midday bar: market 09:30-13:30, run after 13:30 ET.
45 13 * * 1-5 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 4h >> /data/UpBottom/logs/upbottom_4h_midday.log 2>&1

# 4h close bar: run after market close.
20 16 * * 1-5 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 4h >> /data/UpBottom/logs/upbottom_4h_close.log 2>&1

# Daily bar: run after market close and after the 4h close job.
40 16 * * 1-5 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 1day >> /data/UpBottom/logs/upbottom_1day_close.log 2>&1

# Off-hours split-adjustment maintenance. Runs after Twelve Data corporate-action updates are likely to have settled.
30 3 * * 2-6 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 --repair-split-jumps >> /data/UpBottom/logs/upbottom_split_repair.log 2>&1

# Monthly metadata refresh: first Sunday of each month.
15 10 1-7 * 0 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2 >> /data/UpBottom/logs/upbottom_metadata.log 2>&1
```

Create the log directory once:

```bash
mkdir -p /data/UpBottom/logs
```

For a custom universe, add `--symbols-file /data/UpBottom/symbols.csv` to each `fetch_sp500_2026_and_mark.py` command. If you want the custom universe to have separate cache/output files, set a different dataset:

```cron
UPBOTTOM_DATASET=my_universe_2025_10
```

## Metadata Refresh

Metadata is used only for alert display:

- stock ID
- English name
- sector
- sub-industry

The default S&P 500-compatible universe is cached in `sp500_metadata.csv` after it is generated from State Street SPY holdings or the DataHub fallback. Weekly refresh is usually enough. Custom-list metadata comes from your `symbols.csv`; update that file whenever your generated universe changes.

## Outputs

```text
/data/UpBottom/data/stocks_2025_10/{1day,4h}/
/data/UpBottom/outputs/stocks_2025_10/ad_signals.csv
/data/UpBottom/outputs/stocks_2025_10/sp500_metadata.csv
/data/UpBottom/outputs/stocks_2025_10/stock_metadata.csv
/data/UpBottom/outputs/stocks_2025_10/split_jump_repairs.csv
/data/UpBottom/outputs/stocks_2025_10/discord_push_cache.json
/data/UpBottom/outputs/stocks_2025_10/charts/    # only when --render-charts is used
/data/UpBottom/outputs/stocks_2025_10/waterline_entries.csv
/data/UpBottom/outputs/stocks_2025_10/waterline_trades.csv
```

## Useful Commands

Smoke test:

```bash
python fetch_sp500_2026_and_mark.py --limit 5 --workers 2
```

Smoke test with custom universe:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols.example.csv --limit 2 --workers 2
```

Use Yahoo fallback:

```bash
python fetch_sp500_2026_and_mark.py --provider yahoo
```

Run off-hours split-adjustment repair:

```bash
python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 --repair-split-jumps
```

Render charts for manual validation:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
```

Run waterline entry scan and strategy backtest:

```bash
python waterline_signal.py --symbols MU
python waterline_strategy.py
```
