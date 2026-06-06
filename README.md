# UpBottom

`main` is the production signal branch. It is meant for the server to pull and run daily Feishu signal pushes.

Research backtests live on the `backtest` branch. For local backtesting:

```bash
git switch backtest
```

This branch does not contain backtest entry scripts.

## Production Scope

Production currently uses daily bars only.
All production scripts that accept `--date` default to Beijing today minus one day. The server runs in Beijing time, so the morning workflow normally processes the previous US trading session.

Active scheduled jobs:

- Daily data update and bottom-divergence scan: `daily_workflow.py --step fetch-scan`
- Shared temporary 1min download: `daily_workflow.py --step prepare-tmp`
- Bottom-divergence historical BM-break push: `daily_workflow.py --step bottom-history`
- Bottom-divergence C-confirm buy/sell push: `daily_workflow.py --step bottom-trade`
- Waterline signal-day/trade-day push: `daily_workflow.py --step waterline`
- Temporary 1min cleanup: `daily_workflow.py --step cleanup`
- Off-hours split-adjustment repair: `fetch_sp500_2026_and_mark.py --repair-split-jumps`

Paused/manual:

- 4h fetching is not scheduled.
- Monthly S&P 500 metadata refresh is not scheduled when using `/data/UpBottom/symbols.csv`.
- Chart rendering is manual: `fetch_sp500_2026_and_mark.py --skip-fetch --render-charts`.

## Runtime Layout

Default runtime root:

```text
/data/UpBottom
```

Production layout:

```text
/data/UpBottom/data/1day/              # long-term daily cache
/data/UpBottom/outputs/ad_signals.csv  # latest bottom-divergence scan
/data/UpBottom/outputs/push_state/     # historical push dedupe and open-position state
/data/UpBottom/tmp/1min/               # temporary production minute data
/data/UpBottom/logs/                   # cron logs
```

The code does not use a `{dataset}` directory layer. If a different runtime root is needed:

```bash
export UPBOTTOM_RUNTIME_ROOT="/your/path/UpBottom"
```

Do not store production 1min data under `/data/UpBottom/data`. The daily workflow downloads needed 1min bars into `/data/UpBottom/tmp/1min/`, shares them across push jobs, then deletes them.

## Files

Core:

- `fetch_sp500_2026_and_mark.py`: daily data update, merge/dedupe, scan, optional chart rendering.
- `ad_structure_v05_core.py`: bottom-divergence model and structure evaluator.
- `waterline_signal.py`: waterline signal-day and trade-day confirmation logic.
- `constants.py`: runtime paths and Feishu webhook names.
- `credentials.example.py`: private credential template.

Production pushes:

- `daily_workflow.py`: step-based cron workflow.
- `bottom_trade_push.py`: bottom-divergence C-confirm buy/sell alerts and position state.
- `waterline_push.py`: waterline signal-day and trade-day alerts.
- `bottom_history_push.py`: manual historical bottom-divergence BM-break alerts.
- `bottom_common.py`, `push_utils.py`, `intraday_tmp.py`: shared production helpers.

Universes:

- `symbols.example.csv`: small example universe.
- `symbols_us_1610.csv`: US 1610 universe.
- `us_1610_stock_universe.csv`: source file for the US 1610 universe.
- `build_us_1610_symbols.py`: rebuilds the US 1610 symbol CSV.

## Credentials

Create `credentials.py` in the repo root on the server. It is git-ignored.

```python
TWELVE_DATA_API_KEY = "your_twelve_data_api_key"

FEISHU_WEBHOOK_WATERLINE_SIGNAL = "your_waterline_signal_day_webhook"
FEISHU_WEBHOOK_WATERLINE_TRADE = "your_waterline_trade_day_webhook"
FEISHU_WEBHOOK_WATERLINE_SELL = "your_waterline_sell_day_webhook"
FEISHU_WEBHOOK_BOTTOM_HISTORY = "your_bottom_history_webhook"
FEISHU_WEBHOOK_BOTTOM_BUY = "your_bottom_buy_webhook"
FEISHU_WEBHOOK_BOTTOM_SELL = "your_bottom_sell_webhook"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

The old single `FEISHU_WEBHOOK_URL` variable is not used. Discord webhook variables are not used.

## Signal Rules

### Bottom Divergence

The bottom-divergence scanner uses MACD DIF and close prices.

Structure points:

- `GA`: first golden cross.
- `GB`: second golden cross.
- `B`: lowest close between `GA` and `GB`.
- `BM`: highest close between `GA` and `GB`.
- `BM突破`: first close after `B` that breaks above `BM_price`.
- `CM`, `C`, `D`: later structure points kept by the model.
- `B_FAIL`: after `B`, close falls below `B_price * 0.95`.
- `C_FAIL`: after `C`, close falls below `C_price * 0.95`.

Production bottom trading uses the first C confirmation from the double-golden-cross bottom divergence:

```text
buy signal day = first C confirm_time date
reference_price = C confirmation daily close
planned buy = next trading day open
```

New buy alerts are still pushed even when the server already records an open position for the same symbol. The alert includes `Server position state` so stale server state is visible in Feishu. If an open position already exists, the push does not overwrite that position record. BM and BM-break values are kept in the alert as context, but they are not the production entry trigger.
The structure must not trigger `B_FAIL` from `CM_time` through the C confirmation day.

### Waterline

Waterline is a trend-continuation setup. The signal day looks for a small rising wave before the possible main wave:

```text
trend_lookback = 10
trend_up_days >= 6
signal close / first close in trend window - 1 >= 0.08
signal-day close/previous close - 1 > each of the previous 9 daily returns
close > open
close >= MA20
MA20 is above its value 3 trading days ago
volume >= mean(previous 10 daily volumes) * 1.5
reference_price = signal day close
```

Trade day is the next trading day:

```text
above_ratio = count(1min close > signal day close) / count(1min bars)
trade-day confirmation if above_ratio >= 0.8 and minute bars >= 300
```

### Sell Rule

Bottom-trade sell checks only open bottom-divergence positions:

```text
ma5_price = mean(close of previous 5 completed daily bars)
ma5_below_ratio = count(1min close < ma5_price) / count(1min bars)
reference_below_ratio = count(1min close < reference_price) / count(1min bars)

sell signal if ma5_below_ratio >= 0.5
else sell signal if reference_below_ratio >= 0.5
planned sell = next trading day open
```

If both rules trigger on the same day, MA5 takes priority.

Waterline sell checks only open waterline trend positions:

```text
ma20_price = mean(close of previous 20 completed daily bars)
ma20_below_ratio = count(1min close < ma20_price) / count(1min bars)

sell signal if ma20_below_ratio >= 0.5 and minute bars >= 300
planned sell = next trading day open
```

## Server Setup

Use Python 3.11+.

```bash
cd /root/UpBottom
python -m pip install -r requirements.txt
mkdir -p /data/UpBottom/data /data/UpBottom/outputs /data/UpBottom/logs
```

Fresh initial daily build:

```bash
python fetch_sp500_2026_and_mark.py \
  --start 2025-10-01 \
  --refresh-metadata \
  --overlap-days 10 \
  --workers 2 \
  --fetch-timeframes 1day
```

When using the default Twelve Data provider, the daily build fetches OHLCV from `time_series` and then merges daily VWAP values from the separate `/vwap` endpoint when available.

Custom universe:

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

## Migrating From Old Runtime Layout

If old data is under `/data/UpBottom/data/stocks`, move the long-term daily cache once:

```bash
mkdir -p /data/UpBottom/data /data/UpBottom/outputs
mv /data/UpBottom/data/stocks/1day /data/UpBottom/data/
mv /data/UpBottom/outputs/stocks/* /data/UpBottom/outputs/
```

Do not move old long-term 1min caches into `/data/UpBottom/data`.

After migration:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch --fetch-timeframes 1day
python bottom_trade_push.py --dry-run
python waterline_push.py --dry-run
```

Keep old directories for a few successful runs before deleting them manually.

## Cron

All cron times below are Beijing time. Set the server timezone first:

```bash
timedatectl set-timezone Asia/Shanghai
crontab -e
```

Robust split workflow:

```cron
# Beijing time. US Monday-Friday sessions are processed on Beijing Tuesday-Saturday mornings.

# 1. Update 1day cache, merge VWAP, and rescan ad_signals.csv with the custom symbol list.
40 5 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step fetch-scan --symbols-file /data/UpBottom/symbols.csv --start 2024-01-01 --overlap-days 10 --workers 2 --fetch-timeframes 1day' >> /data/UpBottom/logs/upbottom_fetch_scan.log 2>&1

# 2. Download shared temporary 1min data once.
55 5 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step prepare-tmp' >> /data/UpBottom/logs/upbottom_prepare_tmp.log 2>&1

# 3. Push bottom-divergence historical BM-break signals.
10 6 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step bottom-history' >> /data/UpBottom/logs/upbottom_bottom_history.log 2>&1

# 4. Push bottom-divergence C-confirm buy/sell signals.
30 6 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step bottom-trade' >> /data/UpBottom/logs/upbottom_bottom_trade.log 2>&1

# 5. Push waterline signal-day and trade-day signals.
30 7 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step waterline' >> /data/UpBottom/logs/upbottom_waterline.log 2>&1

# 6. Cleanup temporary 1min files.
00 9 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python daily_workflow.py --step cleanup' >> /data/UpBottom/logs/upbottom_cleanup.log 2>&1

# 7. Off-hours split-adjustment maintenance, also using the custom symbol list.
30 16 * * 2-6 /bin/bash -lc 'source /root/miniconda3/etc/profile.d/conda.sh; conda activate myenv; cd /root/UpBottom; python fetch_sp500_2026_and_mark.py --symbols-file /data/UpBottom/symbols.csv --start 2024-01-01 --overlap-days 10 --workers 2 --repair-split-jumps --fetch-timeframes 1day' >> /data/UpBottom/logs/upbottom_split_repair.log 2>&1
```

For a custom universe, add this to workflow commands:

```text
--symbols-file /data/UpBottom/symbols.csv
```

Manual historical bottom push:

```bash
python daily_workflow.py --step bottom-history
```

Cron should use split steps so one failed job does not prevent later jobs or cleanup from running.
The cron commands do not pass `--date`; they rely on the shared default of Beijing today minus one day. Use `--date YYYY-MM-DD` only for manual replays or repairs.

## Push State

Push state lives under:

```text
/data/UpBottom/outputs/push_state/
```

Important files:

```text
bottom_trade_positions.json       # open/closed bottom C-confirm trade state
waterline_positions.json          # open/closed waterline trend trade state
bottom_history_push_cache.json    # manual historical BM-break dedupe
```

Bottom trade and waterline daily pushes do not use push-cache dedupe. They include the server position state in buy/trade alerts, so a stale open position is visible without hiding the alert. Historical bottom BM-break pushes still use `bottom_history_push_cache.json` for daily dedupe.

To clear server position state and start fresh, back up then remove the position files:

```bash
mkdir -p /data/UpBottom/outputs/push_state/backup
cp /data/UpBottom/outputs/push_state/bottom_trade_positions.json /data/UpBottom/outputs/push_state/backup/bottom_trade_positions.$(date +%Y%m%d%H%M%S).json 2>/dev/null || true
cp /data/UpBottom/outputs/push_state/waterline_positions.json /data/UpBottom/outputs/push_state/backup/waterline_positions.$(date +%Y%m%d%H%M%S).json 2>/dev/null || true
rm -f /data/UpBottom/outputs/push_state/bottom_trade_positions.json /data/UpBottom/outputs/push_state/waterline_positions.json
```

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

Branch switch for local research:

```bash
git switch backtest
```

## Troubleshooting

Missing Feishu webhook:

```text
Missing Feishu webhook ...
```

Check the specific webhook in `credentials.py`:

```text
FEISHU_WEBHOOK_WATERLINE_SIGNAL
FEISHU_WEBHOOK_WATERLINE_TRADE
FEISHU_WEBHOOK_WATERLINE_SELL
FEISHU_WEBHOOK_BOTTOM_HISTORY
FEISHU_WEBHOOK_BOTTOM_BUY
FEISHU_WEBHOOK_BOTTOM_SELL
```

No messages in dry-run:

- Confirm daily cache exists in `/data/UpBottom/data/1day/`.
- Confirm `/data/UpBottom/outputs/ad_signals.csv` exists.
- Confirm the run date matches the latest US trading day after Beijing morning conversion.
- For bottom sells, confirm there is an open position in `bottom_trade_positions.json`.

Too much 1min data:

- Production should only use `/data/UpBottom/tmp/1min/`.
- Run `python daily_workflow.py --step cleanup`.

Corporate-action jumps:

```bash
python fetch_sp500_2026_and_mark.py \
  --start 2025-10-01 \
  --overlap-days 10 \
  --workers 2 \
  --repair-split-jumps \
  --fetch-timeframes 1day
```
