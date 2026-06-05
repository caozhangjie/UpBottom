from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ad_structure_v05_core import Row, load_rows
from constants import DATA_ROOT, OUTPUT_ROOT
from push_utils import load_csv, load_json, parse_date, save_json, state_path


SIGNALS_PATH = OUTPUT_ROOT / "ad_signals.csv"
POSITIONS_PATH = state_path("bottom_trade_positions.json")


@dataclass(frozen=True)
class ExitCheck:
    triggered: bool
    rule: str
    reference_price: float
    below_ratio: float
    ma_price: float | None
    ma_below_ratio: float
    signal_below_ratio: float
    minute_total: int


def load_ad_signals(path: Path = SIGNALS_PATH) -> list[dict[str, str]]:
    return load_csv(path)


def bottom_history_key(row: dict[str, str]) -> str:
    return "|".join(
        [
            "bottom_history",
            row.get("symbol", ""),
            row.get("timeframe", ""),
            row.get("golden_A_time", ""),
            row.get("golden_B_time", ""),
            row.get("BM_break_time", ""),
        ]
    )


def is_valid_bm_break(row: dict[str, str]) -> bool:
    if not row.get("BM_break_time"):
        return False
    if row.get("failure_type") in {"B_FAIL", "C_FAIL"}:
        return False
    if row.get("structure_status") in {"NO_BM_BREAK", "STRUCTURE_FAILED"}:
        return False
    return True


def is_trade_buy_candidate(row: dict[str, str], date_text: str) -> bool:
    confirm_time = c_confirm_time(row)
    failure_time = str(row.get("failure_time") or "")
    return (
        row.get("timeframe") == "1day"
        and bool(confirm_time)
        and confirm_time[:10] == date_text
        and (not failure_time or failure_time[:10] > date_text)
    )


def first_c_point(row: dict[str, str]) -> dict | None:
    try:
        items = json.loads(row.get("C_sequence") or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    return item if isinstance(item, dict) else None


def c_confirm_time(row: dict[str, str]) -> str:
    c_point = first_c_point(row)
    if not c_point:
        return ""
    return str(c_point.get("confirm_time") or "")


def c_confirm_row(row: dict[str, str], daily_rows: list[Row]) -> Row | None:
    confirm_date = c_confirm_time(row)[:10]
    if not confirm_date:
        return None
    for daily_row in daily_rows:
        if daily_row.datetime[:10] == confirm_date:
            return daily_row
    return None


def daily_path(symbol: str) -> Path:
    return DATA_ROOT / "1day" / f"{symbol}_1day_indicators.csv"


def load_daily_rows(symbol: str, min_date: str = "1900-01-01") -> list[Row]:
    path = daily_path(symbol)
    return load_rows(path, min_date=min_date) if path.exists() else []


def daily_by_date(rows: list[Row]) -> dict[str, Row]:
    return {row.datetime[:10]: row for row in rows}


def next_daily_row_after(rows: list[Row], date_text: str) -> Row | None:
    for row in rows:
        if row.datetime[:10] > date_text:
            return row
    return None


def prior_ma_by_date(rows: list[Row], window: int) -> dict[str, float]:
    out: dict[str, float] = {}
    closes: list[float] = []
    for row in rows:
        date_text = row.datetime[:10]
        if len(closes) >= window:
            out[date_text] = sum(closes[-window:]) / window
        closes.append(row.close)
    return out


def below_ratio(rows: list[Row], price: float) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row.close < price) / len(rows)


def check_exit_signal(
    daily_rows: list[Row],
    minute_rows: list[Row],
    date_text: str,
    reference_price: float,
    below_threshold: float,
    ma_window: int,
    min_minutes: int,
) -> ExitCheck:
    minute_rows = [row for row in minute_rows if row.datetime[:10] == date_text]
    if len(minute_rows) < min_minutes:
        return ExitCheck(False, "", 0.0, 0.0, None, 0.0, 0.0, len(minute_rows))
    ma_price = prior_ma_by_date(daily_rows, ma_window).get(date_text)
    ma_ratio = below_ratio(minute_rows, ma_price) if ma_price is not None else 0.0
    signal_ratio = below_ratio(minute_rows, reference_price)
    if ma_ratio >= below_threshold:
        return ExitCheck(True, f"MA{ma_window}", ma_price or 0.0, ma_ratio, ma_price, ma_ratio, signal_ratio, len(minute_rows))
    if signal_ratio >= below_threshold:
        return ExitCheck(True, "REFERENCE_CLOSE", reference_price, signal_ratio, ma_price, ma_ratio, signal_ratio, len(minute_rows))
    return ExitCheck(False, "", 0.0, 0.0, ma_price, ma_ratio, signal_ratio, len(minute_rows))


def load_positions() -> dict[str, dict]:
    data = load_json(POSITIONS_PATH, {"positions": {}})
    positions = data.get("positions")
    return positions if isinstance(positions, dict) else {}


def save_positions(positions: dict[str, dict]) -> None:
    save_json(POSITIONS_PATH, {"positions": positions})


def has_open_position(positions: dict[str, dict], symbol: str) -> bool:
    item = positions.get(symbol)
    return bool(item and item.get("status") == "OPEN")


def open_positions(positions: dict[str, dict]) -> dict[str, dict]:
    return {symbol: item for symbol, item in positions.items() if item.get("status") == "OPEN"}


def can_check_exit(position: dict, date_text: str) -> bool:
    entry_date = str(position.get("planned_entry_date") or position.get("entry_date") or "")
    if not entry_date:
        return False
    return parse_date(date_text) >= parse_date(entry_date)
