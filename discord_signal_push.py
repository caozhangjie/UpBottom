"""Push bottom-divergence BM-break alerts to Discord.

Typical runs:

    python discord_signal_push.py --timeframe 4h
    python discord_signal_push.py --timeframe 1day
    python discord_signal_push.py --timeframe 4h --force
    python discord_signal_push.py --timeframe 4h --dry-run
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
DATASET_NAME = os.environ.get("UPBOTTOM_DATASET", "stocks_2025_10")
OUTPUT_ROOT = RUNTIME_ROOT / "outputs" / DATASET_NAME
SIGNALS_PATH = OUTPUT_ROOT / "ad_signals.csv"
METADATA_PATH = OUTPUT_ROOT / "sp500_metadata.csv"
STOCK_METADATA_PATH = OUTPUT_ROOT / "stock_metadata.csv"
CACHE_PATH = OUTPUT_ROOT / "discord_push_cache.json"

try:
    from credentials import DISCORD_WEBHOOK_URL as CREDENTIALS_DISCORD_WEBHOOK_URL
except ImportError:
    CREDENTIALS_DISCORD_WEBHOOK_URL = ""

try:
    from credentials import FEISHU_WEBHOOK_URL as CREDENTIALS_FEISHU_WEBHOOK_URL
except ImportError:
    CREDENTIALS_FEISHU_WEBHOOK_URL = ""

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


def alert_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            row.get("symbol", ""),
            row.get("timeframe", ""),
            row.get("golden_A_time", ""),
            row.get("golden_B_time", ""),
            row.get("BM_break_time", ""),
        ]
    )


def delivery_key(target: str, row: dict[str, str]) -> str:
    key = alert_key(row)
    if target == "discord":
        return key
    return f"{target}|{key}"


def is_push_candidate(row: dict[str, str], timeframe: str) -> bool:
    if timeframe != "all" and row.get("timeframe") != timeframe:
        return False
    if not row.get("BM_break_time"):
        return False
    if row.get("failure_type") in {"B_FAIL", "C_FAIL"}:
        return False
    if row.get("structure_status") in {"NO_BM_BREAK", "STRUCTURE_FAILED"}:
        return False
    return True


def structure_stage_text(row: dict[str, str]) -> str:
    c_items = json.loads(row.get("C_sequence") or "[]")
    if row.get("D_time"):
        return "已经二次突破"
    if c_items:
        return "回踩结束但是没有二次突破"
    if row.get("CM_time"):
        return "到高点后回踩进行中"
    if row.get("BM_break_time"):
        return "突破后到高点过程中"
    return row.get("structure_status") or "-"


def format_alert(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    info = metadata.get(symbol, {})
    english_name = info.get("english_name") or "未获取"
    sector = info.get("sector") or "未获取"
    sub_industry = info.get("sub_industry") or "未获取"
    c_items = json.loads(row.get("C_sequence") or "[]")
    c_text = "-"
    if c_items:
        c = c_items[0]
        c_text = f"{c.get('time', '-')} @ {c.get('price', '-')}"
    return "\n".join(
        [
            f"**{symbol} | {row.get('timeframe', '')} | 底背离 BM 突破警报**",
            f"英文名：{english_name}",
            f"行业：{sector} / {sub_industry}",
            f"第一金叉 GA：{row.get('golden_A_time', '-')} @ {row.get('golden_A_price', '-')}",
            f"BM：{row.get('BM_time', '-')} @ {row.get('BM_price', '-')}",
            f"B：{row.get('B_time', '-')} @ {row.get('B_price', '-')}",
            f"第二金叉 GB：{row.get('golden_B_time', '-')} @ {row.get('golden_B_price', '-')}",
            f"突破BM：{row.get('BM_break_time', '-')} @ {row.get('BM_break_price', '-')}",
            f"CM：{row.get('CM_time') or '-'} @ {row.get('CM_price') or '-'}",
            f"C：{c_text}",
            f"状态：{structure_stage_text(row)}",
            f"图表：{row.get('chart_file') or '-'}",
        ]
    )


def chunk_alerts(
    rows: Iterable[dict[str, str]],
    metadata: dict[str, dict[str, str]],
    header: str,
    limit: int = 1500,
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
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            wait = min(2 ** attempt, 60)
            if exc.code == 429:
                try:
                    wait = max(float(json.loads(body).get("retry_after", wait)), 1.0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    wait = max(wait, 5)
            print(
                f"discord_post_failed attempt={attempt}/{max_attempts} "
                f"http_status={exc.code} wait_seconds={wait:.1f} body={body[:300]}",
                flush=True,
            )
            if attempt == max_attempts:
                raise
            time.sleep(wait)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            http.client.IncompleteRead,
            socket.timeout,
            ssl.SSLError,
        ) as exc:
            wait = min(2 ** attempt, 60)
            print(
                f"discord_post_failed attempt={attempt}/{max_attempts} "
                f"error={type(exc).__name__}: {exc} wait_seconds={wait}",
                flush=True,
            )
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
                try:
                    result = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    result = {}
                code = result.get("code", result.get("StatusCode", result.get("status_code", 0)))
                if status >= 400 or code not in (0, "0", None):
                    raise RuntimeError(f"Feishu webhook returned status={status} body={body[:300]}")
                return status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            wait = min(2 ** attempt, 60)
            print(
                f"feishu_post_failed attempt={attempt}/{max_attempts} "
                f"http_status={exc.code} wait_seconds={wait:.1f} body={body[:300]}",
                flush=True,
            )
            if attempt == max_attempts:
                raise
            time.sleep(wait)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            http.client.IncompleteRead,
            socket.timeout,
            ssl.SSLError,
            RuntimeError,
        ) as exc:
            wait = min(2 ** attempt, 60)
            print(
                f"feishu_post_failed attempt={attempt}/{max_attempts} "
                f"error={type(exc).__name__}: {exc} wait_seconds={wait}",
                flush=True,
            )
            if attempt == max_attempts:
                raise
            time.sleep(wait)
    raise RuntimeError("Feishu post failed without a captured exception.")


def mark_sent(target: str, sent: dict, rows: list[dict[str, str]], chunk_index: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        sent[delivery_key(target, row)] = {
            "sent_at": now,
            "target": target,
            "chunk_index": chunk_index,
            "symbol": row.get("symbol", ""),
            "timeframe": row.get("timeframe", ""),
            "BM_break_time": row.get("BM_break_time", ""),
            "structure_status": row.get("structure_status", ""),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push BM-break bottom-divergence alerts to Discord.")
    parser.add_argument("--timeframe", choices=["4h", "1day", "all"], default="all")
    parser.add_argument("--signals", type=Path, default=SIGNALS_PATH)
    parser.add_argument("--metadata", type=Path, default=default_metadata_path())
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--feishu-webhook-url", default=None)
    parser.add_argument("--target", choices=["discord", "feishu", "both"], default="discord")
    parser.add_argument("--refresh-metadata", action="store_true", help="Refresh S&P 500 names and industry metadata.")
    parser.add_argument("--force", action="store_true", help="Push alerts even if they were already sent.")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the Discord dedupe cache and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending or updating cache.")
    parser.add_argument("--chunk-limit", type=int, default=1500, help="Maximum characters per Discord message chunk.")
    parser.add_argument("--post-timeout", type=int, default=60, help="HTTP timeout seconds for each Discord request.")
    parser.add_argument("--max-attempts", type=int, default=8, help="Retry attempts for each Discord message chunk.")
    parser.add_argument("--chunk-delay", type=float, default=2.0, help="Delay seconds between Discord message chunks.")
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop immediately when a chunk fails after all retries. By default later chunks continue.",
    )
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
        print(
            f"{target}_sending chunk={index}/{len(chunks)} alerts={len(chunk_rows)} "
            f"bytes={len(chunk.encode('utf-8'))} symbols={symbols}",
            flush=True,
        )
        try:
            if target == "discord":
                status = post_discord(webhook_url, chunk, max_attempts=args.max_attempts, timeout=args.post_timeout)
            else:
                status = post_feishu(webhook_url, chunk, max_attempts=args.max_attempts, timeout=args.post_timeout)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            failed_chunks.append((target, index, symbols, message))
            print(
                f"{target}_chunk_failed chunk={index}/{len(chunks)} alerts={len(chunk_rows)} "
                f"symbols={symbols} error={message}",
                flush=True,
            )
            if args.stop_on_error:
                raise
            if index < len(chunks):
                time.sleep(args.chunk_delay)
            continue
        mark_sent(target, sent, chunk_rows, index)
        save_cache(args.cache, cache)
        pushed += len(chunk_rows)
        print(
            f"{target}_sent chunk={index}/{len(chunks)} http_status={status} "
            f"pushed_so_far={pushed} cache={args.cache}",
            flush=True,
        )
        if index < len(chunks):
            time.sleep(args.chunk_delay)
    return pushed, failed_chunks


def main() -> int:
    args = parse_args()
    if args.clear_cache:
        save_cache(args.cache, {"sent": {}})
        print(f"cache_cleared={args.cache}")
        return 0

    discord_webhook_url = args.webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or CREDENTIALS_DISCORD_WEBHOOK_URL
    feishu_webhook_url = (
        args.feishu_webhook_url
        or os.environ.get("FEISHU_WEBHOOK_URL")
        or CREDENTIALS_FEISHU_WEBHOOK_URL
    )
    if args.refresh_metadata or not args.metadata.exists():
        try:
            from fetch_sp500_2026_and_mark import get_sp500_metadata, write_metadata_csv

            write_metadata_csv(get_sp500_metadata(refresh=True), args.metadata)
        except Exception as exc:
            print(f"metadata_refresh_failed={exc}")
    rows = load_csv(args.signals)
    metadata = load_metadata(args.metadata)
    cache = load_cache(args.cache)
    sent = cache.setdefault("sent", {})
    candidates = [row for row in rows if is_push_candidate(row, args.timeframe)]
    targets = ["discord", "feishu"] if args.target == "both" else [args.target]
    pending_by_target = {
        target: [row for row in candidates if args.force or delivery_key(target, row) not in sent]
        for target in targets
    }
    pending_count = sum(len(rows) for rows in pending_by_target.values())

    print(
        f"timeframe={args.timeframe} candidates={len(candidates)} "
        f"pending={pending_count} target={args.target} force={args.force} signals={args.signals} cache={args.cache}",
        flush=True,
    )
    if not pending_count:
        return 0

    if args.dry_run:
        for target in targets:
            pending = pending_by_target[target]
            header = f"【UpBottom 底背离提醒】{args.timeframe}，{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            chunks = chunk_alerts(pending, metadata, header, limit=args.chunk_limit)
            print(f"{target}_chunks={len(chunks)}", flush=True)
            for index, (chunk, chunk_rows) in enumerate(chunks, start=1):
                symbols = ",".join(row.get("symbol", "") for row in chunk_rows)
                print(f"\n--- dry_run_target={target} chunk={index}/{len(chunks)} alerts={len(chunk_rows)} symbols={symbols} ---")
                print("\n" + chunk + "\n")
        return 0
    if "discord" in targets and not discord_webhook_url:
        raise SystemExit("Missing Discord webhook URL. Set DISCORD_WEBHOOK_URL or credentials.DISCORD_WEBHOOK_URL.")
    if "feishu" in targets and not feishu_webhook_url:
        raise SystemExit("Missing Feishu webhook URL. Set FEISHU_WEBHOOK_URL or credentials.FEISHU_WEBHOOK_URL.")

    pushed_total = 0
    failed_chunks: list[tuple[str, int, str, str]] = []
    webhook_by_target = {"discord": discord_webhook_url, "feishu": feishu_webhook_url}
    for target in targets:
        pending = pending_by_target[target]
        if not pending:
            print(f"{target}_pending=0", flush=True)
            continue
        header = f"【UpBottom 底背离提醒】{args.timeframe}，{target} 新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        chunks = chunk_alerts(pending, metadata, header, limit=args.chunk_limit)
        print(f"{target}_chunks={len(chunks)}", flush=True)
        pushed, failures = send_chunks(target, webhook_by_target[target], chunks, args, cache, sent)
        pushed_total += pushed
        failed_chunks.extend(failures)

    if failed_chunks:
        failures_path = args.cache.with_name("discord_push_failures.csv")
        with failures_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["target", "chunk", "symbols", "error"])
            writer.writerows(failed_chunks)
        print(f"pushed={pushed_total} failed_chunks={len(failed_chunks)} failures={failures_path} cache={args.cache}", flush=True)
        return 2

    print(f"pushed={pushed_total} failed_chunks=0 cache={args.cache}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
