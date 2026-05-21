"""Standalone A-D_structure_v0.5 scanner.

This file consolidates the useful recognition logic from the existing
``claude`` scripts into one small, reviewable module. It keeps the current
v0.5 behavior:

- all structure prices use close
- golden_A/golden_B are valid golden-cross bars used for divergence comparison
- B is the lowest close in the two-golden-cross structure window
- MACD A/B values are DIF extrema between the prior death cross and each
  golden cross
- BM is max close in the two-golden-cross structure window
- BM Break is the first post-B close above BM_price
- CM is the first future-3 confirmed high after BM Break
- Cn/CnH are optional future-3 confirmed low/high points after CM
- D triggers on the first close above CM_price before structure failure

The module intentionally does not render charts. It scans local CSV files and
exports a compact index CSV that can be used for later charting or audit.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal


TIMEFRAMES = ("4h", "1day", "3day", "1week")
MIN_DATE = "2000-01-01"
STATUS_ALL = "ALL"
STATUS_COMPLETE = "D_TRIGGERED"


@dataclass(frozen=True)
class Row:
    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class ABSignal:
    symbol: str
    timeframe: str
    data_file: str
    signal_index: int
    A_index: int
    A_time: str
    A_price: float
    B_index: int
    B_time: str
    B_price: float
    golden_A_index: int
    golden_A_time: str
    golden_A_price: float
    golden_B_index: int
    golden_B_time: str
    golden_B_price: float
    AB_macd_fast_max: float
    macd_A_index: int
    macd_A_time: str
    macd_A_value: float
    macd_B_index: int
    macd_B_time: str
    macd_B_value: float


@dataclass
class ADStructure:
    symbol: str
    timeframe: str
    version: str
    A_index: int
    A_time: str
    A_price: float
    B_index: int
    B_time: str
    B_price: float
    AB_macd_fast_max: float
    AB_macd_fast_max_rule: str
    BM_index: int | None = None
    BM_time: str | None = None
    BM_price: float | None = None
    BM_price_rule: str = "max(close[golden_A_index:golden_B_index])"
    BM_break_index: int | None = None
    BM_break_time: str | None = None
    BM_break_price: float | None = None
    CM_index: int | None = None
    CM_time: str | None = None
    CM_price: float | None = None
    CM_confirm_rule: str = "close[i] > max(close[i+1:i+4])"
    C_sequence: list[dict] | None = None
    D_index: int | None = None
    D_time: str | None = None
    D_price: float | None = None
    structure_status: str = "PENDING"
    signal_type: str | None = None
    failure_type: str | None = None
    failure_C_label: str | None = None
    failure_index: int | None = None
    failure_time: str | None = None
    failure_price: float | None = None
    failure_rule: str = "close < B_price * 0.95"


def ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * alpha + out[-1] * (1 - alpha))
    return out


def load_rows(path: Path, min_date: str = MIN_DATE) -> list[Row]:
    rows: list[Row] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for item in reader:
            dt = str(item.get("datetime") or "")
            if dt[:10] < min_date:
                continue
            try:
                row = Row(
                    datetime=dt,
                    open=float(item.get("open") or ""),
                    high=float(item.get("high") or ""),
                    low=float(item.get("low") or ""),
                    close=float(item.get("close") or ""),
                    volume=float(item.get("volume") or 0),
                )
            except ValueError:
                continue
            if row.close > 0:
                rows.append(row)
    return rows


def detect_ab_signals(symbol: str, timeframe: str, path: Path, rows: list[Row]) -> list[ABSignal]:
    closes = [r.close for r in rows]
    times = [r.datetime for r in rows]
    if len(closes) < 60:
        return []

    ema_fast = ema(closes, 12)
    ema_slow = ema(closes, 26)
    dif = [fast - slow for fast, slow in zip(ema_fast, ema_slow)]
    dea = ema(dif, 9)

    crosses: list[dict[str, int | str]] = []
    for i in range(1, len(closes)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            crosses.append({"type": "golden_cross", "index": i})
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            crosses.append({"type": "death_cross", "index": i})

    crosses_with_extrema: list[dict[str, int | str | float | None]] = []
    for idx, cross in enumerate(crosses):
        opposite = "death_cross" if cross["type"] == "golden_cross" else "golden_cross"
        previous_opposite = None
        for j in range(idx - 1, -1, -1):
            if crosses[j]["type"] == opposite:
                previous_opposite = crosses[j]
                break

        info: dict[str, int | str | float | None] = {
            **cross,
            "extremum_value": None,
            "extremum_index": None,
        }
        if previous_opposite is not None:
            start = int(previous_opposite["index"])
            end = int(cross["index"]) + 1
            if cross["type"] == "golden_cross":
                extremum_value = float("inf")
                better = lambda value, current: value < current
            else:
                extremum_value = float("-inf")
                better = lambda value, current: value > current
            extremum_index = start
            for k in range(start, end):
                if better(dif[k], extremum_value):
                    extremum_value = dif[k]
                    extremum_index = k
            info["extremum_value"] = extremum_value
            info["extremum_index"] = extremum_index
        crosses_with_extrema.append(info)

    valid_golden = [
        item
        for item in crosses_with_extrema
        if item["type"] == "golden_cross" and item["extremum_value"] is not None
    ]
    signals: list[ABSignal] = []
    for i in range(1, len(valid_golden)):
        prev = valid_golden[i - 1]
        curr = valid_golden[i]
        prev_index = int(prev["index"])
        curr_index = int(curr["index"])
        prev_extremum = float(prev["extremum_value"])
        curr_extremum = float(curr["extremum_value"])
        ab_macd_fast_max = max(dif[prev_index : curr_index + 1])
        if (
            closes[curr_index] < closes[prev_index] * 0.95
            and curr_extremum > prev_extremum * 1.05
            and dif[curr_index] < 0
            and ab_macd_fast_max <= 0
        ):
            prev_extremum_index = int(prev["extremum_index"])
            curr_extremum_index = int(curr["extremum_index"])
            structure_range = range(prev_index, curr_index)
            b_index = min(structure_range, key=lambda idx: closes[idx])
            signals.append(
                ABSignal(
                    symbol=symbol,
                    timeframe=timeframe,
                    data_file=str(path),
                    signal_index=len(signals),
                    A_index=prev_index,
                    A_time=times[prev_index],
                    A_price=closes[prev_index],
                    B_index=b_index,
                    B_time=times[b_index],
                    B_price=closes[b_index],
                    golden_A_index=prev_index,
                    golden_A_time=times[prev_index],
                    golden_A_price=closes[prev_index],
                    golden_B_index=curr_index,
                    golden_B_time=times[curr_index],
                    golden_B_price=closes[curr_index],
                    AB_macd_fast_max=ab_macd_fast_max,
                    macd_A_index=prev_extremum_index,
                    macd_A_time=times[prev_extremum_index],
                    macd_A_value=prev_extremum,
                    macd_B_index=curr_extremum_index,
                    macd_B_time=times[curr_extremum_index],
                    macd_B_value=curr_extremum,
                )
            )
    return signals


def future_high_confirmed(rows: list[Row], i: int) -> bool:
    return i + 3 < len(rows) and rows[i].close > max(r.close for r in rows[i + 1 : i + 4])


def future_low_confirmed(rows: list[Row], i: int) -> bool:
    return i + 3 < len(rows) and rows[i].close < min(r.close for r in rows[i + 1 : i + 4])


def evaluate_ad_structure(rows: list[Row], sig: ABSignal) -> ADStructure:
    st = ADStructure(
        symbol=sig.symbol,
        timeframe=sig.timeframe,
        version="A-D_structure_v0.5",
        A_index=sig.A_index,
        A_time=sig.A_time,
        A_price=sig.A_price,
        B_index=sig.B_index,
        B_time=sig.B_time,
        B_price=sig.B_price,
        AB_macd_fast_max=sig.AB_macd_fast_max,
        AB_macd_fast_max_rule="max(DIF[A_index:B_index+1]) <= 0",
        C_sequence=[],
    )

    if sig.golden_B_index <= sig.golden_A_index:
        return st

    bm_range = range(sig.golden_A_index, sig.golden_B_index)
    bm_index = max(bm_range, key=lambda idx: rows[idx].close)
    st.BM_index = bm_index
    st.BM_time = rows[bm_index].datetime
    st.BM_price = rows[bm_index].close

    fail_level = sig.B_price * 0.95

    def mark_failure(i: int) -> ADStructure:
        st.structure_status = "STRUCTURE_FAILED"
        st.failure_type = "DROP_BELOW_B_95"
        st.failure_index = i
        st.failure_time = rows[i].datetime
        st.failure_price = rows[i].close
        return st

    break_index = None
    for i in range(sig.B_index + 1, len(rows)):
        if rows[i].close < fail_level:
            return mark_failure(i)
        if rows[i].close > st.BM_price:
            break_index = i
            break
    if break_index is None:
        st.structure_status = "NO_BM_BREAK"
        return st
    st.BM_break_index = break_index
    st.BM_break_time = rows[break_index].datetime
    st.BM_break_price = rows[break_index].close

    cm_index = None
    for i in range(break_index + 1, len(rows)):
        if rows[i].close < fail_level:
            return mark_failure(i)
        if future_high_confirmed(rows, i):
            cm_index = i
            break
    if cm_index is None:
        st.structure_status = "WAITING_FOR_CM"
        return st
    st.CM_index = cm_index
    st.CM_time = rows[cm_index].datetime
    st.CM_price = rows[cm_index].close

    mode: Literal["WAIT_C", "WAIT_H"] = "WAIT_C"
    current_c: dict | None = None
    c_number = 1
    for i in range(cm_index + 1, len(rows)):
        if rows[i].close < fail_level:
            return mark_failure(i)
        if rows[i].close > st.CM_price:
            st.D_index = i
            st.D_time = rows[i].datetime
            st.D_price = rows[i].close
            st.structure_status = "D_TRIGGERED"
            st.signal_type = "D_ALERT"
            return st

        if mode == "WAIT_C" and future_low_confirmed(rows, i):
            current_c = {
                "label": f"C{c_number}",
                "index": i,
                "time": rows[i].datetime,
                "price": rows[i].close,
                "confirm_rule": "close[i] < min(close[i+1:i+4])",
                "rebound_high_label": f"C{c_number}H",
                "rebound_high_index": None,
                "rebound_high_time": None,
                "rebound_high_price": None,
                "rebound_high_confirm_rule": "close[i] > max(close[i+1:i+4])",
            }
            st.C_sequence.append(current_c)
            mode = "WAIT_H"
            continue

        if mode == "WAIT_H" and current_c is not None and future_high_confirmed(rows, i):
            current_c["rebound_high_index"] = i
            current_c["rebound_high_time"] = rows[i].datetime
            current_c["rebound_high_price"] = rows[i].close
            c_number += 1
            mode = "WAIT_C"

    st.structure_status = "WAITING_FOR_D"
    return st


def iter_data_files(data_dir: Path, timeframes: Iterable[str] = TIMEFRAMES) -> Iterable[tuple[str, str, Path]]:
    for timeframe in timeframes:
        suffix = f"_{timeframe}_indicators.csv"
        folder = data_dir / timeframe
        if not folder.exists():
            continue
        for path in sorted(folder.glob(f"*{suffix}")):
            symbol = path.name[: -len(suffix)]
            yield symbol, timeframe, path


def flatten_record(sig: ABSignal, st: ADStructure) -> dict[str, str]:
    row = {
        "symbol": sig.symbol,
        "timeframe": sig.timeframe,
        "data_file": sig.data_file,
        "signal_index": str(sig.signal_index),
        "A_index": str(sig.A_index),
        "A_time": sig.A_time,
        "A_price": fmt(sig.A_price),
        "B_index": str(sig.B_index),
        "B_time": sig.B_time,
        "B_price": fmt(sig.B_price),
        "golden_A_index": str(sig.golden_A_index),
        "golden_A_time": sig.golden_A_time,
        "golden_A_price": fmt(sig.golden_A_price),
        "golden_B_index": str(sig.golden_B_index),
        "golden_B_time": sig.golden_B_time,
        "golden_B_price": fmt(sig.golden_B_price),
        "macd_A_index": str(sig.macd_A_index),
        "macd_A_time": sig.macd_A_time,
        "macd_A_value": fmt(sig.macd_A_value),
        "macd_B_index": str(sig.macd_B_index),
        "macd_B_time": sig.macd_B_time,
        "macd_B_value": fmt(sig.macd_B_value),
        "AB_macd_fast_max": fmt(sig.AB_macd_fast_max),
    }
    data = asdict(st)
    data["C_sequence"] = json.dumps(data["C_sequence"] or [], ensure_ascii=False)
    row.update({key: fmt(value) for key, value in data.items() if key not in row})
    return row


def scan(data_dir: Path, status_filter: str = STATUS_ALL) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for symbol, timeframe, path in iter_data_files(data_dir):
        rows = load_rows(path)
        for sig in detect_ab_signals(symbol, timeframe, path, rows):
            st = evaluate_ad_structure(rows, sig)
            if status_filter == STATUS_ALL or st.structure_status == status_filter:
                out.append(flatten_record(sig, st))
    return out


def write_csv(rows: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol",
        "timeframe",
        "data_file",
        "signal_index",
        "version",
        "A_index",
        "A_time",
        "A_price",
        "B_index",
        "B_time",
        "B_price",
        "golden_A_index",
        "golden_A_time",
        "golden_A_price",
        "golden_B_index",
        "golden_B_time",
        "golden_B_price",
        "macd_A_index",
        "macd_A_time",
        "macd_A_value",
        "macd_B_index",
        "macd_B_time",
        "macd_B_value",
        "AB_macd_fast_max",
        "BM_index",
        "BM_time",
        "BM_price",
        "BM_break_index",
        "BM_break_time",
        "BM_break_price",
        "CM_index",
        "CM_time",
        "CM_price",
        "C_sequence",
        "D_index",
        "D_time",
        "D_price",
        "structure_status",
        "signal_type",
        "failure_type",
        "failure_index",
        "failure_time",
        "failure_price",
    ]
    extra = sorted({key for row in rows for key in row if key not in fields})
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields + extra)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan local CSV files for A-D_structure_v0.5.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Directory containing timeframe folders such as 4h/ and 1day/.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/ad_structure_v05_index.csv"),
        help="CSV output path.",
    )
    parser.add_argument(
        "--status",
        choices=[STATUS_ALL, STATUS_COMPLETE],
        default=STATUS_ALL,
        help="Use D_TRIGGERED to export only complete D_ALERT structures.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = scan(args.data_dir, args.status)
    write_csv(rows, args.output)
    status_counts: dict[str, int] = {}
    for row in rows:
        status = row.get("structure_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
    print(f"scanned_rows={len(rows)} output={args.output}")
    for status, count in sorted(status_counts.items()):
        print(f"{status or 'UNKNOWN'}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
