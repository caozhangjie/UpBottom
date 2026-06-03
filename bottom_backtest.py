from __future__ import annotations

import argparse
import collections
from datetime import date
from pathlib import Path

from ad_structure_v05_core import Row, detect_ab_signals, evaluate_ad_structure, load_rows
from backtest_common import (
    EntrySignal,
    add_common_backtest_args,
    backtest_entry,
    entry_execution_row,
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


ENTRY_VARIANT_TO_STRATEGY = {
    "bm": "bottom_bm_break",
    "c": "bottom_c_confirm",
    "d": "bottom_d_trigger",
}


def is_valid_bm_structure(st) -> bool:
    if not st.BM_break_time or st.BM_break_price is None:
        return False
    if st.failure_type in {"B_FAIL", "C_FAIL"}:
        return False
    if st.structure_status in {"NO_BM_BREAK", "STRUCTURE_FAILED"}:
        return False
    return True


def c_confirmation(rows: list[Row], c_sequence: list[dict] | None) -> Row | None:
    if not c_sequence:
        return None
    c_index = int(c_sequence[0]["index"])
    confirm_index = c_index + 4
    if confirm_index >= len(rows):
        return None
    return rows[confirm_index]


def find_bottom_entries(
    symbol: str,
    daily_path: Path,
    daily_rows: list[Row],
    variants: set[str],
    start: str,
    entry_end: str | None,
    completed_only: bool,
) -> list[EntrySignal]:
    entries: list[EntrySignal] = []
    for sig in detect_ab_signals(symbol, "1day", daily_path, daily_rows):
        st = evaluate_ad_structure(daily_rows, sig)
        if completed_only and st.structure_status != "D_TRIGGERED":
            continue
        structure_key = f"{symbol}:{st.A_time}:{st.B_time}:{st.BM_break_time or st.D_time or ''}"

        if "bm" in variants and is_valid_bm_structure(st):
            signal_date = st.BM_break_time[:10]
            if should_keep_signal(signal_date, start, entry_end):
                entries.append(
                    EntrySignal(
                        strategy=ENTRY_VARIANT_TO_STRATEGY["bm"],
                        symbol=symbol,
                        signal_date=signal_date,
                        signal_time=st.BM_break_time,
                        reference_price=st.BM_break_price,
                        structure_key=structure_key,
                        extra={"entry_variant": "bm", "bm_price": st.BM_price, "bm_break_price": st.BM_break_price},
                    )
                )

        if "c" in variants and is_valid_bm_structure(st):
            confirm_row = c_confirmation(daily_rows, st.C_sequence)
            if confirm_row is not None:
                signal_date = confirm_row.datetime[:10]
                if should_keep_signal(signal_date, start, entry_end):
                    entries.append(
                        EntrySignal(
                            strategy=ENTRY_VARIANT_TO_STRATEGY["c"],
                            symbol=symbol,
                            signal_date=signal_date,
                            signal_time=confirm_row.datetime,
                            reference_price=confirm_row.close,
                            structure_key=structure_key,
                            extra={"entry_variant": "c", "c_confirm_price": confirm_row.close},
                        )
                    )

        if "d" in variants and st.structure_status == "D_TRIGGERED" and st.D_time and st.D_price is not None:
            signal_date = st.D_time[:10]
            if should_keep_signal(signal_date, start, entry_end):
                entries.append(
                    EntrySignal(
                        strategy=ENTRY_VARIANT_TO_STRATEGY["d"],
                        symbol=symbol,
                        signal_date=signal_date,
                        signal_time=st.D_time,
                        reference_price=st.D_price,
                        structure_key=structure_key,
                        extra={"entry_variant": "d", "d_price": st.D_price},
                    )
                )

    entries.sort(key=lambda item: (item.strategy, item.signal_date, item.signal_time, item.structure_key))
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
    entries = find_bottom_entries(
        symbol,
        daily_path,
        daily_rows,
        set(args.entry_variants),
        args.start,
        args.entry_end,
        args.completed_only,
    )
    for variant in args.entry_variants:
        stats[f"{ENTRY_VARIANT_TO_STRATEGY[variant]}_signals"] += sum(
            1 for entry in entries if entry.strategy == ENTRY_VARIANT_TO_STRATEGY[variant]
        )

    trades = []
    blocked_until_by_strategy: dict[str, date] = {}
    for entry in entries:
        entry_row = entry_execution_row(daily_rows, entry.signal_date, args.entry_price_mode)
        if entry_row is None:
            stats[f"{entry.strategy}_unexecutable"] += 1
            continue
        blocked_until = blocked_until_by_strategy.get(entry.strategy)
        if blocked_until is not None and parse_date(entry_row.datetime) <= blocked_until:
            stats[f"{entry.strategy}_skipped_in_position"] += 1
            continue
        trade = backtest_entry(entry, daily_rows, source_symbol, args, stats)
        if trade is None:
            continue
        trades.append(trade)
        stats[f"{entry.strategy}_trades"] += 1
        blocked_until_by_strategy[entry.strategy] = parse_date(trade.exit_date) if trade.status == "EXITED" and trade.exit_date else date.max
    return trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest bottom-divergence BM/C/D entry variants.")
    add_common_backtest_args(parser, OUTPUT_ROOT / "backtests" / "bottom")
    parser.add_argument("--entry-variants", nargs="+", choices=["bm", "c", "d"], default=["bm", "c", "d"])
    parser.add_argument(
        "--completed-only",
        action="store_true",
        help="Use only structures that eventually reached D_TRIGGERED; useful for fair BM/C/D entry comparison.",
    )
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

    trades.sort(key=lambda item: (item.strategy, item.entry_date, item.symbol, item.signal_time))
    rows = [trade_to_row(trade) for trade in trades]
    fieldnames = list(rows[0].keys()) if rows else trade_fieldnames()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_dict_rows(args.output_dir / "bottom_trades.csv", rows, fieldnames)
    write_dict_rows(args.output_dir / "bottom_summary.csv", summarize(rows, ["strategy"]), None)
    write_dict_rows(args.output_dir / "bottom_half_year_summary.csv", summarize(rows, ["strategy", "entry_half_year"]), None)
    for variant in args.entry_variants:
        strategy = ENTRY_VARIANT_TO_STRATEGY[variant]
        variant_rows = [row for row in rows if row["strategy"] == strategy]
        write_dict_rows(args.output_dir / f"{strategy}_trades.csv", variant_rows, fieldnames)
        write_dict_rows(args.output_dir / f"{strategy}_half_year_summary.csv", summarize(variant_rows, ["strategy", "entry_half_year"]), None)
    print("stats=" + " ".join(f"{key}={stats[key]}" for key in sorted(stats)), flush=True)
    print(f"trades={len(rows)} output_dir={args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
