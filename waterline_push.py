from __future__ import annotations

import argparse
import collections
from datetime import datetime
from pathlib import Path

from ad_structure_v05_core import load_rows
from constants import DATA_ROOT, FEISHU_WEBHOOKS
from intraday_tmp import load_tmp_minutes
from push_utils import (
    fmt_price,
    fmt_ratio,
    load_json,
    load_metadata,
    parse_date,
    prior_date_text,
    save_json,
    send_or_print,
    state_path,
    stock_label,
)
from waterline_signal import (
    available_daily_symbols,
    confirm_candidate_entry,
    read_symbols_file,
    scan_symbol_candidates,
)


POSITIONS_PATH = state_path("waterline_positions.json")


def load_positions() -> dict[str, dict]:
    data = load_json(POSITIONS_PATH, {"positions": {}})
    positions = data.get("positions")
    return positions if isinstance(positions, dict) else {}


def save_positions(positions: dict[str, dict]) -> None:
    save_json(POSITIONS_PATH, {"positions": positions})


def open_positions(positions: dict[str, dict]) -> dict[str, dict]:
    return {symbol: item for symbol, item in positions.items() if item.get("status") == "OPEN"}


def has_open_position(positions: dict[str, dict], symbol: str) -> bool:
    item = positions.get(symbol)
    return bool(item and item.get("status") == "OPEN")


def position_summary(position: dict | None) -> str:
    if not position:
        return "NONE"
    parts = [str(position.get("status") or "UNKNOWN")]
    for name in ("strategy", "signal_date", "trade_date", "planned_entry_date", "entry_date"):
        value = position.get(name)
        if value:
            parts.append(f"{name}={value}")
    reference_price = position.get("reference_price")
    if reference_price not in (None, ""):
        parts.append(f"reference={fmt_price(reference_price)}")
    return " ".join(parts)


def symbols_from_args(args: argparse.Namespace) -> list[str]:
    if args.symbols_file:
        return read_symbols_file(args.symbols_file)
    if args.symbols:
        from fetch_sp500_2026_and_mark import safe_symbol

        return [safe_symbol(symbol.upper()) for symbol in args.symbols]
    return available_daily_symbols(args.daily_dir)


def candidate_kwargs(args: argparse.Namespace) -> dict:
    return {
        "volume_lookback": args.volume_lookback,
        "volume_multiple": args.volume_multiple,
        "trend_lookback": args.trend_lookback,
        "trend_min_up_days": args.trend_min_up_days,
        "trend_min_return": args.trend_min_return,
        "ma_window": args.waterline_ma_window,
        "ma_slope_lookback": args.ma_slope_lookback,
    }


def collect_required_tmp_symbols(args: argparse.Namespace, date_text: str | None = None) -> list[str]:
    date_text = date_text or args.date
    symbols: list[str] = []
    for symbol, position in open_positions(load_positions()).items():
        entry_date = str(position.get("entry_date") or position.get("planned_entry_date") or "")
        if entry_date and parse_date(date_text) >= parse_date(entry_date):
            symbols.append(symbol)
    for symbol in symbols_from_args(args):
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        for candidate in scan_symbol_candidates(symbol, daily_rows, **candidate_kwargs(args)):
            if candidate.trade_date == date_text:
                symbols.append(symbol)
                break
    return sorted(set(symbols))


def format_signal(candidate, metadata: dict[str, dict[str, str]]) -> str:
    return "\n".join(
        [
            f"水上漂趋势信号日 | {stock_label(candidate.symbol, metadata)}",
            f"信号日: {candidate.signal_time}",
            f"水位线/reference: {fmt_price(candidate.signal_close)}",
            f"成交量倍数: {candidate.volume_ratio:.2f}",
            f"小升浪: {candidate.trend_lookback}日内上涨{candidate.trend_up_days}天，涨幅 {fmt_ratio(candidate.trend_return)}",
            f"信号日涨幅: {fmt_ratio(candidate.signal_return)}，高于前{candidate.trend_lookback - 1}天",
            f"MA{candidate.ma_window}: {fmt_price(candidate.ma_price)}，斜率 {fmt_ratio(candidate.ma_slope)}",
            f"交易日: {candidate.trade_date}",
        ]
    )


def format_trade(entry, metadata: dict[str, dict[str, str]], server_position: dict | None) -> str:
    message = "\n".join(
        [
            f"水上漂趋势交易日确认 | {stock_label(entry.symbol, metadata)}",
            f"信号日: {entry.signal_time}",
            f"交易日: {entry.trade_date}",
            f"水位线/reference: {fmt_price(entry.signal_close)}",
            f"站上比例: {fmt_ratio(entry.minute_above_ratio)} ({entry.minute_above}/{entry.minute_total})",
            f"小升浪: {entry.trend_lookback}日内上涨{entry.trend_up_days}天，涨幅 {fmt_ratio(entry.trend_return)}",
            f"信号日涨幅: {fmt_ratio(entry.signal_return)}，高于前{entry.trend_lookback - 1}天",
            f"MA{entry.ma_window}: {fmt_price(entry.ma_price)}，斜率 {fmt_ratio(entry.ma_slope)}",
            f"参考买入: {entry.entry_time} close {fmt_price(entry.entry_price)}",
        ]
    )
    return message + f"\nServer position state: {position_summary(server_position)}"


def format_sell(
    symbol: str,
    position: dict,
    date_text: str,
    ma_price: float,
    below: int,
    total: int,
    ratio: float,
    planned_exit_date: str,
    metadata: dict[str, dict[str, str]],
) -> str:
    return "\n".join(
        [
            f"水上漂 MA20 卖出信号 | {stock_label(symbol, metadata)}",
            f"触发日: {date_text}",
            f"MA{position.get('sell_ma_window', 20)}: {fmt_price(ma_price)}",
            f"跌破比例: {fmt_ratio(ratio)} ({below}/{total})",
            f"原始信号日: {position.get('signal_time') or '-'}",
            f"计划执行: {planned_exit_date or '下一交易日'} 开盘卖出",
        ]
    )


def prior_ma_by_date(rows, window: int) -> dict[str, float]:
    out: dict[str, float] = {}
    closes: list[float] = []
    for row in rows:
        date_text = row.datetime[:10]
        if len(closes) >= window:
            out[date_text] = sum(closes[-window:]) / window
        closes.append(row.close)
    return out


def next_daily_row_after(rows, date_text: str):
    for row in rows:
        if row.datetime[:10] > date_text:
            return row
    return None


def below_ratio(rows, price: float) -> tuple[int, int, float]:
    total = len(rows)
    below = sum(1 for row in rows if row.close < price)
    return total, below, below / total if total else 0.0


def maybe_fill_entry(position: dict, daily_rows, date_text: str) -> None:
    if position.get("entry_date") or not position.get("planned_entry_date"):
        return
    planned_entry_date = str(position.get("planned_entry_date"))
    if parse_date(date_text) < parse_date(planned_entry_date):
        return
    for row in daily_rows:
        if row.datetime[:10] == planned_entry_date:
            position["entry_date"] = planned_entry_date
            position["entry_time"] = row.datetime
            position["entry_price"] = row.open
            return


def collect_sell_messages(
    args: argparse.Namespace,
    positions: dict[str, dict],
    metadata: dict[str, dict[str, str]],
    stats: collections.Counter,
) -> list[str]:
    messages: list[str] = []
    open_items = open_positions(positions)
    stats["sell_open_positions"] = len(open_items)
    for symbol, position in list(open_items.items()):
        stats["sell_positions_checked"] += 1
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            stats["sell_missing_daily"] += 1
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        maybe_fill_entry(position, daily_rows, args.date)
        entry_date = str(position.get("entry_date") or position.get("planned_entry_date") or "")
        if not entry_date or parse_date(args.date) < parse_date(entry_date):
            stats["sell_not_ready"] += 1
            positions[symbol] = position
            continue
        ma_price = prior_ma_by_date(daily_rows, args.sell_ma_window).get(args.date)
        if ma_price is None:
            stats["sell_missing_ma"] += 1
            positions[symbol] = position
            continue
        minute_rows = [row for row in load_tmp_minutes(symbol, args.date) if row.datetime[:10] == args.date]
        total, below, ratio = below_ratio(minute_rows, ma_price)
        if total < args.min_minutes or ratio < args.sell_below_ratio:
            if total < args.min_minutes:
                stats["sell_insufficient_minutes"] += 1
            else:
                stats["sell_not_triggered"] += 1
            positions[symbol] = position
            continue
        next_row = next_daily_row_after(daily_rows, args.date)
        planned_exit_date = next_row.datetime[:10] if next_row else ""
        position.update(
            {
                "status": "CLOSED",
                "exit_signal_date": args.date,
                "exit_signal_time": f"{args.date} 16:00:00",
                "exit_rule": f"MA{args.sell_ma_window}",
                "exit_ma_price": ma_price,
                "exit_below_ratio": ratio,
                "planned_exit_date": planned_exit_date,
                "closed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        positions[symbol] = position
        messages.append(format_sell(symbol, position, args.date, ma_price, below, total, ratio, planned_exit_date, metadata))
        stats["sell_messages"] += 1
    return messages


def collect_messages(args: argparse.Namespace) -> tuple[dict[str, list[str]], collections.Counter]:
    metadata = load_metadata()
    positions = load_positions()
    stats: collections.Counter = collections.Counter()
    messages: dict[str, list[str]] = {"signal": [], "trade": [], "sell": []}
    messages["sell"] = collect_sell_messages(args, positions, metadata, stats)

    symbols = symbols_from_args(args)
    stats["symbols"] = len(symbols)
    for symbol in symbols:
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            stats["missing_daily"] += 1
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        for candidate in scan_symbol_candidates(symbol, daily_rows, **candidate_kwargs(args)):
            stats["candidates_total"] += 1
            if candidate.signal_date == args.date and args.include_signal_day:
                stats["signal_candidates_today"] += 1
                messages["signal"].append(format_signal(candidate, metadata))
                stats["signal_messages"] += 1
            if candidate.trade_date != args.date:
                continue
            stats["trade_candidates_today"] += 1
            trade_minutes = load_tmp_minutes(symbol, args.date)
            entry = confirm_candidate_entry(candidate, trade_minutes, args.above_ratio, args.min_minutes, "1min")
            if not entry:
                stats["trade_not_confirmed"] += 1
                continue
            server_position = positions.get(symbol)
            messages["trade"].append(format_trade(entry, metadata, server_position))
            stats["trade_messages"] += 1
            if not has_open_position(positions, symbol):
                positions[symbol] = {
                    "status": "OPEN",
                    "strategy": "waterline_trend",
                    "symbol": symbol,
                    "signal_date": entry.signal_date,
                    "signal_time": entry.signal_time,
                    "trade_date": entry.trade_date,
                    "reference_price": entry.signal_close,
                    "planned_entry_date": entry.trade_date,
                    "trend_lookback": entry.trend_lookback,
                    "trend_up_days": entry.trend_up_days,
                        "trend_return": entry.trend_return,
                        "signal_return": entry.signal_return,
                        "entry_confirm_price": entry.entry_price,
                    "sell_ma_window": args.sell_ma_window,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
            else:
                stats["trade_open_position_exists"] += 1
    if not args.dry_run:
        save_positions(positions)
    return messages, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push waterline trend signal, trade, and MA20 sell confirmations to Feishu.")
    parser.add_argument("--date", default=prior_date_text())
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=1.5)
    parser.add_argument("--trend-lookback", type=int, default=10)
    parser.add_argument("--trend-min-up-days", type=int, default=6)
    parser.add_argument("--trend-min-return", type=float, default=0.08)
    parser.add_argument("--waterline-ma-window", type=int, default=20)
    parser.add_argument("--ma-slope-lookback", type=int, default=3)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--sell-ma-window", type=int, default=20)
    parser.add_argument("--sell-below-ratio", type=float, default=0.5)
    parser.add_argument("--include-signal-day", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--signal-webhook-url", default=None)
    parser.add_argument("--trade-webhook-url", default=None)
    parser.add_argument("--sell-webhook-url", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    messages_by_type, stats = collect_messages(args)
    signal_title = f"[UpBottom waterline trend signal day] {args.date} | {len(messages_by_type['signal'])} messages"
    trade_title = f"[UpBottom waterline trend trade day] {args.date} | {len(messages_by_type['trade'])} messages"
    sell_title = f"[UpBottom waterline MA20 sell] {args.date} | {len(messages_by_type['sell'])} messages"
    signal_pushed = send_or_print(
        signal_title,
        messages_by_type["signal"],
        args.signal_webhook_url or FEISHU_WEBHOOKS.get("waterline_signal", ""),
        args.dry_run,
    )
    trade_pushed = send_or_print(
        trade_title,
        messages_by_type["trade"],
        args.trade_webhook_url or FEISHU_WEBHOOKS.get("waterline_trade", ""),
        args.dry_run,
    )
    sell_pushed = send_or_print(
        sell_title,
        messages_by_type["sell"],
        args.sell_webhook_url or FEISHU_WEBHOOKS.get("waterline_sell", ""),
        args.dry_run,
    )
    print(
        f"waterline_signal_messages={len(messages_by_type['signal'])} pushed={signal_pushed} "
        f"waterline_trade_messages={len(messages_by_type['trade'])} pushed={trade_pushed} "
        f"waterline_sell_messages={len(messages_by_type['sell'])} pushed={sell_pushed}",
        flush=True,
    )
    print("waterline_diagnostics=" + " ".join(f"{key}={stats[key]}" for key in sorted(stats)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
