from __future__ import annotations

import argparse
import collections
import csv
import os
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from ad_structure_v05_core import Row, detect_ab_signals, evaluate_ad_structure, load_rows
from bottom_common import check_exit_signal, next_daily_row_after
from constants import DATA_ROOT, OUTPUT_ROOT
from fetch_sp500_2026_and_mark import fetch_twelve_data_bars, merge_rows, safe_symbol, write_rows
from waterline_signal import confirm_candidate_entry, read_symbols_file, scan_symbol_candidates


@dataclass(frozen=True)
class EntrySignal:
    strategy: str
    symbol: str
    signal_date: str
    signal_time: str
    reference_price: float
    structure_key: str = ""
    extra: dict[str, object] | None = None


@dataclass(frozen=True)
class TradeResult:
    strategy: str
    symbol: str
    signal_date: str
    signal_time: str
    entry_date: str
    entry_time: str
    entry_price: float
    reference_price: float
    exit_signal_date: str
    exit_date: str
    exit_time: str
    exit_price: float
    exit_rule: str
    exit_reference_price: float
    exit_below_ratio: float
    ma_below_ratio: float
    reference_below_ratio: float
    return_pct: float
    holding_days: int
    status: str
    structure_key: str = ""


def parse_date(text: str) -> date:
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def fmt(value: float) -> str:
    return f"{value:.6f}"


def source_symbols_from_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        if "," not in first_line:
            for line in f:
                symbol = line.strip()
                if symbol and not symbol.startswith("#"):
                    mapping[safe_symbol(symbol.upper())] = symbol.upper()
            return mapping
        reader = csv.DictReader(f)
        for row in reader:
            raw_symbol = (row.get("symbol") or row.get("source_symbol") or "").strip()
            source_symbol = (row.get("source_symbol") or raw_symbol).strip()
            if raw_symbol:
                mapping[safe_symbol(raw_symbol.upper())] = source_symbol or raw_symbol
    return mapping


def available_daily_symbols(daily_dir: Path) -> list[str]:
    suffix = "_1day_indicators.csv"
    if not daily_dir.exists():
        return []
    return sorted(path.name[: -len(suffix)] for path in daily_dir.glob(f"*{suffix}"))


def symbols_from_args(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [safe_symbol(symbol.upper()) for symbol in args.symbols]
    if args.symbols_file:
        return read_symbols_file(args.symbols_file)
    return available_daily_symbols(args.daily_dir)


def rows_by_date(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for row in rows:
        out.setdefault(row.datetime[:10], []).append(row)
    for items in out.values():
        items.sort(key=lambda row: row.datetime)
    return out


def should_keep_signal(date_text: str, start: str, entry_end: str | None) -> bool:
    if date_text < start:
        return False
    if entry_end and date_text >= entry_end:
        return False
    return True


def find_bottom_bm_entries(symbol: str, daily_path: Path, daily_rows: list[Row], start: str, entry_end: str | None) -> list[EntrySignal]:
    entries: list[EntrySignal] = []
    for sig in detect_ab_signals(symbol, "1day", daily_path, daily_rows):
        st = evaluate_ad_structure(daily_rows, sig)
        if not st.BM_break_time or st.BM_break_price is None:
            continue
        if st.failure_type in {"B_FAIL", "C_FAIL"} or st.structure_status in {"NO_BM_BREAK", "STRUCTURE_FAILED"}:
            continue
        signal_date = st.BM_break_time[:10]
        if not should_keep_signal(signal_date, start, entry_end):
            continue
        structure_key = f"{symbol}:{st.A_time}:{st.B_time}:{st.BM_break_time}"
        entries.append(
            EntrySignal(
                strategy="bottom_bm_break",
                symbol=symbol,
                signal_date=signal_date,
                signal_time=st.BM_break_time,
                reference_price=st.BM_break_price,
                structure_key=structure_key,
                extra={"bm_price": st.BM_price, "bm_break_price": st.BM_break_price},
            )
        )
    entries.sort(key=lambda item: (item.signal_date, item.signal_time, item.structure_key))
    return entries


def get_api_key(args: argparse.Namespace) -> str:
    if args.apikey:
        return args.apikey
    env_key = os.environ.get("TWELVE_DATA_API_KEY")
    if env_key:
        return env_key
    try:
        from credentials import TWELVE_DATA_API_KEY as credentials_api_key
    except Exception:
        credentials_api_key = ""
    return credentials_api_key or ""


def minute_cache_path(args: argparse.Namespace, symbol: str) -> Path:
    return args.minute_cache_dir / f"{symbol}_1min_indicators.csv"


def exclusive_end(date_text: str) -> str:
    return date.fromordinal(parse_date(date_text).toordinal() + 1).isoformat()


def get_minute_rows(
    symbol: str,
    source_symbol: str,
    date_text: str,
    args: argparse.Namespace,
    stats: collections.Counter,
) -> list[Row]:
    path = minute_cache_path(args, symbol)
    existing = load_rows(path, min_date=date_text) if path.exists() else []
    day_rows = [row for row in existing if row.datetime[:10] == date_text]
    if len(day_rows) >= args.min_minutes or args.skip_download:
        return day_rows
    api_key = get_api_key(args)
    if not api_key:
        stats["missing_api_key"] += 1
        return day_rows
    try:
        fetched = fetch_twelve_data_bars(source_symbol, "1min", date_text, exclusive_end(date_text), api_key)
    except Exception as exc:
        stats["minute_fetch_failed"] += 1
        if args.verbose:
            print(f"minute_fetch_failed symbol={symbol} date={date_text} error={type(exc).__name__}: {exc}", flush=True)
        return day_rows
    merged = merge_rows(existing, fetched)
    write_rows(path, merged)
    return [row for row in merged if row.datetime[:10] == date_text]


def entry_execution_row(daily_rows: list[Row], signal_date: str, mode: str) -> Row | None:
    if mode == "signal-close":
        for row in daily_rows:
            if row.datetime[:10] == signal_date:
                return row
        return None
    return next_daily_row_after(daily_rows, signal_date)


def row_entry_price(row: Row, mode: str) -> float:
    return row.close if mode == "signal-close" else row.open


def backtest_entry(
    entry: EntrySignal,
    daily_rows: list[Row],
    source_symbol: str,
    args: argparse.Namespace,
    stats: collections.Counter,
) -> TradeResult | None:
    entry_row = entry_execution_row(daily_rows, entry.signal_date, args.entry_price_mode)
    if entry_row is None:
        stats[f"{entry.strategy}_unexecutable"] += 1
        return None
    entry_date = entry_row.datetime[:10]
    entry_price = row_entry_price(entry_row, args.entry_price_mode)
    trade_dates = [row.datetime[:10] for row in daily_rows if row.datetime[:10] > entry_date]
    if args.end:
        trade_dates = [date_text for date_text in trade_dates if date_text < args.end]

    for holding_days, date_text in enumerate(trade_dates, start=1):
        minute_rows = get_minute_rows(entry.symbol, source_symbol, date_text, args, stats)
        check = check_exit_signal(
            daily_rows,
            minute_rows,
            date_text,
            entry.reference_price,
            args.exit_below_ratio,
            args.ma_window,
            args.min_minutes,
        )
        if not check.triggered:
            continue
        exit_row = next_daily_row_after(daily_rows, date_text)
        if exit_row is None:
            stats[f"{entry.strategy}_exit_unexecutable"] += 1
            break
        if args.end and exit_row.datetime[:10] >= args.end:
            break
        exit_price = exit_row.open
        return TradeResult(
            strategy=entry.strategy,
            symbol=entry.symbol,
            signal_date=entry.signal_date,
            signal_time=entry.signal_time,
            entry_date=entry_date,
            entry_time=entry_row.datetime,
            entry_price=entry_price,
            reference_price=entry.reference_price,
            exit_signal_date=date_text,
            exit_date=exit_row.datetime[:10],
            exit_time=exit_row.datetime,
            exit_price=exit_price,
            exit_rule=check.rule,
            exit_reference_price=check.reference_price,
            exit_below_ratio=check.below_ratio,
            ma_below_ratio=check.ma_below_ratio,
            reference_below_ratio=check.signal_below_ratio,
            return_pct=(exit_price / entry_price - 1) * 100,
            holding_days=holding_days,
            status="EXITED",
            structure_key=entry.structure_key,
        )

    valuation_rows = [row for row in daily_rows if row.datetime[:10] >= entry_date]
    if args.end:
        valuation_rows = [row for row in valuation_rows if row.datetime[:10] < args.end]
    last_row = valuation_rows[-1] if valuation_rows else entry_row
    return TradeResult(
        strategy=entry.strategy,
        symbol=entry.symbol,
        signal_date=entry.signal_date,
        signal_time=entry.signal_time,
        entry_date=entry_date,
        entry_time=entry_row.datetime,
        entry_price=entry_price,
        reference_price=entry.reference_price,
        exit_signal_date="",
        exit_date="",
        exit_time="",
        exit_price=0.0,
        exit_rule="",
        exit_reference_price=0.0,
        exit_below_ratio=0.0,
        ma_below_ratio=0.0,
        reference_below_ratio=0.0,
        return_pct=(last_row.close / entry_price - 1) * 100,
        holding_days=max(0, (parse_date(last_row.datetime) - parse_date(entry_date)).days),
        status="OPEN",
        structure_key=entry.structure_key,
    )


def find_waterline_entries(
    symbol: str,
    daily_rows: list[Row],
    source_symbol: str,
    args: argparse.Namespace,
    stats: collections.Counter,
) -> list[EntrySignal]:
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


def half_year(date_text: str) -> str:
    month = int(date_text[5:7])
    return f"{date_text[:4]}H{1 if month <= 6 else 2}"


def summarize(rows: list[dict[str, object]], key_fields: list[str]) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], list[dict[str, object]]] = collections.defaultdict(list)
    for row in rows:
        groups[tuple(row[field] for field in key_fields)].append(row)
    out: list[dict[str, object]] = []
    for key, items in sorted(groups.items()):
        returns = [float(item["return_pct"]) for item in items]
        wins = sum(1 for value in returns if value > 0)
        closed = sum(1 for item in items if item["status"] == "EXITED")
        row: dict[str, object] = {field: value for field, value in zip(key_fields, key)}
        row.update(
            {
                "trades": len(items),
                "closed_trades": closed,
                "open_trades": len(items) - closed,
                "wins": wins,
                "losses": len(items) - wins,
                "win_rate": fmt(wins / len(items)) if items else "0.000000",
                "avg_return_pct": fmt(sum(returns) / len(returns)) if returns else "0.000000",
                "median_return_pct": fmt(statistics.median(returns)) if returns else "0.000000",
                "sum_return_pct": fmt(sum(returns)),
                "avg_holding_days": fmt(sum(int(item["holding_days"]) for item in items) / len(items)) if items else "0.000000",
                "max_return_pct": fmt(max(returns)) if returns else "0.000000",
                "min_return_pct": fmt(min(returns)) if returns else "0.000000",
            }
        )
        out.append(row)
    return out


def write_dict_rows(path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def trade_to_row(trade: TradeResult) -> dict[str, object]:
    row = asdict(trade)
    for key, value in list(row.items()):
        if isinstance(value, float):
            row[key] = fmt(value)
    row["entry_half_year"] = half_year(trade.entry_date)
    return row


def run_symbol(
    symbol: str,
    source_symbol: str,
    args: argparse.Namespace,
    stats: collections.Counter,
) -> list[TradeResult]:
    daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
    if not daily_path.exists():
        stats["missing_daily"] += 1
        return []
    daily_rows = load_rows(daily_path, min_date=args.scan_start)
    if not daily_rows:
        stats["empty_daily"] += 1
        return []
    entries: list[EntrySignal] = []
    if args.strategy in {"bottom", "both"}:
        bottom_entries = find_bottom_bm_entries(symbol, daily_path, daily_rows, args.start, args.entry_end)
        entries.extend(bottom_entries)
        stats["bottom_bm_break_signals"] += len(bottom_entries)
    if args.strategy in {"waterline", "both"}:
        waterline_entries = find_waterline_entries(symbol, daily_rows, source_symbol, args, stats)
        entries.extend(waterline_entries)
        stats["waterline_ma5_signals"] += len(waterline_entries)

    entries.sort(key=lambda item: (item.strategy, item.signal_date, item.signal_time))
    trades: list[TradeResult] = []
    blocked_until_by_strategy: dict[str, date] = {}
    for entry in entries:
        blocked_until = blocked_until_by_strategy.get(entry.strategy)
        entry_row = entry_execution_row(daily_rows, entry.signal_date, args.entry_price_mode)
        if entry_row is None:
            stats[f"{entry.strategy}_unexecutable"] += 1
            continue
        if blocked_until is not None and parse_date(entry_row.datetime) <= blocked_until:
            stats[f"{entry.strategy}_skipped_in_position"] += 1
            continue
        trade = backtest_entry(entry, daily_rows, source_symbol, args, stats)
        if trade is None:
            continue
        trades.append(trade)
        stats[f"{entry.strategy}_trades"] += 1
        if trade.status == "EXITED" and trade.exit_date:
            blocked_until_by_strategy[entry.strategy] = parse_date(trade.exit_date)
        else:
            blocked_until_by_strategy[entry.strategy] = date.max
    return trades


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest new UpBottom BM-break and waterline signal systems.")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--minute-cache-dir", type=Path, default=OUTPUT_ROOT / "backtest_minute_cache" / "1min")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT / "backtests")
    parser.add_argument("--strategy", choices=["bottom", "waterline", "both"], default="both")
    parser.add_argument("--start", default="2025-10-01", help="Inclusive first entry signal date.")
    parser.add_argument("--scan-start", default="2000-01-01", help="Earlier daily warmup date for signal detection.")
    parser.add_argument("--entry-end", default=None, help="Exclusive last entry signal date.")
    parser.add_argument("--end", default=None, help="Exclusive last valuation/exit execution date.")
    parser.add_argument("--entry-price-mode", choices=["next-open", "signal-close"], default="next-open")
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--apikey", default=None)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="Reserved for future parallel backtests; current run is sequential.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = symbols_from_args(args)
    source_symbols = source_symbols_from_file(args.symbols_file)
    all_trades: list[TradeResult] = []
    stats: collections.Counter = collections.Counter()
    for ordinal, symbol in enumerate(symbols, start=1):
        source_symbol = source_symbols.get(symbol, symbol.replace("-", "."))
        all_trades.extend(run_symbol(symbol, source_symbol, args, stats))
        if args.verbose or ordinal % 100 == 0 or ordinal == len(symbols):
            print(f"symbol_done {ordinal}/{len(symbols)} {symbol}", flush=True)

    all_trades.sort(key=lambda item: (item.strategy, item.entry_date, item.symbol, item.signal_time))
    rows = [trade_to_row(trade) for trade in all_trades]
    fieldnames = list(trade_to_row(all_trades[0]).keys()) if all_trades else list(TradeResult.__dataclass_fields__.keys()) + ["entry_half_year"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.strategy in {"bottom", "both"}:
        bottom_rows = [row for row in rows if row["strategy"] == "bottom_bm_break"]
        write_dict_rows(args.output_dir / "bottom_bm_break_trades.csv", bottom_rows, fieldnames)
        write_dict_rows(args.output_dir / "bottom_bm_break_half_year_summary.csv", summarize(bottom_rows, ["strategy", "entry_half_year"]))
    if args.strategy in {"waterline", "both"}:
        waterline_rows = [row for row in rows if row["strategy"] == "waterline_ma5"]
        write_dict_rows(args.output_dir / "waterline_ma5_trades.csv", waterline_rows, fieldnames)
        write_dict_rows(args.output_dir / "waterline_ma5_half_year_summary.csv", summarize(waterline_rows, ["strategy", "entry_half_year"]))
    write_dict_rows(args.output_dir / "all_trades.csv", rows, fieldnames)
    write_dict_rows(args.output_dir / "all_half_year_summary.csv", summarize(rows, ["strategy", "entry_half_year"]))
    print("stats=" + " ".join(f"{key}={stats[key]}" for key in sorted(stats)), flush=True)
    print(f"trades={len(rows)} output_dir={args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
