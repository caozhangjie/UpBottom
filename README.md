# UpBottom

UpBottom is a local research tool for finding bottom-divergence reversal structures in S&P 500 price data. It is not a trading bot, backtest engine, or investment advice system. The workflow is:

```text
download OHLCV data -> detect bottom divergence -> evaluate B/BM/CM/C/D structure -> export CSV and SVG review charts
```

## Current Model

The core scanner lives in `ad_structure_v05_core.py`.

The bottom-divergence definition uses MACD DIF:

- For every golden cross, compute the minimum DIF value between that golden cross and the previous death cross.
- A bottom divergence exists when the current golden-cross close is lower than the previous golden-cross close, while the current golden-cross DIF minimum is higher than the previous one.
- The current implementation keeps the confirmed filters from the original code: price must be at least 5% lower, DIF minimum must be at least 5% higher, current golden-cross DIF must be below zero, and DIF must not cross above zero between the two golden crosses.

The structure points are:

- `golden_A` / `golden_B`: the two golden-cross bars used for the divergence comparison.
- `B`: the lowest close between the two golden-cross bars.
- `BM`: the highest close between the two golden-cross bars.
- `BM Break`: the first close after `B` that breaks above `BM_price`.
- `CM`: the first future-3 confirmed high after `BM Break`.
- `C`: optional future-3 confirmed pullback lows after `CM`.
- `D`: the first close above `CM_price` before structure failure.

All structure comparisons use `close`, not `high` or `low`.

## Files

- `ad_structure_v05_core.py`: pure scanner and structure evaluator.
- `fetch_sp500_2026_and_mark.py`: downloads 2026 S&P 500 data, scans signals, and writes SVG review charts.
- `credentials.example.py`: template for local Twelve Data credentials.

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
```

Yahoo Finance remains available as a fallback provider.

## Usage

Download S&P 500 2026 daily and 4-hour data from Twelve Data, scan signals, and export review charts:

```bash
python3 fetch_sp500_2026_and_mark.py
```

Run a smoke test on a small subset:

```bash
python3 fetch_sp500_2026_and_mark.py --limit 5 --workers 2
```

Use existing local CSV files without downloading again:

```bash
python3 fetch_sp500_2026_and_mark.py --skip-fetch
```

Use Yahoo instead:

```bash
python3 fetch_sp500_2026_and_mark.py --provider yahoo
```

Outputs are written under:

```text
data/sp500_2026/
outputs/sp500_2026/ad_signals.csv
outputs/sp500_2026/charts/
```

## Notes

This repo is built for human review. The generated SVG charts mark `GA`, `GB`, `B`, `BM`, `BM Break`, `CM`, optional `C` points, and terminal `D` or failure points so the structure can be inspected visually.
