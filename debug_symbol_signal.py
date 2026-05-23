"""Debug why one symbol does or does not produce bottom-divergence signals."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ad_structure_v05_core import (
    Row,
    detect_ab_signals,
    ema,
    evaluate_ad_structure,
    flatten_record,
    has_large_price_jump,
    load_rows,
)


DEFAULT_RUNTIME_ROOT = Path(os.environ.get("UPBOTTOM_RUNTIME_ROOT") or "/data/UpBottom")
DEFAULT_DATASET = os.environ.get("UPBOTTOM_DATASET", "stocks_2025_10")


def safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "-").replace("/", "_")


def find_crosses(rows: list[Row]) -> tuple[list[float], list[float], list[dict]]:
    closes = [row.close for row in rows]
    dif = [fast - slow for fast, slow in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    crosses: list[dict] = []
    for i in range(1, len(closes)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            crosses.append({"type": "golden_cross", "index": i})
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            crosses.append({"type": "death_cross", "index": i})
    return dif, dea, crosses


def crosses_with_extrema(dif: list[float], crosses: list[dict]) -> list[dict]:
    out: list[dict] = []
    for idx, cross in enumerate(crosses):
        opposite = "death_cross" if cross["type"] == "golden_cross" else "golden_cross"
        previous_opposite = None
        for j in range(idx - 1, -1, -1):
            if crosses[j]["type"] == opposite:
                previous_opposite = crosses[j]
                break
        item = {**cross, "extremum_value": None, "extremum_index": None}
        if previous_opposite is not None:
            start = int(previous_opposite["index"])
            end = int(cross["index"]) + 1
            if cross["type"] == "golden_cross":
                extremum_index = min(range(start, end), key=lambda k: dif[k])
            else:
                extremum_index = max(range(start, end), key=lambda k: dif[k])
            item["extremum_index"] = extremum_index
            item["extremum_value"] = dif[extremum_index]
        out.append(item)
    return out


def fmt_bool(value: bool) -> str:
    return "OK" if value else "FAIL"


def debug_timeframe(symbol: str, timeframe: str, path: Path, min_date: str) -> None:
    rows = load_rows(path, min_date=min_date)
    print(f"\n=== {symbol} {timeframe} ===")
    print(f"file={path}")
    print(f"rows={len(rows)} min_date={min_date}")
    if rows:
        print(f"range={rows[0].datetime} -> {rows[-1].datetime}")
    if len(rows) < 60:
        print("not_enough_rows_for_macd_signal")
        return

    dif, _dea, crosses = find_crosses(rows)
    enriched = crosses_with_extrema(dif, crosses)
    valid_golden = [
        item
        for item in enriched
        if item["type"] == "golden_cross" and item["extremum_value"] is not None
    ]
    print(f"crosses={len(crosses)} valid_golden_crosses={len(valid_golden)}")
    closes = [row.close for row in rows]

    for n, item in enumerate(valid_golden, start=1):
        idx = int(item["index"])
        ext_idx = int(item["extremum_index"])
        print(
            f"G{n}: idx={idx} time={rows[idx].datetime} close={closes[idx]:.6g} "
            f"dif={dif[idx]:.6g} min_dif={float(item['extremum_value']):.6g} "
            f"min_time={rows[ext_idx].datetime}"
        )

    print("\nPair checks:")
    for i in range(1, len(valid_golden)):
        prev = valid_golden[i - 1]
        curr = valid_golden[i]
        prev_index = int(prev["index"])
        curr_index = int(curr["index"])
        prev_ext = float(prev["extremum_value"])
        curr_ext = float(curr["extremum_value"])
        ab_max = max(dif[prev_index : curr_index + 1])
        jump = has_large_price_jump(closes, max(0, prev_index - 35), curr_index)
        price_rule = closes[curr_index] < closes[prev_index] * 0.95
        dif_lift_rule = curr_ext - prev_ext > abs(prev_ext) * 0.05
        curr_dif_rule = dif[curr_index] < 0
        ab_max_rule = ab_max <= 0
        all_ok = (not jump) and price_rule and dif_lift_rule and curr_dif_rule and ab_max_rule
        print(
            f"G{i}->G{i + 1}: {rows[prev_index].datetime} -> {rows[curr_index].datetime} "
            f"overall={fmt_bool(all_ok)}"
        )
        print(
            f"  price_drop {fmt_bool(price_rule)}: "
            f"{closes[curr_index]:.6g} < {closes[prev_index] * 0.95:.6g} "
            f"(curr={closes[curr_index]:.6g}, prev={closes[prev_index]:.6g})"
        )
        print(
            f"  dif_lift   {fmt_bool(dif_lift_rule)}: "
            f"{curr_ext - prev_ext:.6g} > {abs(prev_ext) * 0.05:.6g} "
            f"(curr_ext={curr_ext:.6g}, prev_ext={prev_ext:.6g})"
        )
        print(f"  curr_dif<0 {fmt_bool(curr_dif_rule)}: dif[curr]={dif[curr_index]:.6g}")
        print(f"  ab_max<=0  {fmt_bool(ab_max_rule)}: ab_max={ab_max:.6g}")
        print(f"  split_jump {fmt_bool(not jump)}: jump_detected={jump}")

    signals = detect_ab_signals(symbol, timeframe, path, rows)
    print(f"\naccepted_signals={len(signals)}")
    for sig in signals:
        st = evaluate_ad_structure(rows, sig)
        row = flatten_record(sig, st)
        fields = [
            "structure_status",
            "failure_type",
            "golden_A_time",
            "golden_B_time",
            "B_time",
            "B_price",
            "BM_time",
            "BM_price",
            "BM_break_time",
            "BM_break_price",
            "CM_time",
            "CM_price",
            "D_time",
            "D_price",
            "failure_time",
            "failure_price",
            "failure_rule",
        ]
        for field in fields:
            print(f"  {field}={row.get(field, '')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug one symbol's bottom-divergence scan.")
    parser.add_argument("symbol", help="Safe symbol used in local files, for example HPQ.")
    parser.add_argument("--timeframe", choices=["1day", "4h", "all"], default="all")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--start", default="2025-10-01")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbol = safe_symbol(args.symbol.strip().upper())
    timeframes = ["1day", "4h"] if args.timeframe == "all" else [args.timeframe]
    data_root = args.runtime_root / "data" / args.dataset
    for timeframe in timeframes:
        path = data_root / timeframe / f"{symbol}_{timeframe}_indicators.csv"
        if not path.exists():
            print(f"\n=== {symbol} {timeframe} ===")
            print(f"missing_file={path}")
            continue
        debug_timeframe(symbol, timeframe, path, args.start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
