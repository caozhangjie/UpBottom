# UpBottom

UpBottom scans US stock OHLCV data for bottom-divergence structures and waterline signals, then sends daily Feishu notifications. It is a research and alerting tool, not investment advice or an automated trading bot.

```text
download daily OHLCV -> merge local cache -> scan signals -> push Feishu alerts
                                        -> optional strategy backtests
```

## Current System

The old standalone push scripts have been removed. The production system is now:

- `daily_workflow.py`: scheduled workflow wrapper for fetch/scan, temporary 1min download, push steps, and cleanup.
- `bottom_history_push.py`: bottom-divergence historical BM-break push, currently 1day only.
- `bottom_trade_push.py`: bottom-divergence BM-break buy/sell push.
- `waterline_push.py`: waterline signal-day and trade-day push.
- `strategy_backtest.py`: research backtest for the new BM-break and waterline systems.
- `constants.py`: runtime paths and all Feishu webhook names.
- `bottom_common.py`, `push_utils.py`, `intraday_tmp.py`: shared helpers.
- `fetch_sp500_2026_and_mark.py`: daily data update and bottom-divergence scan.
- `waterline_signal.py`: waterline signal scanner.
- `ad_structure_v05_core.py`: bottom-divergence structure engine.

Runtime data uses no dataset directory layer:

```text
/data/UpBottom/data/1day/
/data/UpBottom/outputs/
/data/UpBottom/outputs/push_state/
/data/UpBottom/tmp/1min/
```

Set `UPBOTTOM_RUNTIME_ROOT` only if you intentionally want a different root.

## Credentials

Create `credentials.py` in the repo root on the server:

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

The old single Feishu webhook setting is no longer used.

## Bottom-Divergence Model

The scanner uses MACD DIF bottom divergence:

- `GA`: first golden cross.
- `GB`: second golden cross.
- `B`: lowest close between `GA` and `GB`.
- `BM`: highest close between `GA` and `GB`.
- `BM突破`: first close after `B` that breaks above `BM_price`.
- `CM`, `C`, `D`: later structure points retained by the core model.
- `B_FAIL` and `C_FAIL`: structure failure guards.

The new trading signal uses `BM突破`, not the old D trigger:

```text
buy signal day = BM_break_time date
reference_price = BM_break_price
planned buy = next trading day open
```

Historical bottom-divergence pushes also require a valid BM break and skip `B_FAIL`, `C_FAIL`, `NO_BM_BREAK`, and `STRUCTURE_FAILED`.

## Waterline Signals

Signal day:

```text
close > open
close - low > 1.2 * (high - close)
volume >= mean(previous 10 daily volumes) * 2
reference_price = signal day close
```

Trade day is the next trading day. The trade-day push confirms that enough 1min closes stayed above the signal close:

```text
above_ratio >= 0.8
min_minutes >= 300
```

## Sell Rule

Bottom-trade sell checks run only for open positions. For each checked day, temporary 1min data is used:

```text
ma5_price = mean(close of previous 5 completed daily bars)
ma5_below_ratio = count(1min close < ma5_price) / count(1min bars)
reference_below_ratio = count(1min close < reference_price) / count(1min bars)

sell signal if ma5_below_ratio >= 0.5
else sell signal if reference_below_ratio >= 0.5
planned sell = next trading day open
```

The daily workflow downloads 1min data once into `/data/UpBottom/tmp/1min/`, shares it across the push steps, and deletes it after all jobs finish.

## Cloud Setup

```bash
cd /root/UpBottom
python -m pip install -r requirements.txt
mkdir -p /data/UpBottom/data /data/UpBottom/outputs /data/UpBottom/logs
```

Initial or migration commands:

```bash
# If migrating from the old dataset layer:
mv /data/UpBottom/data/stocks/1day /data/UpBottom/data/
mv /data/UpBottom/outputs/stocks/* /data/UpBottom/outputs/

# First full daily cache build and scan:
python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2

# Optional dry-run smoke checks:
python bottom_history_push.py --dry-run
python daily_workflow.py --step prepare-tmp --dry-run
python bottom_trade_push.py --dry-run
python waterline_push.py --dry-run
```

Do not move long-term 1min caches into `/data/UpBottom/data`. Production 1min files are temporary.

## Daily Automation

All cron times below are Beijing time. Set the server timezone to Asia/Shanghai first, or use cron support for `CRON_TZ=Asia/Shanghai`.

```bash
crontab -e
```

```cron
# Beijing time. US Monday-Friday sessions are processed on Beijing Tuesday-Saturday mornings.

40 5 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step fetch-scan --start 2025-10-01 --overlap-days 10 --workers 2 >> /data/UpBottom/logs/upbottom_fetch_scan.log 2>&1
55 5 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step prepare-tmp >> /data/UpBottom/logs/upbottom_prepare_tmp.log 2>&1
30 6 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step bottom-trade >> /data/UpBottom/logs/upbottom_bottom_trade.log 2>&1
30 7 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step waterline >> /data/UpBottom/logs/upbottom_waterline.log 2>&1
00 9 * * 2-6 cd /root/UpBottom && python daily_workflow.py --step cleanup >> /data/UpBottom/logs/upbottom_cleanup.log 2>&1
30 16 * * 2-6 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --overlap-days 10 --workers 2 --repair-split-jumps >> /data/UpBottom/logs/upbottom_split_repair.log 2>&1
15 10 1-7 * 0 cd /root/UpBottom && python fetch_sp500_2026_and_mark.py --start 2025-10-01 --refresh-metadata --overlap-days 10 --workers 2 >> /data/UpBottom/logs/upbottom_metadata.log 2>&1
```

`bottom-history` is intentionally not scheduled for now because only the daily trading pushes are active. To run a one-off 1day historical push:

```bash
python daily_workflow.py --step bottom-history
```

For a custom universe, add `--symbols-file /data/UpBottom/symbols.csv` to the workflow commands.

## Backtests

`strategy_backtest.py` matches the new signal system.

Bottom BM-break entry:

```text
valid BM break on 1day
signal_date = BM_break_time date
reference_price = BM_break_price
default entry = next trading day open
```

Waterline entry:

```text
signal day passes waterline rules
next trading day 1min above_ratio >= 0.8
reference_price = signal day close
default entry = next trading day open after confirmation day
```

Shared exit is the same MA5/reference 50% minute-break rule used by push alerts. Backtest 1min data is cached under outputs by default:

```text
/data/UpBottom/outputs/backtest_minute_cache/1min/
```

Run both strategies:

```bash
python strategy_backtest.py \
  --symbols-file symbols_us_1610.csv \
  --daily-dir /data/UpBottom/data/1day \
  --output-dir /data/UpBottom/outputs/backtests \
  --start 2025-10-01 \
  --strategy both
```

Run only the new bottom BM-break strategy without downloading missing 1min data:

```bash
python strategy_backtest.py \
  --strategy bottom \
  --skip-download \
  --start 2025-10-01
```

Outputs:

```text
bottom_bm_break_trades.csv
bottom_bm_break_half_year_summary.csv
waterline_ma5_trades.csv
waterline_ma5_half_year_summary.csv
all_trades.csv
all_half_year_summary.csv
```

## Useful Commands

```bash
python fetch_sp500_2026_and_mark.py --limit 5 --workers 2
python fetch_sp500_2026_and_mark.py --symbols-file symbols.example.csv --limit 2 --workers 2
python fetch_sp500_2026_and_mark.py --skip-fetch
python fetch_sp500_2026_and_mark.py --skip-fetch --render-charts
python daily_workflow.py --step fetch-scan --dry-run
python daily_workflow.py --step cleanup
python strategy_backtest.py --help
```
