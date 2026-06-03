from __future__ import annotations

import csv
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from constants import OUTPUT_ROOT, PUSH_STATE_ROOT


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def parse_date(text: str) -> date:
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def exclusive_end(date_text: str, days: int = 1) -> str:
    return (parse_date(date_text) + timedelta(days=days)).isoformat()


def fmt_price(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def fmt_ratio(value: float) -> str:
    return f"{value * 100:.1f}%"


def today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def state_path(name: str) -> Path:
    return PUSH_STATE_ROOT / name


def load_sent_cache(path: Path) -> dict[str, dict]:
    data = load_json(path, {"sent": {}})
    sent = data.get("sent")
    if not isinstance(sent, dict):
        return {}
    return sent


def save_sent_cache(path: Path, sent: dict[str, dict]) -> None:
    save_json(path, {"sent": sent})


def mark_sent(sent: dict[str, dict], keys: Iterable[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for key in keys:
        sent[key] = {"sent_at": now}


def load_metadata() -> dict[str, dict[str, str]]:
    stock_path = OUTPUT_ROOT / "stock_metadata.csv"
    sp500_path = OUTPUT_ROOT / "sp500_metadata.csv"
    rows = load_csv(stock_path if stock_path.exists() else sp500_path)
    return {row.get("symbol", ""): row for row in rows if row.get("symbol")}


def stock_label(symbol: str, metadata: dict[str, dict[str, str]]) -> str:
    item = metadata.get(symbol, {})
    name = item.get("chinese_name") or item.get("english_name") or ""
    return f"{symbol} {name}".strip()


def chunk_texts(header: str, items: list[str], limit: int = 16000) -> list[str]:
    chunks: list[str] = []
    current = header.strip()
    for item in items:
        piece = item.strip()
        candidate = current + "\n\n" + piece if current else piece
        if len(candidate.encode("utf-8")) > limit and current:
            chunks.append(current)
            current = header.strip() + "\n\n" + piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


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


def send_or_print(
    title: str,
    items: list[str],
    webhook_url: str,
    dry_run: bool,
    chunk_limit: int = 16000,
) -> int:
    if not items:
        print(f"{title}: no messages", flush=True)
        return 0
    chunks = chunk_texts(title, items, chunk_limit)
    if dry_run:
        for index, chunk in enumerate(chunks, start=1):
            print(f"\n--- dry_run chunk={index}/{len(chunks)} ---\n{chunk}", flush=True)
        return len(items)
    if not webhook_url:
        raise SystemExit(f"Missing Feishu webhook for {title}. Configure constants.py or environment variables.")
    for index, chunk in enumerate(chunks, start=1):
        status = post_feishu(webhook_url, chunk)
        print(f"feishu_sent chunk={index}/{len(chunks)} http_status={status}", flush=True)
    return len(items)
