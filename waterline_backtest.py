from __future__ import annotations

import argparse
import collections
from datetime import date

from ad_structure_v05_core import load_rows
from backtest_common import (
    EntrySignal,
    add_common_backtest_args,
    backtest_entry,
    entry_execution_row,
    get_minute_rows,
    parse_date,
    should_keep_signal,
    source_symbols_from_file,
    summarize,
    trade_fieldnames,
    symbols_from_args,
    trade_to_row,
    write_dict_rows,
)
from constants import OUTPUT_ROOT
from waterline_signal import confirm_candidate_entry, scan_symbol_candidates


def find_waterline_entries(symbol: str, daily_rows, source_symbol: str, args: argparse.Namespace, stats: collections.Counter) -> list[EntrySignal]:
    entries: list[EntrySignal] = []
    for candidate in scan_symbol_candidates(symbol, daily_rows, args.volume_lookback, args.volume_multiple, args.candle_k):
        if not should_keep_signal(candidate.trade_date, args.start, args.entry_end):
            continue
        minute_rows = get_minute_rows(symbol, source_symbol, candidate.trade_date, args, stats)
        confirmed = confirm_candidate_entry(candidate, minute_rows, args.above_ratio, args.min_minutes, "1min")
        if not confirmed:
            continue
        entries.append(
            EntrySignal(
                strategy="waterline_ma5",
                symbol=symbol,
                signal_date=confirmed.trade_date,
                signal_time=confirmed.entry_time,
                reference_price=confirmed.signal_close,
                structure_key=f"{symbol}:{confirmed.signal_date}:{confirmed.trade_date}",
                extra={"signal_day": confirmed.signal_date, "minute_above_ratio": confirmed.minute_above_ratio},
            )
        )
    entries.sort(key=lambda item: (item.signal_date, item.signal_time, item.structure_key))
    return entries


def run_symbol(symbol: str, source_symbol: str, args: argparse.Namespace, stats: collections.Counter):
    daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
    if not daily_path.exists():
        stats["missing_daily"] += 1
        return []
    daily_rows = load_rows(daily_path, min_date=args.scan_start)
    if not daily_rows:
        stats["empty_daily"] += 1
        return []
    entries = find_waterline_entries(symbol, daily_rows, source_symbol, args, stats)
    stats["waterline_ma5_signals"] += len(entries)

    trades = []
    blocked_until: date | None = None
    for entry in entries:
        entry_row = entry_execution_row(daily_rows, entry.signal_date, args.entry_price_mode)
        if entry_row is None:
            stats["waterline_ma5_unexecutable"] += 1
            continue
        if blocked_until is not None and parse_date(entry_row.datetime) <= blocked_until:
            stats["waterline_ma5_skipped_in_position"] += 1
            continue
        trade = backtest_entry(entry, daily_rows, source_symbol, args, stats)
        if trade is None:
            continue
        trades.append(trade)
        stats["waterline_ma5_trades"] += 1
        blocked_until = parse_date(trade.exit_date) if trade.status == "EXITED" and trade.exit_date else date.max
    return trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest waterline signal-day/trade-day entries.")
    add_common_backtest_args(parser, OUTPUT_ROOT / "backtests" / "waterline")
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = symbols_from_args(args)
    source_symbols = source_symbols_from_file(args.symbols_file)
    stats: collections.Counter = collections.Counter()
    trades = []
    for ordinal, symbol in enumerate(symbols, start=1):
        source_symbol = source_symbols.get(symbol, symbol.replace("-", "."))
        trades.extend(run_symbol(symbol, source_symbol, args, stats))
        if args.verbose or ordinal % 100 == 0 or ordinal == len(symbols):
            print(f"symbol_done {ordinal}/{len(symbols)} {symbol}", flush=True)

    trades.sort(key=lambda item: (item.entry_date, item.symbol, item.signal_time))
    rows = [trade_to_row(trade) for trade in trades]
    fieldnames = list(rows[0].keys()) if rows else trade_fieldnames()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_dict_rows(args.output_dir / "waterline_ma5_trades.csv", rows, fieldnames)
    write_dict_rows(args.output_dir / "waterline_ma5_summary.csv", summarize(rows, ["strategy"]), None)
    write_dict_rows(args.output_dir / "waterline_ma5_half_year_summary.csv", summarize(rows, ["strategy", "entry_half_year"]), None)
    print("stats=" + " ".join(f"{key}={stats[key]}" for key in sorted(stats)), flush=True)
    print(f"trades={len(rows)} output_dir={args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
