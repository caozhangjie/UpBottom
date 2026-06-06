"""Waterline trend signal scanner.

This module is intentionally separate from the bottom-divergence scanner. It
finds trend-style waterline candidates and confirmed entries:

- candidate: a small rising wave, with price above a rising MA20
- entry: next trading day has enough minute closes above signal close
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from ad_structure_v05_core import Row, load_rows
from fetch_sp500_2026_and_mark import DATA_ROOT, OUTPUT_ROOT, safe_symbol


@dataclass(frozen=True)
class WaterlineEntry:
    symbol: str
    signal_date: str
    signal_time: str
    signal_open: float
    signal_high: float
    signal_low: float
    signal_close: float
    signal_volume: float
    prior_volume_avg: float
    volume_ratio: float
    candle_k: float
    trend_lookback: int
    trend_up_days: int
    trend_return: float
    signal_return: float
    signal_return_lookback: int
    ma_window: int
    ma_price: float
    ma_slope_lookback: int
    ma_slope: float
    trade_date: str
    minute_timeframe: str
    minute_total: int
    minute_above: int
    minute_above_ratio: float
    entry_time: str
    entry_price: float


@dataclass(frozen=True)
class WaterlineCandidate:
    symbol: str
    signal_date: str
    signal_time: str
    signal_open: float
    signal_high: float
    signal_low: float
    signal_close: float
    signal_volume: float
    prior_volume_avg: float
    volume_ratio: float
    candle_k: float
    trend_lookback: int
    trend_up_days: int
    trend_return: float
    signal_return: float
    signal_return_lookback: int
    ma_window: int
    ma_price: float
    ma_slope_lookback: int
    ma_slope: float
    trade_date: str
    trade_time: str
    trade_close: float


def fmt(value: float) -> str:
    return f"{value:.6f}"


def read_symbols_file(path: Path) -> list[str]:
    symbols: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        if "," in first_line:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = (row.get("symbol") or row.get("source_symbol") or "").strip()
                if symbol:
                    symbols.append(safe_symbol(symbol.upper()))
        else:
            for line in f:
                symbol = line.strip()
                if symbol and not symbol.startswith("#"):
                    symbols.append(safe_symbol(symbol.upper()))
    return symbols


def available_daily_symbols(daily_dir: Path) -> list[str]:
    suffix = "_1day_indicators.csv"
    if not daily_dir.exists():
        return []
    return sorted(path.name[: -len(suffix)] for path in daily_dir.glob(f"*{suffix}"))


def rows_by_date(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for row in rows:
        out.setdefault(row.datetime[:10], []).append(row)
    for date_text in out:
        out[date_text].sort(key=lambda item: item.datetime)
    return out


@dataclass(frozen=True)
class WaterlineSignalMetrics:
    prior_volume_avg: float
    volume_ratio: float
    trend_up_days: int
    trend_return: float
    signal_return: float
    ma_price: float
    ma_slope: float


def average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def is_signal_day(
    row: Row,
    prior_rows: list[Row],
    volume_lookback: int,
    volume_multiple: float,
    candle_k: float,
    trend_lookback: int,
    trend_min_up_days: int,
    trend_min_return: float,
    signal_return_lookback: int,
    ma_window: int,
    ma_slope_lookback: int,
) -> tuple[bool, WaterlineSignalMetrics]:
    empty = WaterlineSignalMetrics(0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0)
    min_prior = max(volume_lookback, trend_lookback - 1, signal_return_lookback + 1, ma_window + ma_slope_lookback - 1)
    if len(prior_rows) < min_prior:
        return False, empty
    prior_volume_rows = prior_rows[-volume_lookback:]
    prior_volume_avg = sum(item.volume for item in prior_volume_rows) / len(prior_volume_rows)
    volume_ratio = row.volume / prior_volume_avg if prior_volume_avg > 0 else 0.0
    trend_rows = prior_rows[-(trend_lookback - 1) :] + [row]
    trend_up_days = sum(1 for prev, curr in zip(trend_rows, trend_rows[1:]) if curr.close > prev.close)
    trend_return = row.close / trend_rows[0].close - 1 if trend_rows[0].close > 0 else 0.0
    recent_rows = prior_rows[-(signal_return_lookback + 1) :] + [row]
    daily_returns = [
        curr.close / prev.close - 1
        for prev, curr in zip(recent_rows, recent_rows[1:])
        if prev.close > 0
    ]
    signal_return = daily_returns[-1] if daily_returns else 0.0
    prior_returns = daily_returns[:-1]
    ma_rows = prior_rows[-(ma_window + ma_slope_lookback - 1) :] + [row]
    ma_price = average([item.close for item in ma_rows[-ma_window:]])
    prior_ma = average([item.close for item in ma_rows[-(ma_window + ma_slope_lookback) : -ma_slope_lookback]])
    ma_slope = ma_price / prior_ma - 1 if prior_ma > 0 else 0.0
    upper_shadow = max(row.high - row.close, 0.0)
    bullish_shape = row.close > row.open and (candle_k <= 0 or row.close - row.low > candle_k * upper_shadow)
    enough_volume = prior_volume_avg > 0 and volume_ratio >= volume_multiple
    trend_ok = trend_up_days >= trend_min_up_days and trend_return >= trend_min_return
    signal_return_ok = bool(prior_returns) and signal_return > max(prior_returns)
    ma_ok = ma_price > 0 and row.close >= ma_price and ma_slope > 0
    metrics = WaterlineSignalMetrics(prior_volume_avg, volume_ratio, trend_up_days, trend_return, signal_return, ma_price, ma_slope)
    return bullish_shape and enough_volume and trend_ok and signal_return_ok and ma_ok, metrics


def minute_above_ratio(rows: list[Row], waterline: float) -> tuple[int, int, float]:
    total = len(rows)
    above = sum(1 for row in rows if row.close > waterline)
    ratio = above / total if total else 0.0
    return total, above, ratio


def scan_symbol_entries(
    symbol: str,
    daily_rows: list[Row],
    minute_rows: list[Row],
    volume_lookback: int,
    volume_multiple: float,
    candle_k: float,
    trend_lookback: int,
    trend_min_up_days: int,
    trend_min_return: float,
    signal_return_lookback: int,
    ma_window: int,
    ma_slope_lookback: int,
    above_ratio_threshold: float,
    min_minutes: int,
    minute_timeframe: str,
) -> list[WaterlineEntry]:
    minute_by_date = rows_by_date(minute_rows)
    entries: list[WaterlineEntry] = []
    for index in range(volume_lookback, len(daily_rows) - 1):
        signal_row = daily_rows[index]
        trade_row = daily_rows[index + 1]
        prior_rows = daily_rows[:index]
        ok, metrics = is_signal_day(
            signal_row,
            prior_rows,
            volume_lookback,
            volume_multiple,
            candle_k,
            trend_lookback,
            trend_min_up_days,
            trend_min_return,
            signal_return_lookback,
            ma_window,
            ma_slope_lookback,
        )
        if not ok:
            continue
        trade_date = trade_row.datetime[:10]
        trade_minutes = minute_by_date.get(trade_date, [])
        total, above, ratio = minute_above_ratio(trade_minutes, signal_row.close)
        if total < min_minutes or ratio < above_ratio_threshold:
            continue
        entries.append(
            WaterlineEntry(
                symbol=symbol,
                signal_date=signal_row.datetime[:10],
                signal_time=signal_row.datetime,
                signal_open=signal_row.open,
                signal_high=signal_row.high,
                signal_low=signal_row.low,
                signal_close=signal_row.close,
                signal_volume=signal_row.volume,
                prior_volume_avg=metrics.prior_volume_avg,
                volume_ratio=metrics.volume_ratio,
                candle_k=candle_k,
                trend_lookback=trend_lookback,
                trend_up_days=metrics.trend_up_days,
                trend_return=metrics.trend_return,
                signal_return=metrics.signal_return,
                signal_return_lookback=signal_return_lookback,
                ma_window=ma_window,
                ma_price=metrics.ma_price,
                ma_slope_lookback=ma_slope_lookback,
                ma_slope=metrics.ma_slope,
                trade_date=trade_date,
                minute_timeframe=minute_timeframe,
                minute_total=total,
                minute_above=above,
                minute_above_ratio=ratio,
                entry_time=trade_row.datetime,
                entry_price=trade_row.close,
            )
        )
    return entries


def scan_symbol_candidates(
    symbol: str,
    daily_rows: list[Row],
    volume_lookback: int,
    volume_multiple: float,
    candle_k: float,
    trend_lookback: int,
    trend_min_up_days: int,
    trend_min_return: float,
    signal_return_lookback: int,
    ma_window: int,
    ma_slope_lookback: int,
) -> list[WaterlineCandidate]:
    candidates: list[WaterlineCandidate] = []
    for index in range(volume_lookback, len(daily_rows) - 1):
        signal_row = daily_rows[index]
        trade_row = daily_rows[index + 1]
        prior_rows = daily_rows[:index]
        ok, metrics = is_signal_day(
            signal_row,
            prior_rows,
            volume_lookback,
            volume_multiple,
            candle_k,
            trend_lookback,
            trend_min_up_days,
            trend_min_return,
            signal_return_lookback,
            ma_window,
            ma_slope_lookback,
        )
        if not ok:
            continue
        candidates.append(
            WaterlineCandidate(
                symbol=symbol,
                signal_date=signal_row.datetime[:10],
                signal_time=signal_row.datetime,
                signal_open=signal_row.open,
                signal_high=signal_row.high,
                signal_low=signal_row.low,
                signal_close=signal_row.close,
                signal_volume=signal_row.volume,
                prior_volume_avg=metrics.prior_volume_avg,
                volume_ratio=metrics.volume_ratio,
                candle_k=candle_k,
                trend_lookback=trend_lookback,
                trend_up_days=metrics.trend_up_days,
                trend_return=metrics.trend_return,
                signal_return=metrics.signal_return,
                signal_return_lookback=signal_return_lookback,
                ma_window=ma_window,
                ma_price=metrics.ma_price,
                ma_slope_lookback=ma_slope_lookback,
                ma_slope=metrics.ma_slope,
                trade_date=trade_row.datetime[:10],
                trade_time=trade_row.datetime,
                trade_close=trade_row.close,
            )
        )
    return candidates


def confirm_candidate_entry(
    candidate: WaterlineCandidate,
    trade_minutes: list[Row],
    above_ratio_threshold: float,
    min_minutes: int,
    minute_timeframe: str,
) -> WaterlineEntry | None:
    total, above, ratio = minute_above_ratio(trade_minutes, candidate.signal_close)
    if total < min_minutes or ratio < above_ratio_threshold:
        return None
    return WaterlineEntry(
        symbol=candidate.symbol,
        signal_date=candidate.signal_date,
        signal_time=candidate.signal_time,
        signal_open=candidate.signal_open,
        signal_high=candidate.signal_high,
        signal_low=candidate.signal_low,
        signal_close=candidate.signal_close,
        signal_volume=candidate.signal_volume,
        prior_volume_avg=candidate.prior_volume_avg,
        volume_ratio=candidate.volume_ratio,
        candle_k=candidate.candle_k,
        trend_lookback=candidate.trend_lookback,
        trend_up_days=candidate.trend_up_days,
        trend_return=candidate.trend_return,
        signal_return=candidate.signal_return,
        signal_return_lookback=candidate.signal_return_lookback,
        ma_window=candidate.ma_window,
        ma_price=candidate.ma_price,
        ma_slope_lookback=candidate.ma_slope_lookback,
        ma_slope=candidate.ma_slope,
        trade_date=candidate.trade_date,
        minute_timeframe=minute_timeframe,
        minute_total=total,
        minute_above=above,
        minute_above_ratio=ratio,
        entry_time=candidate.trade_time,
        entry_price=candidate.trade_close,
    )


def write_dataclass_rows(rows: list[WaterlineEntry] | list[WaterlineCandidate], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        fieldnames = list(rows[0].__dataclass_fields__.keys())
    else:
        fieldnames = list(WaterlineEntry.__dataclass_fields__.keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            row = asdict(item)
            for key, value in row.items():
                if isinstance(value, float):
                    row[key] = fmt(value)
            writer.writerow(row)


def write_entries(entries: list[WaterlineEntry], output_path: Path) -> None:
    write_dataclass_rows(entries, output_path)


def write_candidates(candidates: list[WaterlineCandidate], output_path: Path) -> None:
    if candidates:
        write_dataclass_rows(candidates, output_path)
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(WaterlineCandidate.__dataclass_fields__.keys()))
        writer.writeheader()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan waterline entry signals from daily and minute CSV data.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Safe local symbols to scan, e.g. AAPL BRK-B.")
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--minute-dir", type=Path, default=DATA_ROOT / "1min")
    parser.add_argument("--minute-timeframe", default="1min")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=1.2)
    parser.add_argument("--candle-k", type=float, default=0.0)
    parser.add_argument("--trend-lookback", type=int, default=5)
    parser.add_argument("--trend-min-up-days", type=int, default=4)
    parser.add_argument("--trend-min-return", type=float, default=0.03)
    parser.add_argument("--signal-return-lookback", type=int, default=4)
    parser.add_argument("--waterline-ma-window", type=int, default=20)
    parser.add_argument("--ma-slope-lookback", type=int, default=3)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--min-minutes", type=int, default=1)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT / "waterline_entries.csv")
    parser.add_argument("--candidates-output", type=Path, default=OUTPUT_ROOT / "waterline_candidates.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.symbols_file:
        symbols = read_symbols_file(args.symbols_file)
    elif args.symbols:
        symbols = [safe_symbol(symbol.upper()) for symbol in args.symbols]
    else:
        symbols = available_daily_symbols(args.daily_dir)

    candidates: list[WaterlineCandidate] = []
    entries: list[WaterlineEntry] = []
    for symbol in symbols:
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        minute_path = args.minute_dir / f"{symbol}_{args.minute_timeframe}_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        symbol_candidates = scan_symbol_candidates(
            symbol,
            daily_rows,
            args.volume_lookback,
            args.volume_multiple,
            args.candle_k,
            args.trend_lookback,
            args.trend_min_up_days,
            args.trend_min_return,
            args.signal_return_lookback,
            args.waterline_ma_window,
            args.ma_slope_lookback,
        )
        candidates.extend(symbol_candidates)
        if not minute_path.exists():
            continue
        minute_rows = load_rows(minute_path, min_date=args.start)
        entries.extend(
            scan_symbol_entries(
                symbol,
                daily_rows,
                minute_rows,
                args.volume_lookback,
                args.volume_multiple,
                args.candle_k,
                args.trend_lookback,
                args.trend_min_up_days,
                args.trend_min_return,
                args.signal_return_lookback,
                args.waterline_ma_window,
                args.ma_slope_lookback,
                args.above_ratio,
                args.min_minutes,
                args.minute_timeframe,
            )
        )

    candidates.sort(key=lambda item: (item.signal_date, item.symbol, item.trade_date))
    entries.sort(key=lambda item: (item.trade_date, item.symbol, item.signal_date))
    write_candidates(candidates, args.candidates_output)
    write_entries(entries, args.output)
    print(f"candidates={len(candidates)} candidates_output={args.candidates_output}")
    print(f"entries={len(entries)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
