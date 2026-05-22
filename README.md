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
- `CM`: first close after `突破BM` that is higher than the previous 5 closes and next 5 closes.
- `C`: first close after `CM` that is lower than the previous 5 closes and next 5 closes.
- `D`: first close above `CM_price` before structure failure.
- `B_FAIL`: after `B`, close falls below `B_price * 0.95`.
- `C_FAIL`: after `C`, close falls below `C_price * 0.95`.

All structure comparisons use `close`.

## Files

- `fetch_sp500_2026_and_mark.py`: main data/update/scan pipeline. Despite the historical filename, it now supports both the default S&P 500 universe and custom stock lists. PNG charts are optional manual validation output.
- `ad_structure_v05_core.py`: scanner and structure evaluator.
- `discord_signal_push.py`: pushes BM-break alerts to Discord with local dedupe cache.
- `credentials.example.py`: local credential template.
- `requirements.txt`: cloud/local Python dependencies.
- `symbols.example.csv`: example custom stock universe file.

Ignored local/runtime files:

- `credentials.py`
- `data/`
- `outputs/`

## Cloud Setup

Use Python 3.11+.

```bash
cd /data/UpBottom
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

The default dataset name is `stocks_2025_10`, and the default start date is `2025-10-01`. Runtime outputs go under:

```text
data/stocks_2025_10/
outputs/stocks_2025_10/
```

If your repo is not located at `/data/UpBottom`, either run commands from the repo directory or set:

```bash
export UPBOTTOM_ROOT="/your/path/UpBottom"
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
outputs/stocks_2025_10/sp500_metadata.csv
```

For custom lists, metadata is cached in:

```text
outputs/stocks_2025_10/stock_metadata.csv
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

Charts are written to:

```text
outputs/stocks_2025_10/charts/
```

The scan CSV always writes `outputs/stocks_2025_10/ad_signals.csv`. Its `chart_file` column is populated only when `--render-charts` is used.

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
outputs/stocks_2025_10/discord_push_cache.json
```

Cache key:

```text
symbol | timeframe | golden_A_time | golden_B_time | BM_break_time
```

Preview without sending:

```bash
python discord_signal_push.py --timeframe 4h --dry-run
```

Force resend:

```bash
python discord_signal_push.py --timeframe 4h --force
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
UPBOTTOM_ROOT=/data/UpBottom
UPBOTTOM_DATASET=stocks_2025_10

# 4h midday bar: market 09:30-13:30, run after 13:30 ET.
45 13 * * 1-5 cd /data/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 4h >> logs/upbottom_4h_midday.log 2>&1

# 4h close bar: run after market close.
20 16 * * 1-5 cd /data/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 4h >> logs/upbottom_4h_close.log 2>&1

# Daily bar: run after market close and after the 4h close job.
40 16 * * 1-5 cd /data/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 && python discord_signal_push.py --timeframe 1day >> logs/upbottom_1day_close.log 2>&1

# Weekly metadata refresh. This refreshes the default S&P 500-compatible universe from State Street SPY holdings, with DataHub CSV fallback.
15 10 * * 6 cd /data/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2 >> logs/upbottom_metadata.log 2>&1
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
- Chinese name
- sector
- sub-industry

The default S&P 500-compatible universe is cached in `sp500_metadata.csv` after it is generated from State Street SPY holdings or the DataHub fallback. Weekly refresh is usually enough. Custom-list metadata comes from your `symbols.csv`; update that file whenever your generated universe changes.

## Outputs

```text
data/stocks_2025_10/{1day,4h}/
outputs/stocks_2025_10/ad_signals.csv
outputs/stocks_2025_10/sp500_metadata.csv
outputs/stocks_2025_10/stock_metadata.csv
outputs/stocks_2025_10/discord_push_cache.json
outputs/stocks_2025_10/charts/    # only when --render-charts is used
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

Render charts for manual validation:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
```
