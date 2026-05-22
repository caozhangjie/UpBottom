"""Segment OHLC data into simple up/down/range market-structure states.

This is an exploratory, explainable first pass for strategy research. It reads
existing OHLC CSV files produced by fetch_sp500_2026_and_mark.py and renders a
manual-validation candlestick chart with state bands.
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path

from ad_structure_v05_core import Row, load_rows
from fetch_sp500_2026_and_mark import DATASET_NAME, RUNTIME_ROOT, configure_chinese_chart_font, safe_symbol


STATE_UP = "连续上涨"
STATE_DOWN = "连续下跌"
STATE_RANGE = "震荡"


@dataclass(frozen=True)
class Segment:
    state: str
    start_index: int
    end_index: int
    start_time: str
    end_time: str
    start_close: float
    end_close: float


def true_ranges(rows: list[Row]) -> list[float]:
    if not rows:
        return []
    out = [rows[0].high - rows[0].low]
    for i in range(1, len(rows)):
        prev_close = rows[i - 1].close
        out.append(
            max(
                rows[i].high - rows[i].low,
                abs(rows[i].high - prev_close),
                abs(rows[i].low - prev_close),
            )
        )
    return out


def rolling_mean(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= period:
            total -= values[i - period]
        count = min(i + 1, period)
        out.append(total / count if count else 0.0)
    return out


def trend_efficiency(rows: list[Row], start: int, end: int) -> float:
    net = abs(rows[end].close - rows[start].close)
    path = sum(abs(rows[i].close - rows[i - 1].close) for i in range(start + 1, end + 1))
    if path <= 0:
        return 0.0
    return net / path


def classify_states(
    rows: list[Row],
    lookback: int = 5,
    atr_period: int = 14,
    trend_atr: float = 1.4,
    min_efficiency: float = 0.45,
) -> list[str]:
    if not rows:
        return []
    atr = rolling_mean(true_ranges(rows), atr_period)
    states: list[str] = [STATE_RANGE for _ in rows]
    for i in range(len(rows)):
        if i < lookback:
            continue
        start = i - lookback
        atr_value = max(atr[i], rows[i].close * 0.001, 1e-9)
        window = range(start, i + 1)
        low_index = min(window, key=lambda idx: rows[idx].close)
        high_index = max(window, key=lambda idx: rows[idx].close)
        up_move = rows[i].close - rows[low_index].close
        down_move = rows[high_index].close - rows[i].close
        up_efficiency = trend_efficiency(rows, low_index, i) if low_index < i else 0.0
        down_efficiency = trend_efficiency(rows, high_index, i) if high_index < i else 0.0
        if up_move >= trend_atr * atr_value and up_efficiency >= min_efficiency:
            for j in range(low_index, i + 1):
                states[j] = STATE_UP
        elif down_move >= trend_atr * atr_value and down_efficiency >= min_efficiency:
            for j in range(high_index, i + 1):
                states[j] = STATE_DOWN
    return smooth_short_flips(states)


def smooth_short_flips(states: list[str], min_len: int = 2) -> list[str]:
    if not states:
        return states
    out = states[:]
    start = 0
    while start < len(out):
        end = start
        while end + 1 < len(out) and out[end + 1] == out[start]:
            end += 1
        if end - start + 1 < min_len:
            prev_state = out[start - 1] if start > 0 else None
            next_state = out[end + 1] if end + 1 < len(out) else None
            replacement = prev_state if prev_state == next_state and prev_state else STATE_RANGE
            for i in range(start, end + 1):
                out[i] = replacement
        start = end + 1
    return out


def merge_segments(rows: list[Row], states: list[str]) -> list[Segment]:
    if not rows:
        return []
    segments: list[Segment] = []
    start = 0
    for i in range(1, len(rows) + 1):
        if i == len(rows) or states[i] != states[start]:
            segments.append(
                Segment(
                    state=states[start],
                    start_index=start,
                    end_index=i - 1,
                    start_time=rows[start].datetime,
                    end_time=rows[i - 1].datetime,
                    start_close=rows[start].close,
                    end_close=rows[i - 1].close,
                )
            )
            start = i
    return segments


def write_segments_csv(segments: list[Segment], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "state",
                "start_index",
                "end_index",
                "start_time",
                "end_time",
                "start_close",
                "end_close",
            ],
        )
        writer.writeheader()
        for segment in segments:
            writer.writerow(
                {
                    "state": segment.state,
                    "start_index": segment.start_index,
                    "end_index": segment.end_index,
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "start_close": f"{segment.start_close:.6f}",
                    "end_close": f"{segment.end_close:.6f}",
                }
            )


def render_segments_chart(rows: list[Row], segments: list[Segment], output_path: Path, title: str) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ModuleNotFoundError:
        svg_path = output_path.with_suffix(".svg")
        render_segments_svg(rows, segments, svg_path, title)
        return svg_path

    configure_chinese_chart_font(plt)
    fig, ax = plt.subplots(figsize=(18, 9), dpi=140)
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#ffffff")

    colors = {
        STATE_UP: "#dcfce7",
        STATE_DOWN: "#fee2e2",
        STATE_RANGE: "#e0f2fe",
    }
    label_colors = {
        STATE_UP: "#15803d",
        STATE_DOWN: "#b91c1c",
        STATE_RANGE: "#0369a1",
    }

    for segment in segments:
        ax.axvspan(segment.start_index - 0.5, segment.end_index + 0.5, color=colors[segment.state], alpha=0.55, zorder=0)
        if segment.end_index - segment.start_index >= 3:
            y = max(row.high for row in rows[segment.start_index : segment.end_index + 1])
            ax.text(
                (segment.start_index + segment.end_index) / 2,
                y,
                segment.state,
                ha="center",
                va="bottom",
                color=label_colors[segment.state],
                fontsize=9,
                fontweight="bold",
                zorder=4,
            )
        if segment.state == STATE_UP:
            ax.scatter(segment.start_index, segment.start_close, s=38, color="#16a34a", edgecolor="#111827", linewidth=0.6, zorder=5)
            ax.annotate(
                "上涨起点",
                (segment.start_index, segment.start_close),
                xytext=(0, -18),
                textcoords="offset points",
                ha="center",
                va="top",
                color="#15803d",
                fontsize=8,
                fontweight="bold",
                zorder=6,
            )

    candle_width = 0.58
    for x, row in enumerate(rows):
        up = row.close >= row.open
        color = "#16a34a" if up else "#dc2626"
        ax.vlines(x, row.low, row.high, color=color, linewidth=0.85, zorder=2)
        body_low = min(row.open, row.close)
        body_height = max(abs(row.close - row.open), max(row.close * 0.0008, 0.01))
        ax.add_patch(
            Rectangle(
                (x - candle_width / 2, body_low),
                candle_width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.7,
                alpha=0.25 if up else 0.88,
                zorder=3,
            )
        )

    tick_step = max(1, len(rows) // 10)
    ticks = list(range(0, len(rows), tick_step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([rows[i].datetime[:10] for i in ticks], rotation=0, fontsize=8)
    ax.set_title(title, loc="left", fontsize=13, color="#0f172a")
    ax.set_ylabel("Price")
    ax.grid(True, color="#e2e8f0", linewidth=0.7)
    ax.margins(x=0.01, y=0.08)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def render_segments_svg(rows: list[Row], segments: list[Segment], output_path: Path, title: str) -> None:
    width = 1800
    height = 900
    left = 70
    right = 30
    top = 70
    bottom = 80
    plot_w = width - left - right
    plot_h = height - top - bottom
    hi = max(row.high for row in rows)
    lo = min(row.low for row in rows)
    pad = max((hi - lo) * 0.08, hi * 0.01)
    hi += pad
    lo -= pad

    def x_for_index(index: int) -> float:
        if len(rows) <= 1:
            return left + plot_w / 2
        return left + index / (len(rows) - 1) * plot_w

    def y_for_price(price: float) -> float:
        if hi <= lo:
            return top + plot_h / 2
        return top + (hi - price) / (hi - lo) * plot_h

    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    bg = {
        STATE_UP: "#dcfce7",
        STATE_DOWN: "#fee2e2",
        STATE_RANGE: "#e0f2fe",
    }
    label_colors = {
        STATE_UP: "#15803d",
        STATE_DOWN: "#b91c1c",
        STATE_RANGE: "#0369a1",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" fill="#ffffff"/>',
        f'<text x="{left}" y="35" fill="#0f172a" font-size="22" font-family="Noto Sans CJK SC, Arial">{esc(title)}</text>',
    ]
    for segment in segments:
        x1 = x_for_index(segment.start_index) - 4
        x2 = x_for_index(segment.end_index) + 4
        parts.append(
            f'<rect x="{x1:.1f}" y="{top}" width="{max(x2 - x1, 1):.1f}" height="{plot_h}" '
            f'fill="{bg[segment.state]}" opacity="0.55"/>'
        )
        if segment.end_index - segment.start_index >= 3:
            y = min(y_for_price(row.high) for row in rows[segment.start_index : segment.end_index + 1]) - 6
            parts.append(
                f'<text x="{(x1 + x2) / 2:.1f}" y="{max(y, 18):.1f}" fill="{label_colors[segment.state]}" '
                f'font-size="15" font-weight="700" text-anchor="middle" font-family="Noto Sans CJK SC, Arial">{segment.state}</text>'
            )
        if segment.state == STATE_UP:
            x = x_for_index(segment.start_index)
            y = y_for_price(segment.start_close)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#16a34a" stroke="#111827"/>')
            parts.append(
                f'<text x="{x:.1f}" y="{y + 20:.1f}" fill="#15803d" font-size="13" font-weight="700" '
                f'text-anchor="middle" font-family="Noto Sans CJK SC, Arial">上涨起点</text>'
            )

    candle_w = max(2, min(7, plot_w / max(len(rows), 1) * 0.62))
    for idx, row in enumerate(rows):
        x = x_for_index(idx)
        up = row.close >= row.open
        color = "#16a34a" if up else "#dc2626"
        high_y = y_for_price(row.high)
        low_y = y_for_price(row.low)
        open_y = y_for_price(row.open)
        close_y = y_for_price(row.close)
        body_y = min(open_y, close_y)
        body_h = max(abs(open_y - close_y), 1.5)
        opacity = "0.32" if up else "0.88"
        parts.append(f'<line x1="{x:.1f}" y1="{high_y:.1f}" x2="{x:.1f}" y2="{low_y:.1f}" stroke="{color}" stroke-width="1"/>')
        parts.append(
            f'<rect x="{x - candle_w / 2:.1f}" y="{body_y:.1f}" width="{candle_w:.1f}" height="{body_h:.1f}" '
            f'fill="{color}" stroke="{color}" opacity="{opacity}"/>'
        )

    tick_step = max(1, len(rows) // 10)
    for i in range(0, len(rows), tick_step):
        x = x_for_index(i)
        parts.append(f'<line x1="{x:.1f}" y1="{top + plot_h}" x2="{x:.1f}" y2="{top + plot_h + 5}" stroke="#64748b"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 24}" fill="#334155" font-size="12" text-anchor="middle" '
            f'font-family="Arial">{rows[i].datetime[:10]}</text>'
        )
    for frac in [0, 0.25, 0.5, 0.75, 1]:
        price = hi - (hi - lo) * frac
        y = y_for_price(price)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" fill="#334155" font-size="12" text-anchor="end" font-family="Arial">{price:.2f}</text>')
    parts.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")


def default_data_file(symbol: str, timeframe: str) -> Path:
    safe = safe_symbol(symbol)
    return RUNTIME_ROOT / "data" / DATASET_NAME / timeframe / f"{safe}_{timeframe}_indicators.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Segment OHLC data into up/down/range states.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", choices=["1day", "4h"], required=True)
    parser.add_argument("--data-file", type=Path, default=None)
    parser.add_argument("--min-date", default="2025-01-01")
    parser.add_argument("--lookback", type=int, default=5)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--trend-atr", type=float, default=1.4)
    parser.add_argument("--min-efficiency", type=float, default=0.45)
    parser.add_argument("--output-dir", type=Path, default=RUNTIME_ROOT / "outputs" / DATASET_NAME / "segments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_file = args.data_file or default_data_file(args.symbol, args.timeframe)
    rows = load_rows(data_file, min_date=args.min_date)
    if not rows:
        raise SystemExit(f"No rows loaded from {data_file}")
    states = classify_states(rows, args.lookback, args.atr_period, args.trend_atr, args.min_efficiency)
    segments = merge_segments(rows, states)
    safe = safe_symbol(args.symbol)
    trend_token = str(args.trend_atr).replace(".", "p")
    efficiency_token = str(args.min_efficiency).replace(".", "p")
    prefix = f"{safe}_{args.timeframe}_{args.min_date}_lb{args.lookback}_atr{trend_token}_eff{efficiency_token}"
    csv_path = args.output_dir / f"{prefix}_segments.csv"
    png_path = args.output_dir / f"{prefix}_segments.png"
    write_segments_csv(segments, csv_path)
    chart_path = render_segments_chart(
        rows,
        segments,
        png_path,
        f"{safe} {args.timeframe} market structure | lookback={args.lookback} trend_atr={args.trend_atr}",
    )
    counts: dict[str, int] = {}
    for segment in segments:
        counts[segment.state] = counts.get(segment.state, 0) + 1
    print(f"rows={len(rows)} segments={len(segments)} csv={csv_path} chart={chart_path}")
    for state, count in sorted(counts.items()):
        print(f"{state}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
