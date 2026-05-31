"""Waterline strategy backtest.

This module consumes entries produced by waterline_signal.py. It manages the
holding lifecycle only:

- initial anchor is the signal-day close
- exit when a full trading day has at least 50% minute closes below anchor
- raise anchor after a confirmed 1h platform
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from ad_structure_v05_core import Row, load_rows
from fetch_sp500_2026_and_mark import DATA_ROOT, OUTPUT_ROOT, safe_symbol


@dataclass(frozen=True)
class WaterlineTrade:
    symbol: str
    signal_date: str
    trade_date: str
    entry_time: str
    entry_price: float
    initial_anchor: float
    final_anchor: float
    anchor_updates: int
    exit_date: str
    exit_time: str
    exit_price: float
    exit_anchor: float
    exit_below_ratio: float
    exit_rule: str
    exit_reference_price: float
    return_pct: float
    holding_days: int
    status: str


@dataclass
class AnchorState:
    anchor: float
    updates: int = 0
    state: str = "SEEK_PLATFORM"
    define_rows: list[Row] | None = None
    box_high: float | None = None
    box_low: float | None = None
    confirm_count: int = 0


def fmt(value: float) -> str:
    return f"{value:.6f}"


def read_entries(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def rows_by_date(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for row in rows:
        out.setdefault(row.datetime[:10], []).append(row)
    for date_text in out:
        out[date_text].sort(key=lambda item: item.datetime)
    return out


def daily_close_by_date(rows: list[Row]) -> dict[str, Row]:
    return {row.datetime[:10]: row for row in rows}


def below_ratio(rows: list[Row], anchor: float) -> tuple[int, int, float]:
    total = len(rows)
    below = sum(1 for row in rows if row.close < anchor)
    ratio = below / total if total else 0.0
    return total, below, ratio


def prior_ma_by_date(rows: list[Row], window: int) -> dict[str, float]:
    out: dict[str, float] = {}
    prior_closes: list[float] = []
    for row in rows:
        date_text = row.datetime[:10]
        if len(prior_closes) >= window:
            out[date_text] = sum(prior_closes[-window:]) / window
        prior_closes.append(row.close)
    return out


def update_anchor_with_hourly_bar(
    anchor_state: AnchorState,
    row: Row,
    advance_pct: float,
    define_bars: int,
    confirm_bars: int,
) -> None:
    if anchor_state.state == "SEEK_PLATFORM":
        if anchor_state.define_rows is None:
            anchor_state.define_rows = []
        anchor_state.define_rows.append(row)
        if len(anchor_state.define_rows) > define_bars:
            anchor_state.define_rows = anchor_state.define_rows[-define_bars:]
        if len(anchor_state.define_rows) < define_bars:
            return
        box_high = max(item.close for item in anchor_state.define_rows)
        box_low = min(item.close for item in anchor_state.define_rows)
        if box_high >= anchor_state.anchor * (1 + advance_pct):
            anchor_state.state = "CONFIRM_PLATFORM"
            anchor_state.box_high = box_high
            anchor_state.box_low = box_low
            anchor_state.confirm_count = 0
            anchor_state.define_rows = []
        return

    if anchor_state.state != "CONFIRM_PLATFORM" or anchor_state.box_high is None or anchor_state.box_low is None:
        return

    if row.close < anchor_state.box_low:
        anchor_state.state = "SEEK_PLATFORM"
        anchor_state.define_rows = [row]
        anchor_state.box_high = None
        anchor_state.box_low = None
        anchor_state.confirm_count = 0
        return

    if row.close > anchor_state.box_high:
        anchor_state.state = "SEEK_PLATFORM"
        anchor_state.define_rows = [row]
        anchor_state.box_high = None
        anchor_state.box_low = None
        anchor_state.confirm_count = 0
        return

    anchor_state.confirm_count += 1
    if anchor_state.confirm_count >= confirm_bars:
        if anchor_state.box_low > anchor_state.anchor:
            anchor_state.anchor = anchor_state.box_low
            anchor_state.updates += 1
        anchor_state.state = "SEEK_PLATFORM"
        anchor_state.define_rows = []
        anchor_state.box_high = None
        anchor_state.box_low = None
        anchor_state.confirm_count = 0


def update_anchor_for_day(
    anchor_state: AnchorState,
    hourly_rows: list[Row],
    advance_pct: float,
    define_bars: int,
    confirm_bars: int,
) -> None:
    for row in hourly_rows:
        update_anchor_with_hourly_bar(anchor_state, row, advance_pct, define_bars, confirm_bars)


def backtest_entry(
    entry: dict[str, str],
    daily_rows: list[Row],
    minute_rows: list[Row],
    hourly_rows: list[Row],
    exit_below_ratio: float,
    advance_pct: float,
    define_bars: int,
    confirm_bars: int,
    exit_rule: str = "anchor",
    ma_window: int = 5,
) -> WaterlineTrade:
    symbol = entry["symbol"]
    signal_date = entry["signal_date"]
    trade_date = entry["trade_date"]
    entry_time = entry["entry_time"]
    entry_price = float(entry["entry_price"])
    initial_anchor = float(entry["signal_close"])
    anchor_state = AnchorState(anchor=initial_anchor)

    daily_by_date = daily_close_by_date(daily_rows)
    minute_by_date = rows_by_date(minute_rows)
    hourly_by_date = rows_by_date(hourly_rows)
    prior_ma5_by_date = prior_ma_by_date(daily_rows, ma_window)
    dates = sorted(date for date in daily_by_date if date > trade_date)
    for holding_index, date_text in enumerate(dates, start=1):
        update_anchor_for_day(anchor_state, hourly_by_date.get(date_text, []), advance_pct, define_bars, confirm_bars)
        day_minutes = minute_by_date.get(date_text, [])
        if not day_minutes:
            continue
        exit_reason = ""
        exit_reference_price = anchor_state.anchor
        ratio = 0.0
        if exit_rule == "ma5-or-signal":
            ma_price = prior_ma5_by_date.get(date_text)
            ma_ratio = 0.0
            if ma_price is not None:
                _, _, ma_ratio = below_ratio(day_minutes, ma_price)
            _, _, signal_ratio = below_ratio(day_minutes, initial_anchor)
            if ma_ratio >= exit_below_ratio:
                exit_reason = f"MA{ma_window}"
                exit_reference_price = ma_price if ma_price is not None else 0.0
                ratio = ma_ratio
            elif signal_ratio >= exit_below_ratio:
                exit_reason = "SIGNAL_CLOSE"
                exit_reference_price = initial_anchor
                ratio = signal_ratio
        else:
            _, _, ratio = below_ratio(day_minutes, anchor_state.anchor)
            if ratio >= exit_below_ratio:
                exit_reason = "ANCHOR"

        if exit_reason:
            exit_row = daily_by_date[date_text]
            return WaterlineTrade(
                symbol=symbol,
                signal_date=signal_date,
                trade_date=trade_date,
                entry_time=entry_time,
                entry_price=entry_price,
                initial_anchor=initial_anchor,
                final_anchor=anchor_state.anchor,
                anchor_updates=anchor_state.updates,
                exit_date=date_text,
                exit_time=exit_row.datetime,
                exit_price=exit_row.close,
                exit_anchor=anchor_state.anchor,
                exit_below_ratio=ratio,
                exit_rule=exit_reason,
                exit_reference_price=exit_reference_price,
                return_pct=(exit_row.close / entry_price - 1) * 100,
                holding_days=holding_index,
                status="EXITED",
            )

    last_date = dates[-1] if dates else trade_date
    last_row = daily_by_date.get(last_date)
    last_price = last_row.close if last_row else entry_price
    return WaterlineTrade(
        symbol=symbol,
        signal_date=signal_date,
        trade_date=trade_date,
        entry_time=entry_time,
        entry_price=entry_price,
        initial_anchor=initial_anchor,
        final_anchor=anchor_state.anchor,
        anchor_updates=anchor_state.updates,
        exit_date="",
        exit_time="",
        exit_price=0.0,
        exit_anchor=anchor_state.anchor,
        exit_below_ratio=0.0,
        exit_rule="",
        exit_reference_price=0.0,
        return_pct=(last_price / entry_price - 1) * 100,
        holding_days=len(dates),
        status="OPEN",
    )


def write_trades(trades: list[WaterlineTrade], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(WaterlineTrade.__dataclass_fields__.keys())
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            row = asdict(trade)
            for key, value in row.items():
                if isinstance(value, float):
                    row[key] = fmt(value)
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest waterline entries with anchor-based exits.")
    parser.add_argument("--entries", type=Path, default=OUTPUT_ROOT / "waterline_entries.csv")
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--minute-dir", type=Path, default=DATA_ROOT / "1min")
    parser.add_argument("--hourly-dir", type=Path, default=DATA_ROOT / "1h")
    parser.add_argument("--minute-timeframe", default="1min")
    parser.add_argument("--hourly-timeframe", default="1h")
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--exit-rule", choices=["anchor", "ma5-or-signal"], default="anchor")
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--advance-pct", type=float, default=0.15)
    parser.add_argument("--define-bars", type=int, default=15)
    parser.add_argument("--confirm-bars", type=int, default=30)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT / "waterline_trades.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    entries = read_entries(args.entries)
    trades: list[WaterlineTrade] = []
    for entry in entries:
        symbol = safe_symbol(entry["symbol"].upper())
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        minute_path = args.minute_dir / f"{symbol}_{args.minute_timeframe}_indicators.csv"
        hourly_path = args.hourly_dir / f"{symbol}_{args.hourly_timeframe}_indicators.csv"
        if not daily_path.exists() or not minute_path.exists() or not hourly_path.exists():
            continue
        daily_rows = load_rows(daily_path)
        minute_rows = load_rows(minute_path, min_date=entry["trade_date"])
        hourly_rows = load_rows(hourly_path, min_date=entry["trade_date"])
        trades.append(
            backtest_entry(
                entry,
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
        )

    trades.sort(key=lambda item: (item.trade_date, item.symbol, item.signal_date))
    write_trades(trades, args.output)
    print(f"trades={len(trades)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
