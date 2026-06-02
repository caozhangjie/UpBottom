# UpBottom

UpBottom is a research tool for finding bottom-divergence reversal structures in stock OHLCV data. It is not a trading bot, backtest engine, or investment advice system.

```text
download OHLCV data -> merge/dedupe local cache -> scan signals -> export CSV -> optional chart validation -> push Discord/Feishu alerts
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
- `bottom_divergence_signal_push.py`: pushes bottom-divergence BM-break alerts to Discord/Feishu with local dedupe cache.
- `bottom_divergence_d_trigger_push.py`: pushes daily bottom-divergence D second-breakout confirmations after the daily close.
- `waterline_signal.py`: independent waterline entry-signal scanner. It does not import or modify bottom-divergence recognition logic.
- `waterline_signal_push.py`: pushes waterline signal-day and trade-day alerts. It does not import or modify bottom-divergence recognition logic.
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
FEISHU_WEBHOOK_URL = "your_feishu_webhook_url"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

`credentials.py` is git-ignored.

The code lives in `/root/UpBottom` by default. Runtime data and outputs live separately under `/data/UpBottom` by default. The default dataset name is `stocks`, and the current production start date is `2024-01-01`. Runtime outputs go under:

```text
/data/UpBottom/data/stocks/
/data/UpBottom/outputs/stocks/
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
python fetch_sp500_2026_and_mark.py --symbols-file symbols_us_1610.csv --start 2024-01-01 --overlap-days 10 --workers 2
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
/data/UpBottom/outputs/stocks/sp500_metadata.csv
```

For custom lists, metadata is cached in:

```text
/data/UpBottom/outputs/stocks/stock_metadata.csv
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

For the initial default S&P 500-compatible cache from `2024-01-01`, run once without `--skip-fetch`:

```bash
python fetch_sp500_2026_and_mark.py --start 2024-01-01 --refresh-metadata --overlap-days 10 --workers 2
```

This command uses S&P 500 because `--universe-source sp500` is the default.

For a custom universe:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols.csv --start 2024-01-01 --overlap-days 10 --workers 2
```

To download only selected intervals, pass `--fetch-timeframes`:

```bash
python fetch_sp500_2026_and_mark.py --start 2024-01-01 --fetch-timeframes 1day --workers 2
```

Off-hours split-adjustment maintenance:

```bash
python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 --repair-split-jumps
```

This does not call Twelve Data's paid `/splits_calendar` endpoint. It checks the already-downloaded 1day cache for adjacent close jumps of `8x` or more, then fully refreshes only affected symbols. Repair details are written to:

```text
/data/UpBottom/outputs/stocks/split_jump_repairs.csv
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
/data/UpBottom/outputs/stocks/charts/
```

The scan CSV always writes `/data/UpBottom/outputs/stocks/ad_signals.csv`. Its `chart_file` column is populated only when `--render-charts` is used.

## Waterline Signals

Waterline is a separate signal model from the bottom-divergence scanner:

- `waterline_signal.py` finds entry candidates only.
- It does not calculate exits, returns, position state, or portfolio statistics.
- It does not change `ad_structure_v05_core.py` or the bottom-divergence scan pipeline.

Required local data layout:

```text
/data/UpBottom/data/stocks/1day/{SYMBOL}_1day_indicators.csv
/data/UpBottom/data/stocks/1min/{SYMBOL}_1min_indicators.csv
/data/UpBottom/data/stocks/1h/{SYMBOL}_1h_indicators.csv
```

The daily automation reuses the bottom-divergence fetch and does not fetch 1min data again. If local 1min data is missing, `waterline_signal.py` can still write signal-day candidates, but trade-day entries will be empty until minute data is available.

All CSVs use the same columns as the existing OHLCV cache:

```csv
datetime,open,high,low,close,volume
```

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
/data/UpBottom/outputs/stocks/waterline_candidates.csv
/data/UpBottom/outputs/stocks/waterline_entries.csv
```

Important columns:

```text
waterline_candidates.csv:
symbol, signal_date, signal_close, volume_ratio, trade_date, trade_close

waterline_entries.csv:
symbol, signal_date, signal_close, volume_ratio, trade_date, minute_above_ratio, entry_time, entry_price
```

## Alerts

Alert delivery is intentionally separate from signal calculation:

- Alert scripts only read scanner output such as `ad_signals.csv`, format structure progress, and send notifications.
- Alert scripts do not calculate entries, exits, returns, position state, or portfolio statistics.
- Keep operational alert dedupe caches separate from research outputs.

After the bottom-divergence scan, push alerts:

```bash
python bottom_divergence_signal_push.py --timeframe 4h
python bottom_divergence_signal_push.py --timeframe 1day
python bottom_divergence_signal_push.py --timeframe 1day --target feishu
python bottom_divergence_signal_push.py --timeframe 1day --target both
```

After the daily scan, push daily close-confirmed D second-breakout alerts:

```bash
python bottom_divergence_d_trigger_push.py
python bottom_divergence_d_trigger_push.py --target both
```

`bottom_divergence_d_trigger_push.py` defaults to `--timeframe 1day --date today`, where `today` is America/New_York today, not the server's local date. This is intentional for a Beijing-time server: when the job runs around 04:30-05:30 Beijing time after the US close, the selected US market date is usually the previous Beijing calendar date. Use `--date latest` to preview or backfill the latest D date in `ad_signals.csv`, or `--date YYYY-MM-DD` for an exact trading date.

After generating waterline CSVs, push waterline alerts with the separate waterline pusher:

```bash
python waterline_signal_push.py --alert-stage signal-day
python waterline_signal_push.py --alert-stage trade-day
```

`bottom_divergence_signal_push.py` defaults to Discord. `waterline_signal_push.py` defaults to Feishu because the current workflow uses dedicated Feishu groups for waterline signal-day and trade-day alerts. Use `--target both` only when you also want Discord copies.

Server credentials can be configured in `credentials.py` or environment variables:

```python
DISCORD_WEBHOOK_URL = "your_discord_webhook_url"
FEISHU_WEBHOOK_URL = "your_feishu_webhook_url"
WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL = "your_waterline_signal_day_feishu_webhook_url"
WATERLINE_TRADE_FEISHU_WEBHOOK_URL = "your_waterline_trade_day_feishu_webhook_url"
```

Bottom-divergence candidate rule:

```text
BM break exists
and not B_FAIL
and not C_FAIL
and not STRUCTURE_FAILED
```

Bottom-divergence D confirmation rule:

```text
timeframe == 1day
structure_status == D_TRIGGERED
D_time date == selected --date
```

Push dedupe cache:

```text
/data/UpBottom/outputs/stocks/discord_push_cache.json
```

Cache key:

```text
symbol | timeframe | golden_A_time | golden_B_time | BM_break_time
```

Waterline push dedupe cache:

```text
/data/UpBottom/outputs/stocks/waterline_push_cache.json
```

D second-breakout push dedupe cache:

```text
/data/UpBottom/outputs/stocks/bottom_divergence_d_push_cache.json
```

Waterline signal-day cache key:

```text
target | waterline-signal-day | symbol | signal_date
```

Waterline trade-day cache key:

```text
target | waterline-trade-day | symbol | signal_date | trade_date | entry_time
```

Alert messages include stock ID, English name, sector/sub-industry, structure points, and a Chinese progress label:

```text
突破后到高点过程中
到高点后回踩进行中
回踩结束但是没有二次突破
已经二次突破
```

Chinese stock names are not included in alert messages.

Waterline signal-day messages include stock ID, English name, sector/sub-industry, signal day, volume ratio, and next trade day. Waterline trade-day messages include minute waterline ratio and entry confirmation price.

Preview without sending:

```bash
python bottom_divergence_signal_push.py --timeframe 4h --dry-run
python bottom_divergence_d_trigger_push.py --dry-run
python waterline_signal_push.py --alert-stage signal-day --dry-run
python waterline_signal_push.py --alert-stage trade-day --dry-run
```

Large alert batches are split into message chunks. Runtime logs include:

```text
discord_chunks=N
discord_sending chunk=1/N alerts=M bytes=B symbols=...
discord_sent chunk=1/N http_status=204 pushed_so_far=M cache=...
discord_post_failed attempt=1/5 ...
```

Feishu runs use the same flow with `feishu_*` log prefixes. Each successfully sent chunk is written to the dedupe cache immediately. If a later chunk fails after retries, the next run only retries the unsent alerts for that target.

Force resend:

```bash
python bottom_divergence_signal_push.py --timeframe 4h --force
python bottom_divergence_d_trigger_push.py --force
python waterline_signal_push.py --alert-stage signal-day --force
```

Clear the alert dedupe cache without sending alerts:

```bash
python bottom_divergence_signal_push.py --clear-cache
python bottom_divergence_d_trigger_push.py --clear-cache
python waterline_signal_push.py --clear-cache
```

## Full Automation

Recommended crontab below assumes the server crontab runs in Beijing time and does not support `CRON_TZ`. The alert code itself uses America/New_York where market-date filtering matters, especially for daily D second-breakout confirmations.

Edit crontab:

```bash
crontab -e
```

Example:

```cron
# US daylight-saving time example, interpreted by a Beijing-time crontab.
# No TZ/CRON_TZ line is used here because this server's cron scheduler runs in Beijing time.
# Python uses the explicit Miniconda environment at /root/miniconda3/envs/myenv.
#
# 4h midday bar: US market 09:30-13:30 ET, run after 13:30 ET / 01:30 Beijing next day.
45 1 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 && /root/miniconda3/envs/myenv/bin/python bottom_divergence_signal_push.py --timeframe 4h >> /data/UpBottom/logs/upbottom_4h_midday.log 2>&1

# 4h close bar: run after US market close / about 04:00 Beijing next day during DST.
20 4 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 && /root/miniconda3/envs/myenv/bin/python bottom_divergence_signal_push.py --timeframe 4h >> /data/UpBottom/logs/upbottom_4h_close.log 2>&1

# Daily bar: shared fetch for bottom-divergence daily alerts and D confirmation.
# bottom_divergence_d_trigger_push.py uses America/New_York today, so this selects the just-closed US trading date.
40 4 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 && /root/miniconda3/envs/myenv/bin/python bottom_divergence_signal_push.py --timeframe 1day && /root/miniconda3/envs/myenv/bin/python bottom_divergence_d_trigger_push.py >> /data/UpBottom/logs/upbottom_1day_close.log 2>&1

# Waterline trade-day alerts. This job does not fetch data; it reads existing 1day/1min local cache.
# waterline_signal.py writes both waterline_candidates.csv and waterline_entries.csv.
0 5 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python waterline_signal.py --symbols-file symbols_us_1610.csv && /root/miniconda3/envs/myenv/bin/python waterline_signal_push.py --alert-stage trade-day >> /data/UpBottom/logs/waterline_trade_day.log 2>&1

# Waterline signal-day alerts. This only pushes the candidates generated by the 05:00 waterline scan.
30 5 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python waterline_signal_push.py --alert-stage signal-day >> /data/UpBottom/logs/waterline_signal_day.log 2>&1

# Off-hours split-adjustment maintenance. Runs after Twelve Data corporate-action updates are likely to have settled.
30 15 * * 2-6 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 --repair-split-jumps >> /data/UpBottom/logs/upbottom_split_repair.log 2>&1

# Monthly metadata refresh: first Sunday of each month, Beijing time.
15 22 1-7 * 0 cd /root/UpBottom && /root/miniconda3/envs/myenv/bin/python fetch_sp500_2026_and_mark.py --start 2024-01-01 --refresh-metadata --overlap-days 10 --workers 2 >> /data/UpBottom/logs/upbottom_metadata.log 2>&1
```

During US standard time, shift the market-close jobs one hour later (`02:45`, `05:20`, `05:40`, `06:00`, `06:30`) and the split-maintenance job to about `16:30`.

Create the log directory once:

```bash
mkdir -p /data/UpBottom/logs
```

For a custom universe, add `--symbols-file /data/UpBottom/symbols.csv` to each `fetch_sp500_2026_and_mark.py` command and to the daily `waterline_signal.py` command. If you want the custom universe to have separate cache/output files, set a different dataset:

```cron
UPBOTTOM_DATASET=my_universe
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
/data/UpBottom/data/stocks/{1day,4h}/
/data/UpBottom/data/stocks/{1min,1h}/     # needed only for waterline minute/hour validation
/data/UpBottom/outputs/stocks/ad_signals.csv
/data/UpBottom/outputs/stocks/sp500_metadata.csv
/data/UpBottom/outputs/stocks/stock_metadata.csv
/data/UpBottom/outputs/stocks/split_jump_repairs.csv
/data/UpBottom/outputs/stocks/discord_push_cache.json
/data/UpBottom/outputs/stocks/bottom_divergence_d_push_cache.json
/data/UpBottom/outputs/stocks/waterline_push_cache.json
/data/UpBottom/outputs/stocks/charts/    # only when --render-charts is used
/data/UpBottom/outputs/stocks/waterline_candidates.csv
/data/UpBottom/outputs/stocks/waterline_entries.csv
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
python fetch_sp500_2026_and_mark.py --start 2024-01-01 --overlap-days 10 --workers 2 --repair-split-jumps
```

Render charts for manual validation:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
```

Run waterline entry scan:

```bash
python waterline_signal.py --symbols MU
python waterline_signal_push.py --alert-stage signal-day
python waterline_signal_push.py --alert-stage trade-day
```
