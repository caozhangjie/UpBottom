from __future__ import annotations

from pathlib import Path
import urllib.error

from ad_structure_v05_core import Row, load_rows
from constants import TMP_MINUTE_ROOT
from fetch_sp500_2026_and_mark import fetch_twelve_data_bars, write_rows
from push_utils import exclusive_end, load_metadata


def tmp_minute_path(symbol: str) -> Path:
    return TMP_MINUTE_ROOT / f"{symbol}_1min_indicators.csv"


def load_tmp_minutes(symbol: str, date_text: str) -> list[Row]:
    path = tmp_minute_path(symbol)
    if not path.exists():
        return []
    return [row for row in load_rows(path, min_date=date_text) if row.datetime[:10] == date_text]


def source_symbol_for(symbol: str, metadata: dict[str, dict[str, str]]) -> str:
    item = metadata.get(symbol, {})
    return str(item.get("source_symbol") or symbol.replace("-", "."))


def ensure_tmp_minutes(
    symbols: list[str],
    date_text: str,
    api_key: str,
    min_minutes: int,
) -> dict[str, int]:
    metadata = load_metadata()
    TMP_MINUTE_ROOT.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for symbol in sorted(set(symbols)):
        existing = load_tmp_minutes(symbol, date_text)
        if len(existing) >= min_minutes:
            counts[symbol] = len(existing)
            continue
        source_symbol = source_symbol_for(symbol, metadata)
        try:
            rows = fetch_twelve_data_bars(source_symbol, "1min", date_text, exclusive_end(date_text), api_key)
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError, TimeoutError) as exc:
            print(
                f"tmp_1min_fetch_failed symbol={symbol} source_symbol={source_symbol} "
                f"date={date_text} error={type(exc).__name__}: {exc}",
                flush=True,
            )
            counts[symbol] = 0
            continue
        day_rows = [row for row in rows if row.datetime[:10] == date_text]
        write_rows(tmp_minute_path(symbol), day_rows)
        counts[symbol] = len(day_rows)
    return counts


def cleanup_tmp_minutes() -> int:
    if not TMP_MINUTE_ROOT.exists():
        return 0
    count = 0
    for path in TMP_MINUTE_ROOT.glob("*_1min_indicators.csv"):
        path.unlink()
        count += 1
    return count
