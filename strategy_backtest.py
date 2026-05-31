"""Backtest bottom-divergence and waterline entries with a shared exit rule.

This module is intentionally separate from alert delivery. It uses:

- bottom divergence: buy at D_TRIGGERED close
- waterline: buy at confirmed trade-day close
- shared exit: sell when at least 50% of a day's minute closes are below
  either prior MA5 or the strategy reference close
"""

from __future__ import annotations

import argparse
import collections
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from ad_structure_v05_core import Row, detect_ab_signals, evaluate_ad_structure, load_rows
from fetch_sp500_2026_and_mark import (
    CREDENTIALS_TWELVE_DATA_API_KEY,
    DATA_ROOT,
    OUTPUT_ROOT,
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
)


@dataclass(frozen=True)
class StrategyTrade:
    strategy: str
    symbol: str
    signal_date: str
    signal_time: str
    entry_date: str
    entry_time: str
    entry_price: float
    reference_price: float
    exit_date: str
    exit_time: str
    exit_price: float
    exit_rule: str
    exit_reference_price: float
    exit_below_ratio: float
    return_pct: float
    holding_days: int
    status: str


@dataclass(frozen=True)
class BottomDivergenceEntry:
    symbol: str
    signal_date: str
    signal_time: str
    entry_date: str
    entry_time: str
    entry_price: float
    reference_price: float


def parse_date(text: str) -> date:
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def exclusive_end(start: str, days: int = 1) -> str:
    return (parse_date(start) + timedelta(days=days)).isoformat()


def fmt(value: float) -> str:
    return f"{value:.6f}"


def rows_by_date(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for row in rows:
        out.setdefault(row.datetime[:10], []).append(row)
    for date_text in out:
        out[date_text].sort(key=lambda item: item.datetime)
    return out


def daily_close_by_date(rows: list[Row]) -> dict[str, Row]:
    return {row.datetime[:10]: row for row in rows}


def prior_ma_by_date(rows: list[Row], window: int) -> dict[str, float]:
    out: dict[str, float] = {}
    prior_closes: list[float] = []
    for row in rows:
        date_text = row.datetime[:10]
        if len(prior_closes) >= window:
            out[date_text] = sum(prior_closes[-window:]) / window
        prior_closes.append(row.close)
    return out


def below_ratio(rows: list[Row], price: float) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row.close < price) / len(rows)


def half_year(date_text: str) -> str:
    dt = parse_date(date_text)
    return f"{dt.year}H{1 if dt.month <= 6 else 2}"


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


def entry_to_row(entry: WaterlineEntry) -> dict[str, str]:
    row = asdict(entry)
    for key, value in row.items():
        row[key] = fmt(value) if isinstance(value, float) else str(value)
    return row


def find_bottom_entries(symbol: str, daily_path: Path, daily_rows: list[Row], start: str) -> list[BottomDivergenceEntry]:
    entries: list[BottomDivergenceEntry] = []
    for sig in detect_ab_signals(symbol, "1day", daily_path, daily_rows):
        st = evaluate_ad_structure(daily_rows, sig)
        if st.structure_status != "D_TRIGGERED" or not st.D_time or st.D_price is None:
            continue
        if st.D_time[:10] < start:
            continue
        entries.append(
            BottomDivergenceEntry(
                symbol=symbol,
                signal_date=st.D_time[:10],
                signal_time=st.D_time,
                entry_date=st.D_time[:10],
                entry_time=st.D_time,
                entry_price=st.D_price,
                reference_price=st.D_price,
            )
        )
    entries.sort(key=lambda item: (item.entry_date, item.signal_time))
    return entries


def backtest_shared_exit(
    strategy: str,
    symbol: str,
    signal_date: str,
    signal_time: str,
    entry_date: str,
    entry_time: str,
    entry_price: float,
    reference_price: float,
    daily_rows: list[Row],
    minute_rows: list[Row],
    below_threshold: float,
    ma_window: int,
    min_exit_minutes: int,
) -> StrategyTrade:
    daily_by_date = daily_close_by_date(daily_rows)
    minute_by_date = rows_by_date(minute_rows)
    ma_by_date = prior_ma_by_date(daily_rows, ma_window)
    dates = sorted(date_text for date_text in daily_by_date if date_text > entry_date)
    for holding_index, date_text in enumerate(dates, start=1):
        day_minutes = minute_by_date.get(date_text, [])
        if len(day_minutes) < min_exit_minutes:
            continue
        ma_price = ma_by_date.get(date_text)
        ma_ratio = below_ratio(day_minutes, ma_price) if ma_price is not None else 0.0
        signal_ratio = below_ratio(day_minutes, reference_price)
        exit_rule = ""
        exit_reference_price = 0.0
        exit_ratio = 0.0
        if ma_ratio >= below_threshold:
            exit_rule = f"MA{ma_window}"
            exit_reference_price = ma_price if ma_price is not None else 0.0
            exit_ratio = ma_ratio
        elif signal_ratio >= below_threshold:
            exit_rule = "REFERENCE_CLOSE"
            exit_reference_price = reference_price
            exit_ratio = signal_ratio
        if not exit_rule:
            continue
        exit_row = daily_by_date[date_text]
        return StrategyTrade(
            strategy=strategy,
            symbol=symbol,
            signal_date=signal_date,
            signal_time=signal_time,
            entry_date=entry_date,
            entry_time=entry_time,
            entry_price=entry_price,
            reference_price=reference_price,
            exit_date=date_text,
            exit_time=exit_row.datetime,
            exit_price=exit_row.close,
            exit_rule=exit_rule,
            exit_reference_price=exit_reference_price,
            exit_below_ratio=exit_ratio,
            return_pct=(exit_row.close / entry_price - 1) * 100,
            holding_days=holding_index,
            status="EXITED",
        )

    last_date = dates[-1] if dates else entry_date
    last_row = daily_by_date.get(last_date)
    last_price = last_row.close if last_row else entry_price
    return StrategyTrade(
        strategy=strategy,
        symbol=symbol,
        signal_date=signal_date,
        signal_time=signal_time,
        entry_date=entry_date,
        entry_time=entry_time,
        entry_price=entry_price,
        reference_price=reference_price,
        exit_date="",
        exit_time="",
        exit_price=0.0,
        exit_rule="",
        exit_reference_price=0.0,
        exit_below_ratio=0.0,
        return_pct=(last_price / entry_price - 1) * 100,
        holding_days=len(dates),
        status="OPEN",
    )


def write_trades(trades: list[StrategyTrade], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(StrategyTrade.__dataclass_fields__.keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            row = asdict(trade)
            for key, value in row.items():
                if isinstance(value, float):
                    row[key] = fmt(value)
            writer.writerow(row)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    items = sorted(values)
    mid = len(items) // 2
    if len(items) % 2 == 0:
        return (items[mid - 1] + items[mid]) / 2
    return items[mid]


def write_half_year_summary(trades: list[StrategyTrade], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[StrategyTrade]] = collections.defaultdict(list)
    for trade in trades:
        groups[half_year(trade.entry_date)].append(trade)
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
        "max_return_pct",
        "min_return_pct",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for period in sorted(groups):
            items = groups[period]
            returns = [trade.return_pct for trade in items]
            wins = sum(1 for value in returns if value > 0)
            closed = sum(1 for trade in items if trade.status == "EXITED")
            compound = 1.0
            for value in returns:
                compound *= 1 + value / 100
            writer.writerow(
                {
                    "period": period,
                    "trades": len(items),
                    "closed_trades": closed,
                    "open_trades": len(items) - closed,
                    "wins": wins,
                    "losses": len(items) - wins,
                    "win_rate": fmt(wins / len(items)) if items else "0.000000",
                    "avg_return_pct": fmt(sum(returns) / len(returns)) if returns else "0.000000",
                    "median_return_pct": fmt(median(returns)),
                    "sum_return_pct": fmt(sum(returns)),
                    "compound_return_pct": fmt((compound - 1) * 100),
                    "avg_holding_days": fmt(sum(trade.holding_days for trade in items) / len(items)) if items else "0.000000",
                    "max_return_pct": fmt(max(returns)) if returns else "0.000000",
                    "min_return_pct": fmt(min(returns)) if returns else "0.000000",
                }
            )


def process_symbol(
    symbol: str,
    source_symbol: str,
    daily_rows: list[Row],
    daily_path: Path,
    args: argparse.Namespace,
    api_key: str | None,
) -> tuple[str, list[StrategyTrade], list[StrategyTrade], dict[str, int]]:
    bottom_trades: list[StrategyTrade] = []
    waterline_trades: list[StrategyTrade] = []
    stats = collections.Counter()

    if args.strategy in {"bottom", "both"}:
        bottom_entries = find_bottom_entries(symbol, daily_path, daily_rows, args.start)
        stats["bottom_entries"] = len(bottom_entries)
        blocked_until: date | None = None
        for entry in bottom_entries:
            entry_dt = parse_date(entry.entry_date)
            if blocked_until is not None and entry_dt <= blocked_until:
                stats["bottom_skipped_in_position"] += 1
                continue
            minute_rows = get_minute_rows(symbol, source_symbol, entry.entry_date, args, api_key)
            trade = backtest_shared_exit(
                "bottom_divergence",
                symbol,
                entry.signal_date,
                entry.signal_time,
                entry.entry_date,
                entry.entry_time,
                entry.entry_price,
                entry.reference_price,
                daily_rows,
                minute_rows,
                args.exit_below_ratio,
                args.ma_window,
                args.min_exit_minutes,
            )
            bottom_trades.append(trade)
            stats["bottom_trades"] += 1
            blocked_until = parse_date(trade.exit_date) if trade.status == "EXITED" and trade.exit_date else date.max

    if args.strategy in {"waterline", "both"}:
        candidates = scan_symbol_candidates(symbol, daily_rows, args.volume_lookback, args.volume_multiple, args.candle_k)
        candidates.sort(key=lambda item: (item.trade_date, item.signal_date))
        stats["waterline_candidates"] = len(candidates)
        blocked_until = None
        for candidate in candidates:
            trade_dt = parse_date(candidate.trade_date)
            if blocked_until is not None and trade_dt <= blocked_until:
                stats["waterline_skipped_in_position"] += 1
                continue
            trade_minutes = get_trade_day_minutes(symbol, source_symbol, candidate, args, api_key)
            entry = confirm_candidate_entry(candidate, trade_minutes, args.above_ratio, args.min_entry_minutes, "1min")
            if not entry:
                stats["waterline_entry_rejected"] += 1
                continue
            minute_rows = get_minute_rows(symbol, source_symbol, entry.trade_date, args, api_key)
            trade = backtest_shared_exit(
                "waterline",
                symbol,
                entry.signal_date,
                entry.signal_time,
                entry.trade_date,
                entry.entry_time,
                entry.entry_price,
                entry.signal_close,
                daily_rows,
                minute_rows,
                args.exit_below_ratio,
                args.ma_window,
                args.min_exit_minutes,
            )
            waterline_trades.append(trade)
            stats["waterline_trades"] += 1
            blocked_until = parse_date(trade.exit_date) if trade.status == "EXITED" and trade.exit_date else date.max

    return symbol, bottom_trades, waterline_trades, dict(stats)


def get_trade_day_minutes(
    symbol: str,
    source_symbol: str,
    candidate: WaterlineCandidate,
    args: argparse.Namespace,
    api_key: str | None,
) -> list[Row]:
    if args.skip_download:
        minute_rows = load_optional_rows(args.data_root / "1min" / f"{symbol}_1min_indicators.csv", min_date=candidate.trade_date)
    else:
        minute_rows = fetch_cached_range(
            source_symbol,
            symbol,
            "1min",
            candidate.trade_date,
            exclusive_end(candidate.trade_date),
            api_key or "",
            args.data_root,
        )
    return [row for row in minute_rows if row.datetime[:10] == candidate.trade_date]


def get_minute_rows(
    symbol: str,
    source_symbol: str,
    entry_date: str,
    args: argparse.Namespace,
    api_key: str | None,
) -> list[Row]:
    if args.skip_download:
        return load_optional_rows(args.data_root / "1min" / f"{symbol}_1min_indicators.csv", min_date=entry_date)
    return fetch_cached_range(source_symbol, symbol, "1min", entry_date, args.end, api_key or "", args.data_root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest bottom-divergence and waterline entries with shared MA5/reference exits.")
    parser.add_argument("--strategy", choices=["bottom", "waterline", "both"], default="both")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None, help="End date, exclusive, for post-entry minute data.")
    parser.add_argument("--apikey", default=None)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--min-exit-minutes", type=int, default=300)
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--min-entry-minutes", type=int, default=300)
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

    daily_cache: dict[str, tuple[list[Row], Path]] = {}
    for symbol in symbols:
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        if daily_rows:
            daily_cache[symbol] = (daily_rows, daily_path)

    totals = collections.Counter()
    bottom_trades: list[StrategyTrade] = []
    waterline_trades: list[StrategyTrade] = []
    print(
        f"strategy={args.strategy} symbols={len(daily_cache)} workers={args.workers} "
        f"skip_download={args.skip_download}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
        futures = {
            executor.submit(
                process_symbol,
                symbol,
                source_symbols.get(symbol, symbol.replace("-", ".")),
                daily_rows,
                daily_path,
                args,
                api_key,
            ): symbol
            for symbol, (daily_rows, daily_path) in daily_cache.items()
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            symbol, symbol_bottom, symbol_waterline, stats = future.result()
            bottom_trades.extend(symbol_bottom)
            waterline_trades.extend(symbol_waterline)
            totals.update(stats)
            if args.verbose or completed % 50 == 0 or completed == len(futures):
                print(
                    f"symbol_done {completed}/{len(futures)} {symbol} "
                    f"bottom_trades={stats.get('bottom_trades', 0)} "
                    f"waterline_trades={stats.get('waterline_trades', 0)}",
                    flush=True,
                )

    bottom_trades.sort(key=lambda item: (item.entry_date, item.symbol, item.signal_time))
    waterline_trades.sort(key=lambda item: (item.entry_date, item.symbol, item.signal_time))
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.strategy in {"bottom", "both"}:
        trades_path = args.output_dir / "bottom_divergence_trades.csv"
        summary_path = args.output_dir / "bottom_divergence_half_year_summary.csv"
        write_trades(bottom_trades, trades_path)
        write_half_year_summary(bottom_trades, summary_path)
        print(f"bottom_trades={len(bottom_trades)} output={trades_path} summary={summary_path}", flush=True)

    if args.strategy in {"waterline", "both"}:
        trades_path = args.output_dir / "waterline_ma5_trades.csv"
        summary_path = args.output_dir / "waterline_ma5_half_year_summary.csv"
        write_trades(waterline_trades, trades_path)
        write_half_year_summary(waterline_trades, summary_path)
        print(f"waterline_trades={len(waterline_trades)} output={trades_path} summary={summary_path}", flush=True)

    print("stats=" + " ".join(f"{key}={totals[key]}" for key in sorted(totals)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
