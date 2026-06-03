from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from bottom_common import bottom_history_key, is_valid_bm_break, load_ad_signals
from constants import FEISHU_WEBHOOKS, OUTPUT_ROOT
from push_utils import (
    fmt_price,
    load_metadata,
    load_sent_cache,
    mark_sent,
    save_sent_cache,
    send_or_print,
    state_path,
    stock_label,
)


def format_history_message(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    return "\n".join(
        [
            f"底背离历史 BM 突破 | {stock_label(symbol, metadata)} | {row.get('timeframe', '-')}",
            f"BM: {row.get('BM_time') or '-'} @ {fmt_price(row.get('BM_price'))}",
            f"BM 突破: {row.get('BM_break_time') or '-'} @ {fmt_price(row.get('BM_break_price'))}",
            f"状态: {row.get('structure_status') or '-'}",
            f"GA/GB: {row.get('golden_A_time') or '-'} -> {row.get('golden_B_time') or '-'}",
        ]
    )


def collect_messages(
    signals_path: Path,
    timeframes: set[str],
    force: bool,
    sent: dict[str, dict],
) -> tuple[list[str], list[str]]:
    metadata = load_metadata()
    items: list[tuple[str, str, str]] = []
    for row in load_ad_signals(signals_path):
        if row.get("timeframe") not in timeframes:
            continue
        if not is_valid_bm_break(row):
            continue
        key = bottom_history_key(row)
        if not force and key in sent:
            continue
        items.append((row.get("BM_break_time", ""), key, format_history_message(row, metadata)))
    items.sort(key=lambda item: item[0])
    return [item[2] for item in items], [item[1] for item in items]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push historical bottom-divergence BM-break signals to Feishu.")
    parser.add_argument("--signals", type=Path, default=OUTPUT_ROOT / "ad_signals.csv")
    parser.add_argument("--cache", type=Path, default=state_path("bottom_history_push_cache.json"))
    parser.add_argument("--timeframes", nargs="+", default=["1day"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--webhook-url", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sent = load_sent_cache(args.cache)
    messages, keys = collect_messages(args.signals, set(args.timeframes), args.force, sent)
    title = f"【UpBottom 底背离历史 BM 突破】{len(messages)} 条 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    pushed = send_or_print(title, messages, args.webhook_url or FEISHU_WEBHOOKS.get("bottom_history", ""), args.dry_run)
    if pushed and not args.dry_run:
        mark_sent(sent, keys)
        save_sent_cache(args.cache, sent)
    print(f"bottom_history_messages={len(messages)} pushed={pushed} cache={args.cache}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
