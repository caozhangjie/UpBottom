from __future__ import annotations

import argparse
from pathlib import Path

from ad_structure_v05_core import load_rows
from constants import DATA_ROOT, FEISHU_WEBHOOKS
from intraday_tmp import load_tmp_minutes
from push_utils import (
    fmt_price,
    fmt_ratio,
    load_metadata,
    load_sent_cache,
    mark_sent,
    save_sent_cache,
    send_or_print,
    state_path,
    stock_label,
    today_text,
)
from waterline_signal import (
    available_daily_symbols,
    confirm_candidate_entry,
    read_symbols_file,
    scan_symbol_candidates,
)


def signal_key(symbol: str, signal_date: str, trade_date: str) -> str:
    return f"waterline_signal|{symbol}|{signal_date}|{trade_date}"


def trade_key(symbol: str, signal_date: str, trade_date: str) -> str:
    return f"waterline_trade|{symbol}|{signal_date}|{trade_date}"


def symbols_from_args(args: argparse.Namespace) -> list[str]:
    if args.symbols_file:
        return read_symbols_file(args.symbols_file)
    if args.symbols:
        from fetch_sp500_2026_and_mark import safe_symbol

        return [safe_symbol(symbol.upper()) for symbol in args.symbols]
    return available_daily_symbols(args.daily_dir)


def collect_required_tmp_symbols(args: argparse.Namespace, date_text: str | None = None) -> list[str]:
    date_text = date_text or args.date
    symbols: list[str] = []
    for symbol in symbols_from_args(args):
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        for candidate in scan_symbol_candidates(symbol, daily_rows, args.volume_lookback, args.volume_multiple, args.candle_k):
            if candidate.trade_date == date_text:
                symbols.append(symbol)
                break
    return sorted(set(symbols))


def format_signal(candidate, metadata: dict[str, dict[str, str]]) -> str:
    return "\n".join(
        [
            f"水上漂信号日 | {stock_label(candidate.symbol, metadata)}",
            f"信号日: {candidate.signal_time}",
            f"水位线/reference: {fmt_price(candidate.signal_close)}",
            f"成交量倍数: {candidate.volume_ratio:.2f}",
            f"交易日: {candidate.trade_date}",
        ]
    )


def format_trade(entry, metadata: dict[str, dict[str, str]]) -> str:
    return "\n".join(
        [
            f"水上漂交易日确认 | {stock_label(entry.symbol, metadata)}",
            f"信号日: {entry.signal_time}",
            f"交易日: {entry.trade_date}",
            f"水位线/reference: {fmt_price(entry.signal_close)}",
            f"站上比例: {fmt_ratio(entry.minute_above_ratio)} ({entry.minute_above}/{entry.minute_total})",
            f"参考买入: {entry.entry_time} close {fmt_price(entry.entry_price)}",
        ]
    )


def collect_messages(
    args: argparse.Namespace,
    sent: dict[str, dict],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    metadata = load_metadata()
    messages: dict[str, list[str]] = {"signal": [], "trade": []}
    keys: dict[str, list[str]] = {"signal": [], "trade": []}
    for symbol in symbols_from_args(args):
        daily_path = args.daily_dir / f"{symbol}_1day_indicators.csv"
        if not daily_path.exists():
            continue
        daily_rows = load_rows(daily_path, min_date=args.start)
        for candidate in scan_symbol_candidates(symbol, daily_rows, args.volume_lookback, args.volume_multiple, args.candle_k):
            if candidate.signal_date == args.date and args.include_signal_day:
                key = signal_key(symbol, candidate.signal_date, candidate.trade_date)
                if args.force or key not in sent:
                    messages["signal"].append(format_signal(candidate, metadata))
                    keys["signal"].append(key)
            if candidate.trade_date != args.date:
                continue
            trade_minutes = load_tmp_minutes(symbol, args.date)
            entry = confirm_candidate_entry(candidate, trade_minutes, args.above_ratio, args.min_minutes, "1min")
            if not entry:
                continue
            key = trade_key(symbol, entry.signal_date, entry.trade_date)
            if args.force or key not in sent:
                messages["trade"].append(format_trade(entry, metadata))
                keys["trade"].append(key)
    return messages, keys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push waterline signal-day and trade-day confirmations to Feishu.")
    parser.add_argument("--date", default=today_text())
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--include-signal-day", action="store_true", default=True)
    parser.add_argument("--cache", type=Path, default=state_path("waterline_push_cache.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--signal-webhook-url", default=None)
    parser.add_argument("--trade-webhook-url", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sent = load_sent_cache(args.cache)
    messages_by_type, keys_by_type = collect_messages(args, sent)
    signal_title = f"[UpBottom waterline signal day] {args.date} | {len(messages_by_type['signal'])} messages"
    trade_title = f"[UpBottom waterline trade day] {args.date} | {len(messages_by_type['trade'])} messages"
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
    if not args.dry_run:
        if signal_pushed:
            mark_sent(sent, keys_by_type["signal"])
        if trade_pushed:
            mark_sent(sent, keys_by_type["trade"])
        save_sent_cache(args.cache, sent)
    print(
        f"waterline_signal_messages={len(messages_by_type['signal'])} pushed={signal_pushed} "
        f"waterline_trade_messages={len(messages_by_type['trade'])} pushed={trade_pushed} cache={args.cache}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
