# UpBottom

UpBottom is a daily US-stock signal scanner and Feishu alerting system. It scans daily OHLCV data for bottom-divergence structures and waterline signals, tracks bottom-divergence trade positions, and provides separate research backtests.

It is not an automated trading bot and it is not investment advice.

```text
daily data update -> bottom-divergence scan -> temporary 1min download -> Feishu pushes -> tmp cleanup
                                                    |
                                                    +-> research backtests
```

## What Runs In Production

The production system currently uses daily bars only. 4h fetching and bottom-divergence historical pushes are intentionally paused unless you run them manually.

Active scheduled jobs:

- Daily data fetch and scan: `daily_workflow.py --step fetch-scan`
- Shared temporary 1min download: `daily_workflow.py --step prepare-tmp`
- Bottom-divergence BM buy/sell push: `daily_workflow.py --step bottom-trade`
- Waterline signal-day/trade-day push: `daily_workflow.py --step waterline`
- Temporary 1min cleanup: `daily_workflow.py --step cleanup`
- Split-adjustment repair: `fetch_sp500_2026_and_mark.py --repair-split-jumps`
- Monthly metadata refresh: `fetch_sp500_2026_and_mark.py --refresh-metadata`

Manual-only jobs:

- Bottom-divergence historical BM-break push: `daily_workflow.py --step bottom-history`
- Chart rendering: `fetch_sp500_2026_and_mark.py --skip-fetch --render-charts`
- Research backtests: `bottom_backtest.py`, `waterline_backtest.py`

## Runtime Layout

Runtime data lives outside the repo. The default root is `/data/UpBottom`.

```text
/data/UpBottom/data/1day/              # long-term daily cache
/data/UpBottom/outputs/ad_signals.csv  # latest bottom-divergence scan
/data/UpBottom/outputs/push_state/     # push caches and open bottom positions
/data/UpBottom/outputs/backtests/      # research outputs
/data/UpBottom/tmp/1min/               # temporary production minute data
/data/UpBottom/logs/                   # cron logs
```

The code does not use a `{dataset}` directory layer anymore. If you need a different runtime root:

```bash
export UPBOTTOM_RUNTIME_ROOT="/your/path/UpBottom"
```

Do not store production 1min data under `/data/UpBottom/data`. Production 1min bars are downloaded once per workflow into `/data/UpBottom/tmp/1min/` and deleted after all push steps finish. Backtests use their own cache under `/data/UpBottom/outputs/backtest_minute_cache/1min/`.

## Files

Core scanner and data:

- `fetch_sp500_2026_and_mark.py`: data update, merge/dedupe, scan, optional chart rendering.
- `ad_structure_v05_core.py`: bottom-divergence model and structure evaluator.
- `waterline_signal.py`: waterline signal-day and trade-day confirmation logic.
- `constants.py`: runtime paths and Feishu webhook names.
- `credentials.example.py`: template for private credentials.

Production pushes:

- `daily_workflow.py`: robust step-based workflow for cron.
- `bottom_trade_push.py`: bottom-divergence BM buy/sell alerts and position state.
- `waterline_push.py`: waterline signal-day and trade-day alerts.
- `bottom_history_push.py`: manual historical bottom-divergence BM-break alerts.
- `bottom_common.py`, `push_utils.py`, `intraday_tmp.py`: shared production helpers.

Backtests:

- `bottom_backtest.py`: bottom-divergence BM/C/D entry comparison.
- `waterline_backtest.py`: waterline backtest.
- `backtest_common.py`: shared backtest paths, symbols, 1min cache, exit engine, summaries.
- `strategy_backtest.py`: compatibility wrapper that forwards old commands.

Universes:

- `symbols.example.csv`: small example universe.
- `symbols_us_1610.csv`: US 1610 universe.
- `us_1610_stock_universe.csv`: source file for the US 1610 universe.
- `build_us_1610_symbols.py`: rebuilds the US 1610 symbol CSV.

Removed legacy scripts:

- `bottom_divergence_signal_push.py`
- `bottom_divergence_d_trigger_push.py`
- `waterline_signal_push.py`

## Credentials

Create `credentials.py` in the repo root. It is git-ignored.

```python
TWELVE_DATA_API_KEY = "your_twelve_data_api_key"

FEISHU_WEBHOOK_WATERLINE_SIGNAL = "your_waterline_signal_day_webhook"
FEISHU_WEBHOOK_WATERLINE_TRADE = "your_waterline_trade_day_webhook"
FEISHU_WEBHOOK_BOTTOM_HISTORY = "your_bottom_history_webhook"
FEISHU_WEBHOOK_BOTTOM_BUY = "your_bottom_buy_webhook"
FEISHU_WEBHOOK_BOTTOM_SELL = "your_bottom_sell_webhook"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

The old single `FEISHU_WEBHOOK_URL` variable is not used. The old Discord webhook is not used.

## Signal Rules

### Bottom Divergence

The core bottom-divergence scanner uses MACD DIF:

- For every golden cross, compute the minimum DIF between that golden cross and the previous death cross.
- A bottom divergence exists when the second golden-cross close is lower than the first golden-cross close, while the second DIF minimum is higher than the first DIF minimum.
- Filters require enough price decline, enough DIF improvement, the current golden-cross DIF below zero, and no DIF cross above zero between the two golden crosses.

Structure points:

- `GA`: first golden cross.
- `GB`: second golden cross.
- `B`: lowest close between `GA` and `GB`.
- `BM`: highest close between `GA` and `GB`.
- `BM突破`: first close after `B` that breaks above `BM_price`.
- `CM`: first later high after `BM突破` using the local high rule.
- `C`: first later pullback low after `CM` using the local low rule.
- `D`: first close above `CM_price` before structure failure.
- `B_FAIL`: after `B`, close falls below `B_price * 0.95`.
- `C_FAIL`: after `C`, close falls below `C_price * 0.95`.

Production bottom trading uses BM break, not D:

```text
buy signal day = BM_break_time date
reference_price = BM_break_price
planned buy = next trading day open
```

Historical bottom pushes require a valid BM break and skip `B_FAIL`, `C_FAIL`, `NO_BM_BREAK`, and `STRUCTURE_FAILED`.

### Waterline

Signal day:

```text
close > open
close - low > 1.2 * (high - close)
volume >= mean(previous 10 daily volumes) * 2
reference_price = signal day close
```

Trade day is the next trading day:

```text
above_ratio = count(1min close > signal day close) / count(1min bars)
trade-day confirmation if above_ratio >= 0.8 and minute bars >= 300
```

Waterline has two push types:

- Signal-day push: the daily candle and volume setup.
- Trade-day push: next-day minute confirmation.

### Sell Rule

Bottom-trade sell checks only open positions. For each checked day:

```text
ma5_price = mean(close of previous 5 completed daily bars)
ma5_below_ratio = count(1min close < ma5_price) / count(1min bars)
reference_below_ratio = count(1min close < reference_price) / count(1min bars)

sell signal if ma5_below_ratio >= 0.5
else sell signal if reference_below_ratio >= 0.5
planned sell = next trading day open
```

If both MA5 and reference trigger on the same day, MA5 takes priority.

## Cloud Setup

Use Python 3.11+.

```bash
cd /root/UpBottom
python -m pip install -r requirements.txt
mkdir -p /data/UpBottom/data /data/UpBottom/outputs /data/UpBottom/logs
```

For a fresh server, build the initial daily cache and scan once:

```bash
python fetch_sp500_2026_and_mark.py \
  --start 2025-10-01 \
  --refresh-metadata \
  --overlap-days 10 \
  --workers 2 \
  --fetch-timeframes 1day
```

For a custom universe:

```bash
python fetch_sp500_2026_and_mark.py \
  --symbols-file /data/UpBottom/symbols.csv \
  --start 2025-10-01 \
  --overlap-days 10 \
  --workers 2 \
  --fetch-timeframes 1day
```

Smoke checks:

```bash
python fetch_sp500_2026_and_mark.py --limit 5 --workers 2 --fetch-timeframes 1day
python bottom_trade_push.py --dry-run
python waterline_push.py --dry-run
python daily_workflow.py --step all --dry-run --skip-fetch-scan --skip-tmp-download
```

## Migrating From The Old Layout

If old data is under `/data/UpBottom/data/stocks`, move the long-term daily cache once:

```bash
mkdir -p /data/UpBottom/data /data/UpBottom/outputs
mv /data/UpBottom/data/stocks/1day /data/UpBottom/data/
mv /data/UpBottom/outputs/stocks/* /data/UpBottom/outputs/
```

Do not move old long-term 1min caches into `/data/UpBottom/data`. The new production workflow uses temporary 1min files only.

After migration, run:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --fetch-timeframes 1day
python bottom_trade_push.py --dry-run
python waterline_push.py --dry-run
```

Keep old directories for a few successful runs before deleting them manually.

## Daily Automation

All cron times below are Beijing time. Set the server timezone to Asia/Shanghai first:

```bash
timedatectl set-timezone Asia/Shanghai
crontab -e
```

Robust split workflow:

```cron
# Beijing time. US Monday-Friday sessions are processed on Beijing Tuesday-Saturday mornings.

# 1. Update 1day cache and rescan ad_signals.csv.
40 5 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step fetch-scan --start 2025-10-01 --overlap-days 10 --workers 2 >> /data/UpBottom/logs/upbottom_fetch_scan.log 2>&1

# 2. Download shared temporary 1min data once.
55 5 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step prepare-tmp >> /data/UpBottom/logs/upbottom_prepare_tmp.log 2>&1

# 3. Push bottom-divergence BM buy/sell signals.
30 6 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step bottom-trade >> /data/UpBottom/logs/upbottom_bottom_trade.log 2>&1

# 4. Push waterline signal-day and trade-day signals.
30 7 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step waterline >> /data/UpBottom/logs/upbottom_waterline.log 2>&1

# 5. Cleanup temporary 1min files.
00 9 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step cleanup >> /data/UpBottom/logs/upbottom_cleanup.log 2>&1

# 6. Off-hours split-adjustment maintenance.
30 16 * * 2-6 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 --repair-split-jumps --fetch-timeframes 1day >> /data/UpBottom/logs/upbottom_split_repair.log 2>&1

# 7. Monthly metadata refresh: first Sunday of each month.
15 10 1-7 * 0 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2 --fetch-timeframes 1day >> /data/UpBottom/logs/upbottom_metadata.log 2>&1
```

For a custom universe, add this to the workflow commands:

```text
--symbols-file /data/UpBottom/symbols.csv
```

Manual historical bottom push:

```bash
python daily_workflow.py --step bottom-history
```

The `all` step is useful for manual dry-runs, but cron should use split steps so one failed task does not prevent later repair or cleanup jobs from running.

## Data Update Details

`fetch_sp500_2026_and_mark.py` is incremental:

```text
read local CSV
-> re-fetch from overlap window
-> merge by datetime
-> newer fetched row overwrites old local row
-> write clean CSV
-> scan local daily cache
```

Default overlap in production examples is 10 calendar days. Direct script default supports both `1day` and `4h`, but production cron passes `--fetch-timeframes 1day`.

Default universe is S&P 500 if `--symbols-file` is not provided. Metadata is cached under:

```text
/data/UpBottom/outputs/sp500_metadata.csv
/data/UpBottom/outputs/stock_metadata.csv
```

## Push State

Push state is stored under `/data/UpBottom/outputs/push_state/`.

Important files:

```text
bottom_trade_positions.json       # open/closed bottom BM trade state
bottom_trade_daily_cache.json     # daily bottom buy/sell dedupe
waterline_push_cache.json         # waterline signal/trade dedupe
bottom_history_push_cache.json    # manual historical BM-break dedupe
```

Bottom-trade buy logic skips new buys when that symbol already has an open position.

## Backtests

Backtests are research-only. They do not send Feishu messages and they do not update production push state.

Shared behavior:

- Runtime paths come from `constants.py`.
- Symbol and provider source-symbol mapping uses the same CSV conventions as production.
- Missing daily data skips the symbol instead of aborting the whole batch.
- Missing 1min data is downloaded into `/data/UpBottom/outputs/backtest_minute_cache/1min/` unless `--skip-download` is set.
- Exit rule is the same MA5/reference 50% minute-break rule as production.
- Default entry execution is next trading day open.

### Bottom Backtest

Bottom backtest supports BM/C/D entry variants:

```text
bm:
  signal_date = BM_break_time date
  reference_price = BM_break_price

c:
  signal_date = C confirmation date
  reference_price = C confirmation close

d:
  signal_date = D_time date
  reference_price = D_price
```

Run BM/C/D comparison:

```bash
python bottom_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --daily-dir /data/UpBottom/data/1day \
  --output-dir /data/UpBottom/outputs/backtests/bottom \
  --start 2025-10-01 \
  --entry-variants bm c d
```

For a stricter comparison on structures that eventually reached D:

```bash
python bottom_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --daily-dir /data/UpBottom/data/1day \
  --output-dir /data/UpBottom/outputs/backtests/bottom_completed \
  --start 2025-10-01 \
  --entry-variants bm c d \
  --completed-only
```

Run only the production-style BM entry:

```bash
python bottom_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --entry-variants bm \
  --start 2025-10-01
```

Bottom outputs:

```text
bottom_trades.csv
bottom_summary.csv
bottom_half_year_summary.csv
bottom_bm_break_trades.csv
bottom_bm_break_half_year_summary.csv
bottom_c_confirm_trades.csv
bottom_c_confirm_half_year_summary.csv
bottom_d_trigger_trades.csv
bottom_d_trigger_half_year_summary.csv
```

### Waterline Backtest

Run waterline backtest:

```bash
python waterline_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --daily-dir /data/UpBottom/data/1day \
  --output-dir /data/UpBottom/outputs/backtests/waterline \
  --start 2025-10-01
```

Tune waterline thresholds:

```bash
python waterline_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --volume-lookback 10 \
  --volume-multiple 2.0 \
  --candle-k 1.2 \
  --above-ratio 0.8 \
  --min-minutes 300
```

Waterline outputs:

```text
waterline_ma5_trades.csv
waterline_ma5_summary.csv
waterline_ma5_half_year_summary.csv
```

### Compatibility Wrapper

`strategy_backtest.py` remains as a compatibility wrapper:

```bash
python strategy_backtest.py --strategy bottom --symbols AAPL --entry-variants bm
python strategy_backtest.py --strategy waterline --symbols AAPL
python strategy_backtest.py --strategy both --symbols-file symbols_us_1610.csv
```

For new work, prefer `bottom_backtest.py` and `waterline_backtest.py` directly.

## Useful Commands

Production:

```bash
python fetch_sp500_2026_and_mark.py --limit 5 --workers 2 --fetch-timeframes 1day
python fetch_sp500_2026_and_mark.py --skip-fetch --fetch-timeframes 1day
python daily_workflow.py --step fetch-scan --dry-run
python daily_workflow.py --step prepare-tmp --dry-run
python daily_workflow.py --step bottom-trade --dry-run
python daily_workflow.py --step waterline --dry-run
python daily_workflow.py --step cleanup
```

Manual inspection:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
python bottom_history_push.py --dry-run
python bottom_trade_push.py --dry-run
python waterline_push.py --dry-run
```

Backtests:

```bash
python bottom_backtest.py --help
python waterline_backtest.py --help
python bottom_backtest.py --symbols AAPL --entry-variants bm c d --skip-download
python waterline_backtest.py --symbols AAPL --skip-download
```

## Troubleshooting

Missing Feishu webhook:

```text
Missing Feishu webhook ...
```

Check `credentials.py` or environment variables for the specific webhook:

```text
FEISHU_WEBHOOK_WATERLINE_SIGNAL
FEISHU_WEBHOOK_WATERLINE_TRADE
FEISHU_WEBHOOK_BOTTOM_HISTORY
FEISHU_WEBHOOK_BOTTOM_BUY
FEISHU_WEBHOOK_BOTTOM_SELL
```

No messages in dry-run:

- Confirm daily cache exists in `/data/UpBottom/data/1day/`.
- Confirm `ad_signals.csv` exists under `/data/UpBottom/outputs/`.
- Confirm the run date matches the latest US trading day after conversion to Beijing morning.
- For bottom sells, confirm there is an open position in `bottom_trade_positions.json`.

Too much 1min data:

- Production should only use `/data/UpBottom/tmp/1min/`.
- Backtest cache should stay under `/data/UpBottom/outputs/backtest_minute_cache/1min/`.
- Use `daily_workflow.py --step cleanup` to clear production tmp files.

Corporate-action jumps:

```bash
python fetch_sp500_2026_and_mark.py \
  --start 2025-10-01 \
  --overlap-days 10 \
  --workers 2 \
  --repair-split-jumps \
  --fetch-timeframes 1day
```

## Notes

- All production times in this README are Beijing time.
- Production daily workflow fetches only `1day`.
- Bottom historical push is currently manual-only.
- BM/C/D comparison is a research feature, not the production trading signal.
- Production bottom trading signal remains BM break only.
