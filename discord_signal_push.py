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
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(os.environ.get("UPBOTTOM_ROOT") or Path(__file__).resolve().parent)
DATASET_NAME = os.environ.get("UPBOTTOM_DATASET", "stocks_2025_10")
OUTPUT_ROOT = ROOT / "outputs" / DATASET_NAME
SIGNALS_PATH = OUTPUT_ROOT / "ad_signals.csv"
METADATA_PATH = OUTPUT_ROOT / "sp500_metadata.csv"
STOCK_METADATA_PATH = OUTPUT_ROOT / "stock_metadata.csv"
CACHE_PATH = OUTPUT_ROOT / "discord_push_cache.json"

try:
    from credentials import DISCORD_WEBHOOK_URL as CREDENTIALS_DISCORD_WEBHOOK_URL
except ImportError:
    CREDENTIALS_DISCORD_WEBHOOK_URL = ""

try:
    from credentials import STOCK_CN_NAMES as CREDENTIALS_STOCK_CN_NAMES
except ImportError:
    CREDENTIALS_STOCK_CN_NAMES = {}


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_metadata(path: Path) -> dict[str, dict[str, str]]:
    metadata = {row.get("symbol", ""): row for row in load_csv(path) if row.get("symbol")}
    for symbol, chinese_name in CREDENTIALS_STOCK_CN_NAMES.items():
        item = metadata.setdefault(symbol, {"symbol": symbol})
        item["chinese_name"] = str(chinese_name)
    return metadata


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


def format_alert(row: dict[str, str], metadata: dict[str, dict[str, str]]) -> str:
    symbol = row.get("symbol", "")
    info = metadata.get(symbol, {})
    english_name = info.get("english_name") or "未获取"
    chinese_name = info.get("chinese_name") or "未配置"
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
            f"中文名：{chinese_name}",
            f"行业：{sector} / {sub_industry}",
            f"第一金叉 GA：{row.get('golden_A_time', '-')} @ {row.get('golden_A_price', '-')}",
            f"BM：{row.get('BM_time', '-')} @ {row.get('BM_price', '-')}",
            f"B：{row.get('B_time', '-')} @ {row.get('B_price', '-')}",
            f"第二金叉 GB：{row.get('golden_B_time', '-')} @ {row.get('golden_B_price', '-')}",
            f"突破BM：{row.get('BM_break_time', '-')} @ {row.get('BM_break_price', '-')}",
            f"CM：{row.get('CM_time') or '-'} @ {row.get('CM_price') or '-'}",
            f"C：{c_text}",
            f"状态：{row.get('structure_status', '-')}",
            f"图表：{row.get('chart_file', '-')}",
        ]
    )


def chunk_messages(messages: Iterable[str], limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for message in messages:
        item = message.strip()
        if not item:
            continue
        if current and len(current) + len(item) + 4 > limit:
            chunks.append(current)
            current = item
        elif current:
            current += "\n\n---\n\n" + item
        else:
            current = item
    if current:
        chunks.append(current)
    return chunks


def post_discord(webhook_url: str, content: str) -> None:
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "UpBottom/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push BM-break bottom-divergence alerts to Discord.")
    parser.add_argument("--timeframe", choices=["4h", "1day", "all"], default="all")
    parser.add_argument("--signals", type=Path, default=SIGNALS_PATH)
    parser.add_argument("--metadata", type=Path, default=default_metadata_path())
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--refresh-metadata", action="store_true", help="Refresh S&P 500 names and industry metadata.")
    parser.add_argument("--force", action="store_true", help="Push alerts even if they were already sent.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending or updating cache.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    webhook_url = args.webhook_url or os.environ.get("DISCORD_WEBHOOK_URL") or CREDENTIALS_DISCORD_WEBHOOK_URL
    if args.refresh_metadata or not args.metadata.exists():
        try:
            from fetch_sp500_2026_and_mark import fetch_sp500_metadata, write_metadata_csv

            write_metadata_csv(fetch_sp500_metadata(), args.metadata)
        except Exception as exc:
            print(f"metadata_refresh_failed={exc}")
    rows = load_csv(args.signals)
    metadata = load_metadata(args.metadata)
    cache = load_cache(args.cache)
    sent = cache.setdefault("sent", {})
    candidates = [row for row in rows if is_push_candidate(row, args.timeframe)]
    pending = [row for row in candidates if args.force or alert_key(row) not in sent]

    print(f"timeframe={args.timeframe} candidates={len(candidates)} pending={len(pending)} force={args.force}")
    if not pending:
        return 0

    messages = [format_alert(row, metadata) for row in pending]
    header = f"【UpBottom 底背离提醒】{args.timeframe}，新增 {len(pending)} 条，生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    chunks = chunk_messages([header, *messages])
    if args.dry_run:
        for chunk in chunks:
            print("\n" + chunk + "\n")
        return 0
    if not webhook_url:
        raise SystemExit("Missing Discord webhook URL. Set DISCORD_WEBHOOK_URL or credentials.DISCORD_WEBHOOK_URL.")

    for chunk in chunks:
        post_discord(webhook_url, chunk)

    now = datetime.now().isoformat(timespec="seconds")
    for row in pending:
        sent[alert_key(row)] = {
            "sent_at": now,
            "symbol": row.get("symbol", ""),
            "timeframe": row.get("timeframe", ""),
            "BM_break_time": row.get("BM_break_time", ""),
            "structure_status": row.get("structure_status", ""),
        }
    save_cache(args.cache, cache)
    print(f"pushed={len(pending)} cache={args.cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
