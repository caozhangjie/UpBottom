"""Push bottom-divergence D-trigger confirmation alerts.

This pusher is separate from bottom_divergence_signal_push.py. It only sends
daily close-confirmed second-breakout (`D_TRIGGERED`) alerts, intended for a
post-close run around 30 minutes after the US market close.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bottom_divergence_signal_push import (
    CREDENTIALS_DISCORD_WEBHOOK_URL,
    CREDENTIALS_FEISHU_WEBHOOK_URL,
    METADATA_PATH,
    OUTPUT_ROOT,
    SIGNALS_PATH,
    STOCK_METADATA_PATH,
    default_metadata_path,
    load_cache,
    load_csv,
    load_metadata,
    post_discord,
    post_feishu,
    save_cache,
)


EASTERN = ZoneInfo("America/New_York")
CACHE_PATH = OUTPUT_ROOT / "bottom_divergence_d_push_cache.json"


def d_alert_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            "bottom-d-trigger",
            row.get("symbol", ""),
            row.get("timeframe", ""),
            row.get("golden_A_time", ""),
            row.get("golden_B_time", ""),
            row.get("D_time", ""),
        ]
    )


def delivery_key(target: str, row: dict[str, str]) -> str:
    return f"{target}|{d_alert_key(row)}"


def metadata_text(symbol: str, metadata: dict[str, dict[str, str]]) -> tuple[str, str, str]:
    info = metadata.get(symbol, {})
    english_name = info.get("english_name") or "未获取"
    sector = info.get("sector") or "未获取"
    sub_industry = info.get("sub_industry") or "未获取"
    return english_name, sector, sub_industry


def first_c_text(row: dict[str, str]) -> str:
    try:
        c_items = json.loads(row.get("C_sequence") or "[]")
    except json.JSONDecodeError:
        c_items = []
    if not c_items:
        return "-"
    c = c_items[0]
    return f"{c.get('time', '-')} @ {c.get('price', '-')}"


def target_date_text(value: str, rows: list[dict[str, str]]) -> str | None:
    if value == "all":
        return None
    if value == "today":
        return datetime.now(EASTERN).date().isoformat()
    if value == "latest":
        dates = sorted({row.get("D_time", "")[:10] for row in rows if row.get("D_time")})
        return dates[-1] if dates else datetime.now(EASTERN).date().isoformat()
    return value


def is_d_candidate(row: dict[str, str], timeframe: str, date_text: str | None) -> bool:
    if timeframe != "all" and row.get("timeframe") != timeframe:
        return False
    if row.get("structure_status") != "D_TRIGGERED":
        return False
    if not row.get("D_time"):
        return False
    if date_text is not None and row.get("D_time", "")[:10] != date_text:
        return False
    return True


def format_alert(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    english_name, sector, sub_industry = metadata_text(symbol, metadata)
    return "\n".join(
        [
            f"**{symbol} | {row.get('timeframe', '')} | 底背离 D 二次突破确认**",
            f"英文名：{english_name}",
            f"行业：{sector} / {sub_industry}",
            f"第一金叉 GA：{row.get('golden_A_time', '-')} @ {row.get('golden_A_price', '-')}",
            f"BM：{row.get('BM_time', '-')} @ {row.get('BM_price', '-')}",
            f"B：{row.get('B_time', '-')} @ {row.get('B_price', '-')}",
            f"第二金叉 GB：{row.get('golden_B_time', '-')} @ {row.get('golden_B_price', '-')}",
            f"突破BM：{row.get('BM_break_time', '-')} @ {row.get('BM_break_price', '-')}",
            f"CM：{row.get('CM_time') or '-'} @ {row.get('CM_price') or '-'}",
            f"C：{first_c_text(row)}",
            f"D 二次突破：{row.get('D_time', '-')} @ {row.get('D_price', '-')}",
            f"状态：已经二次突破",
            f"图表：{row.get('chart_file') or '-'}",
        ]
    )


def chunk_alerts(
    rows: list[dict[str, str]],
    metadata: dict[str, dict[str, str]],
    header: str,
    limit: int,
) -> list[tuple[str, list[dict[str, str]]]]:
    chunks: list[tuple[str, list[dict[str, str]]]] = []
    current = ""
    current_rows: list[dict[str, str]] = []
    for row in rows:
        item = format_alert(row, metadata).strip()
        if not item:
            continue
        separator = "\n\n---\n\n"
        next_length = len(header) + 2 + len(current) + len(separator) + len(item)
        if current and next_length > limit:
            chunks.append((f"{header}\n\n{current}", current_rows))
            current = item
            current_rows = [row]
        elif current:
            current += separator + item
            current_rows.append(row)
        else:
            current = item
            current_rows = [row]
    if current:
        chunks.append((f"{header}\n\n{current}", current_rows))
    return chunks


def mark_sent(target: str, sent: dict, rows: list[dict[str, str]], chunk_index: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        sent[delivery_key(target, row)] = {
            "sent_at": now,
            "target": target,
            "chunk_index": chunk_index,
            "symbol": row.get("symbol", ""),
            "timeframe": row.get("timeframe", ""),
            "D_time": row.get("D_time", ""),
            "structure_status": row.get("structure_status", ""),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily bottom-divergence D-trigger confirmation alerts.")
    parser.add_argument("--timeframe", choices=["1day", "all"], default="1day")
    parser.add_argument("--date", default="today", help="today, latest, all, or YYYY-MM-DD. Default uses America/New_York today.")
    parser.add_argument("--signals", type=Path, default=SIGNALS_PATH)
    parser.add_argument("--metadata", type=Path, default=default_metadata_path())
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--feishu-webhook-url", default=None)
    parser.add_argument("--target", choices=["discord", "feishu", "both"], default="discord")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chunk-limit", type=int, default=1500)
    parser.add_argument("--post-timeout", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--chunk-delay", type=float, default=2.0)
    parser.add_argument("--stop-on-error", action="store_true")
    return parser.parse_args()


def send_chunks(
    target: str,
    webhook_url: str,
    chunks: list[tuple[str, list[dict[str, str]]]],
    args: argparse.Namespace,
    cache: dict,
    sent: dict,
) -> tuple[int, list[tuple[str, int, str, str]]]:
    pushed = 0
    failed_chunks: list[tuple[str, int, str, str]] = []
    for index, (chunk, chunk_rows) in enumerate(chunks, start=1):
        symbols = ",".join(row.get("symbol", "") for row in chunk_rows[:12])
        if len(chunk_rows) > 12:
            symbols += ",..."
        print(f"{target}_sending chunk={index}/{len(chunks)} alerts={len(chunk_rows)} symbols={symbols}", flush=True)
        try:
            if target == "discord":
                status = post_discord(webhook_url, chunk, max_attempts=args.max_attempts, timeout=args.post_timeout)
            else:
                status = post_feishu(webhook_url, chunk, max_attempts=args.max_attempts, timeout=args.post_timeout)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            failed_chunks.append((target, index, symbols, message))
            print(f"{target}_chunk_failed chunk={index}/{len(chunks)} symbols={symbols} error={message}", flush=True)
            if args.stop_on_error:
                raise
            if index < len(chunks):
                time.sleep(args.chunk_delay)
            continue
        mark_sent(target, sent, chunk_rows, index)
        save_cache(args.cache, cache)
        pushed += len(chunk_rows)
        print(f"{target}_sent chunk={index}/{len(chunks)} http_status={status} pushed_so_far={pushed}", flush=True)
        if index < len(chunks):
            time.sleep(args.chunk_delay)
    return pushed, failed_chunks


def main() -> int:
    args = parse_args()
    if args.clear_cache:
        save_cache(args.cache, {"sent": {}})
        print(f"cache_cleared={args.cache}")
        return 0

    rows = load_csv(args.signals)
    date_text = target_date_text(args.date, rows)
    metadata = load_metadata(args.metadata)
    cache = load_cache(args.cache)
    sent = cache.setdefault("sent", {})
    candidates = [row for row in rows if is_d_candidate(row, args.timeframe, date_text)]
    targets = ["discord", "feishu"] if args.target == "both" else [args.target]
    pending_by_target = {
        target: [row for row in candidates if args.force or delivery_key(target, row) not in sent]
        for target in targets
    }
    pending_count = sum(len(items) for items in pending_by_target.values())
    print(
        f"timeframe={args.timeframe} date={date_text or 'all'} candidates={len(candidates)} "
        f"pending={pending_count} target={args.target} signals={args.signals} cache={args.cache}",
        flush=True,
    )
    if not pending_count:
        return 0

    if args.dry_run:
        for target in targets:
            pending = pending_by_target[target]
            header = f"【UpBottom 底背离 D 二次突破确认】{date_text or 'all'}，{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            chunks = chunk_alerts(pending, metadata, header, args.chunk_limit)
            print(f"{target}_chunks={len(chunks)}", flush=True)
            for index, (chunk, chunk_rows) in enumerate(chunks, start=1):
                symbols = ",".join(row.get("symbol", "") for row in chunk_rows)
                print(f"\n--- dry_run_target={target} chunk={index}/{len(chunks)} alerts={len(chunk_rows)} symbols={symbols} ---")
                print("\n" + chunk + "\n")
        return 0

    discord_webhook_url = args.webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or CREDENTIALS_DISCORD_WEBHOOK_URL
    feishu_webhook_url = (
        args.feishu_webhook_url
        or os.environ.get("FEISHU_WEBHOOK_URL")
        or CREDENTIALS_FEISHU_WEBHOOK_URL
    )
    if "discord" in targets and not discord_webhook_url:
        raise SystemExit("Missing Discord webhook URL. Set DISCORD_WEBHOOK_URL or pass --webhook-url.")
    if "feishu" in targets and not feishu_webhook_url:
        raise SystemExit("Missing Feishu webhook URL. Set FEISHU_WEBHOOK_URL or pass --feishu-webhook-url.")

    pushed_total = 0
    failed_chunks: list[tuple[str, int, str, str]] = []
    webhook_by_target = {"discord": discord_webhook_url, "feishu": feishu_webhook_url}
    for target in targets:
        pending = pending_by_target[target]
        if not pending:
            print(f"{target}_pending=0", flush=True)
            continue
        header = f"【UpBottom 底背离 D 二次突破确认】{date_text or 'all'}，{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        chunks = chunk_alerts(pending, metadata, header, args.chunk_limit)
        print(f"{target}_chunks={len(chunks)}", flush=True)
        pushed, failures = send_chunks(target, webhook_by_target[target], chunks, args, cache, sent)
        pushed_total += pushed
        failed_chunks.extend(failures)

    if failed_chunks:
        failures_path = args.cache.with_name("bottom_divergence_d_push_failures.csv")
        with failures_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["target", "chunk", "symbols", "error"])
            writer.writerows(failed_chunks)
        print(f"pushed={pushed_total} failed_chunks={len(failed_chunks)} failures={failures_path}", flush=True)
        return 2
    print(f"pushed={pushed_total} failed_chunks=0 cache={args.cache}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
