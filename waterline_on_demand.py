"""On-demand waterline backtest pipeline.

The pipeline avoids downloading full-market intraday data:

1. Scan daily CSVs for signal-day candidates.
2. Download only each candidate's next-trading-day 1min bars.
3. Confirm entries from those minute bars.
4. Download 1min/1h bars only for confirmed entries from trade date onward.
5. Run the existing anchor/exit backtest.
"""

from __future__ import annotations

import argparse
import collections
import csv
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from ad_structure_v05_core import Row, load_rows
from fetch_sp500_2026_and_mark import (
    DATA_ROOT,
    OUTPUT_ROOT,
    CREDENTIALS_TWELVE_DATA_API_KEY,
    fetch_twelve_data_bars,
    merge_rows,
    safe_symbol,
    write_rows,
)
from waterline_signal import (
    WaterlineCandidate,
    WaterlineEntry,
    available_daily_symbols,
    confirm_candidate_entry,
    read_symbols_file,
    scan_symbol_candidates,
    write_entries,
)
from waterline_strategy import WaterlineTrade, backtest_entry, write_trades


def parse_date(text: str) -> date:
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def exclusive_end(start: str, days: int = 1) -> str:
    return (parse_date(start) + timedelta(days=days)).isoformat()


def fetch_cached_range(
    source_symbol: str,
    safe: str,
    timeframe: str,
    start: str,
    end: str | None,
    api_key: str,
    data_root: Path,
) -> list[Row]:
    folder = data_root / timeframe
    path = folder / f"{safe}_{timeframe}_indicators.csv"
    existing = load_rows(path, min_date=start) if path.exists() else []
    incoming = fetch_twelve_data_bars(source_symbol, timeframe, start, end, api_key)
    rows = merge_rows(existing, incoming)
    write_rows(path, rows)
    return load_rows(path, min_date=start)


def load_optional_rows(path: Path, min_date: str) -> list[Row]:
    return load_rows(path, min_date=min_date) if path.exists() else []


def candidate_key(candidate: WaterlineCandidate) -> tuple[str, str, str]:
    return candidate.symbol, candidate.signal_date, candidate.trade_date


def entry_to_row(entry: WaterlineEntry) -> dict[str, str]:
    row = asdict(entry)
    for key, value in row.items():
        if isinstance(value, float):
            row[key] = f"{value:.6f}"
        else:
            row[key] = str(value)
    return row


def write_candidates(candidates: list[WaterlineCandidate], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(WaterlineCandidate.__dataclass_fields__.keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            row = asdict(candidate)
            for key, value in row.items():
                if isinstance(value, float):
                    row[key] = f"{value:.6f}"
            writer.writerow(row)


def half_year(date_text: str) -> str:
    dt = parse_date(date_text)
    return f"{dt.year}H{1 if dt.month <= 6 else 2}"


def write_half_year_summary(trades: list[WaterlineTrade], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[WaterlineTrade]] = collections.defaultdict(list)
    for trade in trades:
        groups[half_year(trade.trade_date)].append(trade)
    fields = [
        "period",
        "trades",
        "closed_trades",
        "open_trades",
        "wins",
        "losses",
        "win_rate",
        "avg_return_pct",
        "median_return_pct",
        "sum_return_pct",
        "compound_return_pct",
        "avg_holding_days",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for period in sorted(groups):
            items = groups[period]
            returns = sorted(trade.return_pct for trade in items)
            wins = sum(1 for value in returns if value > 0)
            closed = sum(1 for trade in items if trade.status == "EXITED")
            compound = 1.0
            for value in returns:
                compound *= 1 + value / 100
            mid = len(returns) // 2
            median = (
                (returns[mid - 1] + returns[mid]) / 2
                if len(returns) % 2 == 0 and returns
                else returns[mid]
                if returns
                else 0.0
            )
            writer.writerow(
                {
                    "period": period,
                    "trades": len(items),
                    "closed_trades": closed,
                    "open_trades": len(items) - closed,
                    "wins": wins,
                    "losses": len(items) - wins,
                    "win_rate": f"{wins / len(items):.6f}" if items else "0.000000",
                    "avg_return_pct": f"{sum(returns) / len(returns):.6f}" if returns else "0.000000",
                    "median_return_pct": f"{median:.6f}",
                    "sum_return_pct": f"{sum(returns):.6f}",
                    "compound_return_pct": f"{(compound - 1) * 100:.6f}",
                    "avg_holding_days": f"{sum(trade.holding_days for trade in items) / len(items):.6f}" if items else "0.000000",
                }
            )


def source_symbols_from_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        if "," not in first_line:
            for line in f:
                symbol = line.strip().split()[0] if line.strip() else ""
                if symbol and not symbol.startswith("#"):
                    out[safe_symbol(symbol.upper())] = symbol.upper()
            return out
        for row in csv.DictReader(f):
            source = str(row.get("source_symbol") or row.get("symbol") or row.get("ticker") or "").strip().upper()
            safe = safe_symbol(str(row.get("symbol") or source).strip().upper())
            if source:
                out[safe] = source
    return out


def process_symbol(
    symbol: str,
    source_symbol: str,
    daily_rows: list[Row],
    args: argparse.Namespace,
    api_key: str | None,
) -> tuple[str, list[WaterlineCandidate], list[WaterlineEntry], list[WaterlineTrade], dict[str, int]]:
    candidates = scan_symbol_candidates(
        symbol,
        daily_rows,
        args.volume_lookback,
        args.volume_multiple,
        args.candle_k,
    )
    candidates.sort(key=lambda item: (item.trade_date, item.signal_date))
    entries: list[WaterlineEntry] = []
    trades: list[WaterlineTrade] = []
    stats = {
        "candidates": len(candidates),
        "skipped_in_position": 0,
        "entry_rejected": 0,
        "entries": 0,
        "trades": 0,
    }
    blocked_until: date | None = None
    for candidate in candidates:
        trade_dt = parse_date(candidate.trade_date)
        if blocked_until is not None and trade_dt <= blocked_until:
            stats["skipped_in_position"] += 1
            continue

        if args.skip_download:
            minute_rows = load_optional_rows(
                args.data_root / "1min" / f"{candidate.symbol}_1min_indicators.csv",
                min_date=candidate.trade_date,
            )
            trade_minutes = [row for row in minute_rows if row.datetime[:10] == candidate.trade_date]
        else:
            trade_minutes = fetch_cached_range(
                source_symbol,
                candidate.symbol,
                "1min",
                candidate.trade_date,
                exclusive_end(candidate.trade_date),
                api_key or "",
                args.data_root,
            )
            trade_minutes = [row for row in trade_minutes if row.datetime[:10] == candidate.trade_date]

        entry = confirm_candidate_entry(
            candidate,
            trade_minutes,
            args.above_ratio,
            args.min_minutes,
            "1min",
        )
        if not entry:
            stats["entry_rejected"] += 1
            continue

        entries.append(entry)
        stats["entries"] += 1
        if not args.skip_download:
            fetch_cached_range(source_symbol, entry.symbol, "1min", entry.trade_date, args.end, api_key or "", args.data_root)
            fetch_cached_range(source_symbol, entry.symbol, "1h", entry.trade_date, args.end, api_key or "", args.data_root)

        minute_rows = load_optional_rows(args.data_root / "1min" / f"{entry.symbol}_1min_indicators.csv", min_date=entry.trade_date)
        hourly_rows = load_optional_rows(args.data_root / "1h" / f"{entry.symbol}_1h_indicators.csv", min_date=entry.trade_date)
        trade = backtest_entry(
            entry_to_row(entry),
            daily_rows,
            minute_rows,
            hourly_rows,
            args.exit_below_ratio,
            args.advance_pct,
            args.define_bars,
            args.confirm_bars,
            args.exit_rule,
            args.ma_window,
        )
        trades.append(trade)
        stats["trades"] += 1

        if trade.status == "EXITED" and trade.exit_date:
            blocked_until = parse_date(trade.exit_date)
        else:
            blocked_until = parse_date(args.end) if args.end else date.max

    return symbol, candidates, entries, trades, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run waterline backtest with on-demand intraday downloads.")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None, help="End date, exclusive, for post-entry intraday data.")
    parser.add_argument("--apikey", default=None)
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--exit-rule", choices=["anchor", "ma5-or-signal"], default="anchor")
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--advance-pct", type=float, default=0.15)
    parser.add_argument("--define-bars", type=int, default=15)
    parser.add_argument("--confirm-bars", type=int, default=30)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent symbols to process.")
    parser.add_argument("--verbose", action="store_true", help="Print per-symbol completion details.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = args.apikey or CREDENTIALS_TWELVE_DATA_API_KEY
    if not args.skip_download and not api_key:
        raise SystemExit("Missing Twelve Data API key. Set credentials.TWELVE_DATA_API_KEY or pass --apikey.")

    if args.symbols_file:
        symbols = read_symbols_file(args.symbols_file)
    elif args.symbols:
        symbols = [safe_symbol(symbol.upper()) for symbol in args.symbols]
    else:
        symbols = available_daily_symbols(args.daily_dir)
    source_symbols = source_symbols_from_file(args.symbols_file)

    daily_cache: dict[str, list[Row]] = {}
    for symbol in symbols:
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        daily_cache[symbol] = daily_rows

    candidates: list[WaterlineCandidate] = []
    entries: list[WaterlineEntry] = []
    trades: list[WaterlineTrade] = []
    totals = collections.Counter()
    print(f"symbols={len(daily_cache)} workers={args.workers} skip_download={args.skip_download}", flush=True)
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
        futures = {
            executor.submit(
                process_symbol,
                symbol,
                source_symbols.get(symbol, symbol.replace("-", ".")),
                daily_rows,
                args,
                api_key,
            ): symbol
            for symbol, daily_rows in daily_cache.items()
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            symbol, symbol_candidates, symbol_entries, symbol_trades, stats = future.result()
            candidates.extend(symbol_candidates)
            entries.extend(symbol_entries)
            trades.extend(symbol_trades)
            totals.update(stats)
            if args.verbose or completed % 50 == 0 or completed == len(futures):
                print(
                    f"symbol_done {completed}/{len(futures)} {symbol} "
                    f"candidates={stats['candidates']} entries={stats['entries']} "
                    f"trades={stats['trades']} skipped_in_position={stats['skipped_in_position']}",
                    flush=True,
                )

    candidates.sort(key=lambda item: (item.trade_date, item.symbol, item.signal_date))
    entries.sort(key=lambda item: (item.trade_date, item.symbol, item.signal_date))
    trades.sort(key=lambda item: (item.trade_date, item.symbol, item.signal_date))
    candidates_path = args.output_dir / "waterline_candidates.csv"
    write_candidates(candidates, candidates_path)
    print(
        f"daily_candidates={len(candidates)} entries={len(entries)} trades={len(trades)} "
        f"entry_rejected={totals['entry_rejected']} skipped_in_position={totals['skipped_in_position']} "
        f"output={candidates_path}",
        flush=True,
    )

    entries_path = args.output_dir / "waterline_entries.csv"
    write_entries(entries, entries_path)
    print(f"entries={len(entries)} output={entries_path}", flush=True)

    trades_path = args.output_dir / "waterline_trades.csv"
    write_trades(trades, trades_path)
    print(f"trades={len(trades)} output={trades_path}", flush=True)
    summary_path = args.output_dir / "waterline_half_year_summary.csv"
    write_half_year_summary(trades, summary_path)
    print(f"summary={summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
