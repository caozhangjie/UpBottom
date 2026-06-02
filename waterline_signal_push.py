"""Push waterline signal-day and trade-day alerts to Feishu/Discord.

This file is intentionally separate from bottom_divergence_signal_push.py.
It only reads CSVs produced by waterline_signal.py:

- waterline_candidates.csv for signal-day alerts
- waterline_entries.csv for trade-day confirmed alerts
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable


RUNTIME_ROOT = Path(os.environ.get("UPBOTTOM_RUNTIME_ROOT") or "/data/UpBottom")
DATASET_NAME = os.environ.get("UPBOTTOM_DATASET", "stocks")
OUTPUT_ROOT = RUNTIME_ROOT / "outputs" / DATASET_NAME
CANDIDATES_PATH = OUTPUT_ROOT / "waterline_candidates.csv"
ENTRIES_PATH = OUTPUT_ROOT / "waterline_entries.csv"
METADATA_PATH = OUTPUT_ROOT / "sp500_metadata.csv"
STOCK_METADATA_PATH = OUTPUT_ROOT / "stock_metadata.csv"
CACHE_PATH = OUTPUT_ROOT / "waterline_push_cache.json"

try:
    from credentials import DISCORD_WEBHOOK_URL as CREDENTIALS_DISCORD_WEBHOOK_URL
except ImportError:
    CREDENTIALS_DISCORD_WEBHOOK_URL = ""

try:
    from credentials import FEISHU_WEBHOOK_URL as CREDENTIALS_FEISHU_WEBHOOK_URL
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_URL = ""

try:
    from credentials import WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL as CREDENTIALS_WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL
except ImportError:
    CREDENTIALS_WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL = ""

try:
    from credentials import WATERLINE_TRADE_FEISHU_WEBHOOK_URL as CREDENTIALS_WATERLINE_TRADE_FEISHU_WEBHOOK_URL
except ImportError:
    CREDENTIALS_WATERLINE_TRADE_FEISHU_WEBHOOK_URL = ""


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_metadata(path: Path) -> dict[str, dict[str, str]]:
    return {row.get("symbol", ""): row for row in load_csv(path) if row.get("symbol")}


def default_metadata_path() -> Path:
    return STOCK_METADATA_PATH if STOCK_METADATA_PATH.exists() else METADATA_PATH


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {"sent": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"sent": {}}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def metadata_text(symbol: str, metadata: dict[str, dict[str, str]]) -> tuple[str, str, str]:
    info = metadata.get(symbol, {})
    english_name = info.get("english_name") or "未获取"
    sector = info.get("sector") or "未获取"
    sub_industry = info.get("sub_industry") or "未获取"
    return english_name, sector, sub_industry


def signal_key(row: dict[str, str], alert_stage: str) -> str:
    if alert_stage == "signal-day":
        return "|".join(["waterline-signal-day", row.get("symbol", ""), row.get("signal_date", "")])
    return "|".join(
        [
            "waterline-trade-day",
            row.get("symbol", ""),
            row.get("signal_date", ""),
            row.get("trade_date", ""),
            row.get("entry_time", ""),
        ]
    )


def delivery_key(target: str, row: dict[str, str], alert_stage: str) -> str:
    return f"{target}|{signal_key(row, alert_stage)}"


def is_push_candidate(row: dict[str, str], alert_stage: str) -> bool:
    if alert_stage == "signal-day":
        return bool(row.get("symbol") and row.get("signal_date") and row.get("trade_date"))
    return bool(row.get("symbol") and row.get("signal_date") and row.get("trade_date") and row.get("entry_time"))


def format_signal_day_alert(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    english_name, sector, sub_industry = metadata_text(symbol, metadata)
    return "\n".join(
        [
            f"**{symbol} | 水上漂信号日提醒**",
            f"英文名：{english_name}",
            f"行业：{sector} / {sub_industry}",
            f"信号日：{row.get('signal_date', '-')} @ {row.get('signal_close', '-')}",
            f"信号日开高低收：{row.get('signal_open', '-')} / {row.get('signal_high', '-')} / {row.get('signal_low', '-')} / {row.get('signal_close', '-')}",
            f"量能倍数：{row.get('volume_ratio', '-')}，前10日均量：{row.get('prior_volume_avg', '-')}",
            f"下一交易日：{row.get('trade_date', '-')} @ {row.get('trade_close', '-')}",
        ]
    )


def format_trade_day_alert(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    english_name, sector, sub_industry = metadata_text(symbol, metadata)
    return "\n".join(
        [
            f"**{symbol} | 水上漂交易日确认提醒**",
            f"英文名：{english_name}",
            f"行业：{sector} / {sub_industry}",
            f"信号日：{row.get('signal_date', '-')} @ {row.get('signal_close', '-')}",
            f"交易日：{row.get('trade_date', '-')}",
            f"分钟水上比例：{row.get('minute_above_ratio', '-')} ({row.get('minute_above', '-')}/{row.get('minute_total', '-')})",
            f"入场确认：{row.get('entry_time', '-')} @ {row.get('entry_price', '-')}",
            f"分钟周期：{row.get('minute_timeframe', '-')}",
        ]
    )


def format_alert(row: dict[str, str], metadata: dict[str, dict[str, str]], alert_stage: str) -> str:
    if alert_stage == "signal-day":
        return format_signal_day_alert(row, metadata)
    return format_trade_day_alert(row, metadata)


def chunk_alerts(
    rows: Iterable[dict[str, str]],
    metadata: dict[str, dict[str, str]],
    header: str,
    alert_stage: str,
    limit: int = 1500,
) -> list[tuple[str, list[dict[str, str]]]]:
    chunks: list[tuple[str, list[dict[str, str]]]] = []
    current = ""
    current_rows: list[dict[str, str]] = []
    for row in rows:
        item = format_alert(row, metadata, alert_stage).strip()
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


def post_discord(webhook_url: str, content: str, max_attempts: int = 8, timeout: int = 60) -> int:
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "UpBottom/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
                return int(getattr(response, "status", 0) or response.getcode())
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, socket.timeout, ssl.SSLError) as exc:
            wait = min(2 ** attempt, 60)
            print(f"discord_post_failed attempt={attempt}/{max_attempts} error={type(exc).__name__}: {exc} wait_seconds={wait}", flush=True)
            if attempt == max_attempts:
                raise
            time.sleep(wait)
    raise RuntimeError("Discord post failed without a captured exception.")


def post_feishu(webhook_url: str, content: str, max_attempts: int = 8, timeout: int = 60) -> int:
    payload = json.dumps({"msg_type": "text", "content": {"text": content}}, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "UpBottom/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 0) or response.getcode())
                result = json.loads(body) if body else {}
                code = result.get("code", result.get("StatusCode", result.get("status_code", 0)))
                if status >= 400 or code not in (0, "0", None):
                    raise RuntimeError(f"Feishu webhook returned status={status} body={body[:300]}")
                return status
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, socket.timeout, ssl.SSLError, RuntimeError, json.JSONDecodeError) as exc:
            wait = min(2 ** attempt, 60)
            print(f"feishu_post_failed attempt={attempt}/{max_attempts} error={type(exc).__name__}: {exc} wait_seconds={wait}", flush=True)
            if attempt == max_attempts:
                raise
            time.sleep(wait)
    raise RuntimeError("Feishu post failed without a captured exception.")


def mark_sent(target: str, sent: dict, rows: list[dict[str, str]], chunk_index: int, alert_stage: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        sent[delivery_key(target, row, alert_stage)] = {
            "sent_at": now,
            "target": target,
            "alert_stage": alert_stage,
            "chunk_index": chunk_index,
            "symbol": row.get("symbol", ""),
            "signal_date": row.get("signal_date", ""),
            "trade_date": row.get("trade_date", ""),
            "entry_time": row.get("entry_time", ""),
        }


def default_signals_path(alert_stage: str) -> Path:
    return CANDIDATES_PATH if alert_stage == "signal-day" else ENTRIES_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push waterline signal-day or trade-day alerts.")
    parser.add_argument("--alert-stage", choices=["signal-day", "trade-day"], default="trade-day")
    parser.add_argument("--signals", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=default_metadata_path())
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--feishu-webhook-url", default=None)
    parser.add_argument("--target", choices=["discord", "feishu", "both"], default="feishu")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chunk-limit", type=int, default=1500)
    parser.add_argument("--post-timeout", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--chunk-delay", type=float, default=2.0)
    parser.add_argument("--stop-on-error", action="store_true")
    return parser.parse_args()


def feishu_webhook_for_stage(args: argparse.Namespace) -> str:
    if args.feishu_webhook_url:
        return args.feishu_webhook_url
    if args.alert_stage == "signal-day":
        return (
            os.environ.get("WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL")
            or CREDENTIALS_WATERLINE_SIGNAL_FEISHU_WEBHOOK_URL
            or os.environ.get("FEISHU_WEBHOOK_URL")
            or CREDENTIALS_FEISHU_WEBHOOK_URL
        )
    return (
        os.environ.get("WATERLINE_TRADE_FEISHU_WEBHOOK_URL")
        or CREDENTIALS_WATERLINE_TRADE_FEISHU_WEBHOOK_URL
        or os.environ.get("FEISHU_WEBHOOK_URL")
        or CREDENTIALS_FEISHU_WEBHOOK_URL
    )


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
        mark_sent(target, sent, chunk_rows, index, args.alert_stage)
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

    signals_path = args.signals or default_signals_path(args.alert_stage)
    rows = load_csv(signals_path)
    metadata = load_metadata(args.metadata)
    cache = load_cache(args.cache)
    sent = cache.setdefault("sent", {})
    candidates = [row for row in rows if is_push_candidate(row, args.alert_stage)]
    targets = ["discord", "feishu"] if args.target == "both" else [args.target]
    pending_by_target = {
        target: [row for row in candidates if args.force or delivery_key(target, row, args.alert_stage) not in sent]
        for target in targets
    }
    pending_count = sum(len(items) for items in pending_by_target.values())
    print(
        f"alert_stage={args.alert_stage} candidates={len(candidates)} pending={pending_count} "
        f"target={args.target} signals={signals_path} cache={args.cache}",
        flush=True,
    )
    if not pending_count:
        return 0

    if args.dry_run:
        for target in targets:
            pending = pending_by_target[target]
            title = "水上漂信号日提醒" if args.alert_stage == "signal-day" else "水上漂交易日确认提醒"
            header = f"【UpBottom {title}】{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            chunks = chunk_alerts(pending, metadata, header, args.alert_stage, limit=args.chunk_limit)
            print(f"{target}_chunks={len(chunks)}", flush=True)
            for index, (chunk, chunk_rows) in enumerate(chunks, start=1):
                symbols = ",".join(row.get("symbol", "") for row in chunk_rows)
                print(f"\n--- dry_run_target={target} chunk={index}/{len(chunks)} alerts={len(chunk_rows)} symbols={symbols} ---")
                print("\n" + chunk + "\n")
        return 0

    discord_webhook_url = args.webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or CREDENTIALS_DISCORD_WEBHOOK_URL
    feishu_webhook_url = feishu_webhook_for_stage(args)
    if "discord" in targets and not discord_webhook_url:
        raise SystemExit("Missing Discord webhook URL. Set DISCORD_WEBHOOK_URL or pass --webhook-url.")
    if "feishu" in targets and not feishu_webhook_url:
        raise SystemExit("Missing Feishu webhook URL for waterline alert stage.")

    webhook_by_target = {"discord": discord_webhook_url, "feishu": feishu_webhook_url}
    pushed_total = 0
    failed_chunks: list[tuple[str, int, str, str]] = []
    for target in targets:
        pending = pending_by_target[target]
        if not pending:
            print(f"{target}_pending=0", flush=True)
            continue
        title = "水上漂信号日提醒" if args.alert_stage == "signal-day" else "水上漂交易日确认提醒"
        header = f"【UpBottom {title}】{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        chunks = chunk_alerts(pending, metadata, header, args.alert_stage, limit=args.chunk_limit)
        print(f"{target}_chunks={len(chunks)}", flush=True)
        pushed, failures = send_chunks(target, webhook_by_target[target], chunks, args, cache, sent)
        pushed_total += pushed
        failed_chunks.extend(failures)

    if failed_chunks:
        failures_path = args.cache.with_name("waterline_push_failures.csv")
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
