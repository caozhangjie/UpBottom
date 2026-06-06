from __future__ import annotations

import argparse
import collections
from datetime import datetime
from pathlib import Path

from bottom_common import (
    can_check_exit,
    c_confirm_row,
    c_confirm_time,
    check_exit_signal,
    first_c_point,
    is_trade_buy_candidate,
    load_ad_signals,
    load_daily_rows,
    load_positions,
    next_daily_row_after,
    open_positions,
    save_positions,
)
from constants import FEISHU_WEBHOOKS, OUTPUT_ROOT
from intraday_tmp import load_tmp_minutes
from push_utils import (
    fmt_price,
    fmt_ratio,
    load_metadata,
    parse_date,
    prior_date_text,
    send_or_print,
    stock_label,
)


def collect_required_tmp_symbols(date_text: str) -> list[str]:
    symbols: list[str] = []
    for symbol, position in open_positions(load_positions()).items():
        signal_date = str(position.get("signal_date") or "")
        entry_date = str(position.get("entry_date") or position.get("planned_entry_date") or "")
        if entry_date and parse_date(date_text) >= parse_date(entry_date):
            symbols.append(symbol)
        elif signal_date and parse_date(date_text) > parse_date(signal_date):
            symbols.append(symbol)
    return sorted(set(symbols))


def maybe_fill_entry(position: dict, daily_rows, date_text: str) -> None:
    if position.get("entry_date"):
        return
    signal_date = str(position.get("signal_date") or "")
    if not signal_date:
        return
    next_row = next_daily_row_after(daily_rows, signal_date)
    if next_row is None or next_row.datetime[:10] > date_text:
        return
    position["entry_date"] = next_row.datetime[:10]
    position["entry_time"] = next_row.datetime
    position["entry_price"] = next_row.open
    position["planned_entry_date"] = next_row.datetime[:10]


def position_summary(position: dict | None) -> str:
    if not position:
        return "NONE"
    parts = [str(position.get("status") or "UNKNOWN")]
    for name in ("strategy", "signal_date", "planned_entry_date", "entry_date"):
        value = position.get(name)
        if value:
            parts.append(f"{name}={value}")
    reference_price = position.get("reference_price")
    if reference_price not in (None, ""):
        parts.append(f"reference={fmt_price(reference_price)}")
    return " ".join(parts)


def format_buy(
    row: dict[str, str],
    metadata: dict[str, dict[str, str]],
    planned_entry_date: str,
    server_position: dict | None,
) -> str:
    symbol = row.get("symbol", "")
    c_point = first_c_point(row) or {}
    return "\n".join(
        [
            f"底背离 C 确认买入信号 | {stock_label(symbol, metadata)}",
            f"信号日: {c_confirm_time(row) or '-'}",
            f"C 点: {c_point.get('time') or '-'} @ {fmt_price(c_point.get('price'))}",
            f"C 确认收盘价/reference: {fmt_price(row.get('_c_confirm_close'))}",
            f"BM: {row.get('BM_time') or '-'} @ {fmt_price(row.get('BM_price'))}",
            f"Server position state: {position_summary(server_position)}",
            f"BM 突破: {row.get('BM_break_time') or '-'} @ {fmt_price(row.get('BM_break_price'))}",
            f"计划执行: {planned_entry_date or '下一交易日'} 开盘买入",
        ]
    )


def format_sell(
    symbol: str,
    position: dict,
    check,
    date_text: str,
    metadata: dict[str, dict[str, str]],
    planned_exit_date: str,
) -> str:
    return "\n".join(
        [
            f"底背离 C 确认卖出信号 | {stock_label(symbol, metadata)}",
            f"触发日: {date_text}",
            f"触发规则: {check.rule}，跌破比例 {fmt_ratio(check.below_ratio)}",
            f"MA{position.get('ma_window', 5)}: {fmt_price(check.ma_price) if check.ma_price is not None else '-'}，MA跌破比例 {fmt_ratio(check.ma_below_ratio)}",
            f"reference_price: {fmt_price(position.get('reference_price'))}，reference跌破比例 {fmt_ratio(check.signal_below_ratio)}",
            f"分钟数: {check.minute_total}",
            f"计划执行: {planned_exit_date or '下一交易日'} 开盘卖出",
            f"原始买入信号: {position.get('signal_time') or '-'} @ {fmt_price(position.get('reference_price'))}",
        ]
    )


def process_trade_signals(
    date_text: str,
    signals_path: Path,
    exit_below_ratio: float,
    ma_window: int,
    min_minutes: int,
) -> tuple[dict[str, list[str]], dict[str, dict], collections.Counter]:
    metadata = load_metadata()
    positions = load_positions()
    stats: collections.Counter = collections.Counter()
    messages: dict[str, list[str]] = {"buy": [], "sell": []}
    open_items = open_positions(positions)
    stats["open_positions"] = len(open_items)

    for symbol, position in list(open_items.items()):
        stats["sell_positions_checked"] += 1
        daily_rows = load_daily_rows(symbol)
        maybe_fill_entry(position, daily_rows, date_text)
        if not can_check_exit(position, date_text):
            stats["sell_not_ready"] += 1
            positions[symbol] = position
            continue
        minute_rows = load_tmp_minutes(symbol, date_text)
        check = check_exit_signal(
            daily_rows,
            minute_rows,
            date_text,
            float(position.get("reference_price") or 0),
            exit_below_ratio,
            ma_window,
            min_minutes,
        )
        if not check.triggered:
            if check.minute_total < min_minutes:
                stats["sell_insufficient_minutes"] += 1
            else:
                stats["sell_not_triggered"] += 1
            positions[symbol] = position
            continue
        next_row = next_daily_row_after(daily_rows, date_text)
        planned_exit_date = next_row.datetime[:10] if next_row else ""
        position.update(
            {
                "status": "CLOSED",
                "exit_signal_date": date_text,
                "exit_signal_time": f"{date_text} 16:00:00",
                "exit_rule": check.rule,
                "exit_reference_price": check.reference_price,
                "exit_below_ratio": check.below_ratio,
                "planned_exit_date": planned_exit_date,
                "closed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        positions[symbol] = position
        messages["sell"].append(format_sell(symbol, position, check, date_text, metadata, planned_exit_date))
        stats["sell_messages"] += 1

    buy_rows = [row for row in load_ad_signals(signals_path) if is_trade_buy_candidate(row, date_text)]
    stats["buy_candidates"] = len(buy_rows)
    buy_rows.sort(key=lambda row: (c_confirm_time(row), row.get("symbol", "")))
    for row in buy_rows:
        symbol = row.get("symbol", "")
        if not symbol:
            stats["buy_missing_symbol"] += 1
            continue
        server_position = positions.get(symbol)
        if server_position and server_position.get("status") == "OPEN":
            stats["buy_existing_open_position"] += 1
        daily_rows = load_daily_rows(symbol)
        confirm_row = c_confirm_row(row, daily_rows)
        if confirm_row is None:
            stats["buy_missing_confirm_row"] += 1
            continue
        next_row = next_daily_row_after(daily_rows, date_text)
        planned_entry_date = next_row.datetime[:10] if next_row else ""
        reference_price = confirm_row.close
        row["_c_confirm_close"] = str(reference_price)
        c_point = first_c_point(row) or {}
        if not (server_position and server_position.get("status") == "OPEN"):
            positions[symbol] = {
                "status": "OPEN",
                "strategy": "bottom_c_confirm",
                "symbol": symbol,
                "signal_date": date_text,
                "signal_time": c_confirm_time(row),
                "reference_price": reference_price,
                "c_time": str(c_point.get("time") or ""),
                "c_price": float(c_point.get("price") or 0),
                "c_confirm_price": reference_price,
                "bm_price": float(row.get("BM_price") or 0),
                "bm_break_price": float(row.get("BM_break_price") or 0),
                "planned_entry_date": planned_entry_date,
                "entry_date": "",
                "entry_time": "",
                "entry_price": None,
                "ma_window": ma_window,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        messages["buy"].append(format_buy(row, metadata, planned_entry_date, server_position))
        stats["buy_messages"] += 1

    return messages, positions, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily bottom-divergence C-confirm buy/sell signals to Feishu.")
    parser.add_argument("--date", default=prior_date_text())
    parser.add_argument("--signals", type=Path, default=OUTPUT_ROOT / "ad_signals.csv")
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--buy-webhook-url", default=None)
    parser.add_argument("--sell-webhook-url", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    messages_by_type, positions, stats = process_trade_signals(
        args.date,
        args.signals,
        args.exit_below_ratio,
        args.ma_window,
        args.min_minutes,
    )
    buy_title = f"[UpBottom bottom C-confirm buy signals] {args.date} | {len(messages_by_type['buy'])} messages"
    sell_title = f"[UpBottom bottom C-confirm sell signals] {args.date} | {len(messages_by_type['sell'])} messages"
    buy_pushed = send_or_print(
        buy_title,
        messages_by_type["buy"],
        args.buy_webhook_url or FEISHU_WEBHOOKS.get("bottom_buy", ""),
        args.dry_run,
    )
    sell_pushed = send_or_print(
        sell_title,
        messages_by_type["sell"],
        args.sell_webhook_url or FEISHU_WEBHOOKS.get("bottom_sell", ""),
        args.dry_run,
    )
    if not args.dry_run:
        save_positions(positions)
    print(
        f"bottom_buy_messages={len(messages_by_type['buy'])} pushed={buy_pushed} "
        f"bottom_sell_messages={len(messages_by_type['sell'])} pushed={sell_pushed}",
        flush=True,
    )
    print("bottom_diagnostics=" + " ".join(f"{key}={stats[key]}" for key in sorted(stats)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
