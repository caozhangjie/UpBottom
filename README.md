# UpBottom

UpBottom is a local research tool for finding bottom-divergence reversal structures in S&P 500 price data. It is not a trading bot, backtest engine, or investment advice system. The workflow is:

```text
download OHLCV data -> detect bottom divergence -> evaluate B/BM/CM/C/D structure -> export CSV and PNG review charts
```

## Current Model

The core scanner lives in `ad_structure_v05_core.py`.

The bottom-divergence definition uses MACD DIF:

- For every golden cross, compute the minimum DIF value between that golden cross and the previous death cross.
- A bottom divergence exists when the current golden-cross close is lower than the previous golden-cross close, while the current golden-cross DIF minimum is higher than the previous one.
- The current implementation keeps these confirmed filters: price must be at least 5% lower, the current DIF minimum must exceed the previous DIF minimum by at least 5% of the previous DIF minimum's absolute value, current golden-cross DIF must be below zero, and DIF must not cross above zero between the two golden crosses.

The structure points are:

- `golden_A` / `golden_B`: the two golden-cross bars used for the divergence comparison.
- `B`: the lowest close between the two golden-cross bars.
- `BM`: the highest close between the two golden-cross bars.
- `BM Break`: the first close after `B` that breaks above `BM_price`.
- `CM`: the first close after `BM Break` that is higher than the previous 5 closes and next 5 closes.
- `C`: the first close after `CM` that is lower than the previous 5 closes and next 5 closes.
- `D`: the first close above `CM_price` before structure failure.
- Structure failure: after `B`, a close below `B_price * 0.95` is `B_FAIL`; after `C`, a close below `C_price * 0.95` is `C_FAIL`.

All structure comparisons use `close`, not `high` or `low`.

## Files

- `ad_structure_v05_core.py`: pure scanner and structure evaluator.
- `fetch_sp500_2026_and_mark.py`: incrementally downloads stock data, scans signals, and writes PNG review charts.
- `discord_signal_push.py`: pushes BM-break bottom-divergence alerts to Discord with a local dedupe cache.
- `credentials.example.py`: template for local Twelve Data and Discord credentials.
- `requirements.txt`: Python dependencies for local or cloud deployment.

Local-only files are intentionally ignored:

- `credentials.py`
- `data/`
- `outputs/`

## Data Source

The default data provider is Twelve Data. The script reads the API key in this order:

1. `--apikey`
2. `TWELVE_DATA_API_KEY`
3. local `credentials.py`

Create `credentials.py` locally:

```python
TWELVE_DATA_API_KEY = "your_api_key_here"
DISCORD_WEBHOOK_URL = "your_discord_webhook_url_here"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

Yahoo Finance remains available as a fallback provider.

## Cloud Setup

Use Python 3.11+.

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

For cloud deployment, copy your local `credentials.py` into the project root on the cloud machine. The file is intentionally ignored by git, so keep a private copy outside the repository as well.

```python
TWELVE_DATA_API_KEY = "your_twelve_data_api_key"
DISCORD_WEBHOOK_URL = "your_discord_webhook_url"

STOCK_CN_NAMES = {
    "AAPL": "苹果",
}
```

The only third-party runtime dependency is `matplotlib` for PNG chart rendering. `tzdata` is included so `zoneinfo` works consistently across minimal cloud images.

First run, or occasional metadata refresh:

```bash
python fetch_sp500_2026_and_mark.py --refresh-metadata
```

Normal daily incremental run with the default S&P 500 universe:

```bash
python fetch_sp500_2026_and_mark.py --overlap-days 10
```

Run with a custom stock universe:

```bash
python fetch_sp500_2026_and_mark.py --symbols-file symbols.csv --overlap-days 10
```

`symbols.csv` can be either a plain text file with one symbol per line:

```text
AAPL
MSFT
BRK.B
```

Or a CSV file with optional metadata columns:

```csv
symbol,source_symbol,english_name,chinese_name,sector,sub_industry
AAPL,AAPL,Apple Inc.,苹果,Information Technology,"Technology Hardware, Storage & Peripherals"
BRK-B,BRK.B,Berkshire Hathaway,伯克希尔,Financials,Multi-Sector Holdings
```

`source_symbol` is the provider symbol used for downloading. `symbol` is the safe local ID used in filenames and alerts. If `symbol` is omitted, the script derives one automatically.

Fast local rescan without downloading:

```bash
python fetch_sp500_2026_and_mark.py --skip-fetch
```

Send Discord alerts after the scan:

```bash
python discord_signal_push.py --timeframe 4h
python discord_signal_push.py --timeframe 1day
```

Preview or force resend:

```bash
python discord_signal_push.py --timeframe 4h --dry-run
python discord_signal_push.py --timeframe 4h --force
```

## Usage

Download daily and 4-hour data from Twelve Data, scan signals, and export review charts:

```bash
python3 fetch_sp500_2026_and_mark.py
```

Daily use is incremental by default: each existing CSV is loaded, the script re-fetches from 10 calendar days before the last local bar, then merges rows by timestamp before scanning. This keeps runs fast while filling recent gaps and refreshing revised latest bars.

```bash
python3 fetch_sp500_2026_and_mark.py --overlap-days 10
```

Use local data only for a fast rescan:

```bash
python3 fetch_sp500_2026_and_mark.py --skip-fetch
```

Run a smoke test on a small subset:

```bash
python3 fetch_sp500_2026_and_mark.py --limit 5 --workers 2
```

Run a smoke test on a custom list:

```bash
python3 fetch_sp500_2026_and_mark.py --symbols-file symbols.example.csv --limit 2 --workers 2
```

Refresh S&P 500 names and industry metadata only when needed:

```bash
python3 fetch_sp500_2026_and_mark.py --refresh-metadata
```

Use Yahoo instead:

```bash
python3 fetch_sp500_2026_and_mark.py --provider yahoo
```

Push Discord BM-break alerts after data has been updated and scanned:

```bash
python3 discord_signal_push.py --timeframe 4h
python3 discord_signal_push.py --timeframe 1day
```

Preview without sending, or manually force a resend:

```bash
python3 discord_signal_push.py --timeframe 4h --dry-run
python3 discord_signal_push.py --timeframe 4h --force
```

Outputs are written under:

```text
data/sp500_2026/
outputs/sp500_2026/ad_signals.csv
outputs/sp500_2026/sp500_metadata.csv
outputs/sp500_2026/stock_metadata.csv
outputs/sp500_2026/discord_push_cache.json
outputs/sp500_2026/charts/
```

## Notes

This repo is built for human review. The generated PNG charts mark `GA`, `GB`, `B`, `BM`, `突破BM`, `CM`, optional `C`, and terminal `D` or failure points so the structure can be inspected visually.
