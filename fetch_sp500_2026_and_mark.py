"""Fetch 2026 S&P 500 data, scan A-D bottom divergences, and mark charts.

Outputs:
- data/sp500_2026/{1day,4h}/{SYMBOL}_{timeframe}_indicators.csv
- outputs/sp500_2026/ad_signals.csv
- outputs/sp500_2026/charts/*.svg

Only the Python standard library is used for fetching and drawing. Market data
can come from Yahoo Finance's chart endpoint or Twelve Data's time_series
endpoint. Yahoo 4h is built from 1h bars; Twelve Data 4h is fetched directly.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, time as dtime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from ad_structure_v05_core import (
    ABSignal,
    ADStructure,
    Row,
    detect_ab_signals,
    evaluate_ad_structure,
    flatten_record,
    load_rows,
    write_csv,
)

try:
    from credentials import TWELVE_DATA_API_KEY as CREDENTIALS_TWELVE_DATA_API_KEY
except ImportError:
    CREDENTIALS_TWELVE_DATA_API_KEY = None


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "sp500_2026"
OUTPUT_ROOT = ROOT / "outputs" / "sp500_2026"
CHART_ROOT = OUTPUT_ROOT / "charts"
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TWELVE_DATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
START_DATE = "2026-01-01"
TIMEFRAMES = ("1day", "4h")
EASTERN = ZoneInfo("America/New_York")


class SP500TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.table_done = False
        self.in_cell = False
        self.current_cell: list[str] = []
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if (
            tag == "table"
            and "wikitable" in (attrs_dict.get("class") or "")
            and not self.in_table
            and not self.table_done
        ):
            self.in_table = True
        if self.in_table and tag == "tr":
            self.current_row = []
        if self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.in_table and tag in {"td", "th"}:
            text = " ".join("".join(self.current_cell).split())
            self.current_row.append(text)
            self.in_cell = False
        if self.in_table and tag == "tr" and self.current_row:
            self.rows.append(self.current_row)
        if self.in_table and tag == "table":
            self.in_table = False
            self.table_done = True


def http_get_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_get_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def twelve_data_symbol(symbol: str) -> str:
    return symbol.replace(".", ".")


def safe_symbol(symbol: str) -> str:
    return yahoo_symbol(symbol).replace("/", "_")


def fetch_sp500_symbols(limit: int | None = None) -> list[str]:
    html = http_get_text(SP500_URL)
    parser = SP500TableParser()
    parser.feed(html)
    if not parser.rows:
        raise RuntimeError("Could not parse S&P 500 table from Wikipedia.")
    header = parser.rows[0]
    try:
        symbol_idx = header.index("Symbol")
    except ValueError as exc:
        raise RuntimeError(f"Unexpected S&P 500 table header: {header}") from exc
    symbols = [row[symbol_idx].strip() for row in parser.rows[1:] if len(row) > symbol_idx and row[symbol_idx]]
    return symbols[:limit] if limit else symbols


def unix_seconds(date_text: str) -> int:
    dt = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_yahoo_bars(symbol: str, interval: str, start: str, end: str | None) -> list[Row]:
    period1 = unix_seconds(start)
    period2 = int(time.time()) if end is None else unix_seconds(end)
    query = urllib.parse.urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": interval,
            "events": "history",
            "includePrePost": "false",
        }
    )
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(yahoo_symbol(symbol))) + "?" + query
    payload = http_get_json(url)
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo returned no result for {symbol} {interval}: {error}")
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows: list[Row] = []
    for i, ts in enumerate(timestamps):
        values = [opens, highs, lows, closes]
        if any(i >= len(series) or series[i] is None for series in values):
            continue
        local_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(EASTERN)
        rows.append(
            Row(
                datetime=local_dt.strftime("%Y-%m-%d %H:%M:%S"),
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i] or 0) if i < len(volumes) else 0.0,
            )
        )
    return rows


def parse_twelve_data_datetime(value: str) -> str:
    text = value.strip()
    if len(text) == 10:
        return f"{text} 09:30:00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return text


def fetch_twelve_data_bars(
    symbol: str,
    interval: str,
    start: str,
    end: str | None,
    api_key: str,
) -> list[Row]:
    params = {
        "symbol": twelve_data_symbol(symbol),
        "interval": interval,
        "start_date": start,
        "apikey": api_key,
        "order": "ASC",
        "timezone": "America/New_York",
    }
    if end:
        params["end_date"] = end
    url = TWELVE_DATA_TIME_SERIES_URL + "?" + urllib.parse.urlencode(params)
    payload = http_get_json(url)
    if payload.get("status") == "error":
        raise RuntimeError(payload.get("message") or payload)
    values = payload.get("values") or []
    rows: list[Row] = []
    for item in values:
        try:
            rows.append(
                Row(
                    datetime=parse_twelve_data_datetime(str(item.get("datetime") or "")),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def resample_1h_to_4h(rows: list[Row]) -> list[Row]:
    buckets: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        dt = datetime.strptime(row.datetime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
        if dt.time() < dtime(9, 30) or dt.time() > dtime(16, 0):
            continue
        bucket_time = "09:30:00" if dt.time() < dtime(13, 30) else "13:30:00"
        buckets.setdefault((dt.strftime("%Y-%m-%d"), bucket_time), []).append(row)

    out: list[Row] = []
    for (date_text, bucket_time), group in sorted(buckets.items()):
        group = sorted(group, key=lambda item: item.datetime)
        out.append(
            Row(
                datetime=f"{date_text} {bucket_time}",
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
            )
        )
    return out


def write_rows(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["datetime", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def fetch_symbol(
    symbol: str,
    start: str,
    end: str | None,
    provider: str,
    twelve_data_api_key: str | None,
) -> tuple[str, bool, str]:
    try:
        if provider == "twelve-data":
            if not twelve_data_api_key:
                raise RuntimeError("Missing Twelve Data API key. Set TWELVE_DATA_API_KEY or pass --apikey.")
            daily = fetch_twelve_data_bars(symbol, "1day", start, end, twelve_data_api_key)
            four_hour = fetch_twelve_data_bars(symbol, "4h", start, end, twelve_data_api_key)
        else:
            daily = fetch_yahoo_bars(symbol, "1d", start, end)
            hourly = fetch_yahoo_bars(symbol, "1h", start, end)
            four_hour = resample_1h_to_4h(hourly)
        file_symbol = safe_symbol(symbol)
        write_rows(DATA_ROOT / "1day" / f"{file_symbol}_1day_indicators.csv", daily)
        write_rows(DATA_ROOT / "4h" / f"{file_symbol}_4h_indicators.csv", four_hour)
        return symbol, True, f"1day={len(daily)} 4h={len(four_hour)}"
    except Exception as exc:
        return symbol, False, str(exc)


def data_files() -> Iterable[tuple[str, str, Path]]:
    for timeframe in TIMEFRAMES:
        folder = DATA_ROOT / timeframe
        if not folder.exists():
            continue
        suffix = f"_{timeframe}_indicators.csv"
        for path in sorted(folder.glob(f"*{suffix}")):
            symbol = path.name[: -len(suffix)]
            yield symbol, timeframe, path


def scan_saved_data() -> list[tuple[ABSignal, ADStructure, list[Row]]]:
    out: list[tuple[ABSignal, ADStructure, list[Row]]] = []
    for symbol, timeframe, path in data_files():
        rows = load_rows(path, min_date=START_DATE)
        for sig in detect_ab_signals(symbol, timeframe, path, rows):
            out.append((sig, evaluate_ad_structure(rows, sig), rows))
    return out


def price_to_y(price: float, lo: float, hi: float, top: float, bottom: float) -> float:
    if hi <= lo:
        return (top + bottom) / 2
    return bottom - ((price - lo) / (hi - lo)) * (bottom - top)


def x_for_index(index: int, start: int, end: int, left: float, right: float) -> float:
    if end <= start:
        return (left + right) / 2
    return left + ((index - start) / (end - start)) * (right - left)


def svg_text(x: float, y: float, text: str, fill: str = "#111827", size: int = 12) -> str:
    return f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" font-family="Arial">{escape_xml(text)}</text>'


def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def draw_marker(parts: list[str], x: float, y: float, label: str, color: str) -> None:
    parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="#111827" stroke-width="1"/>')
    parts.append(svg_text(x + 7, y - 7, label, color, 12))


def render_svg(sig: ABSignal, st: ADStructure, rows: list[Row], chart_path: Path) -> None:
    candidates = [
        sig.golden_A_index,
        sig.golden_B_index,
        sig.B_index,
        st.BM_index,
        st.BM_break_index,
        st.CM_index,
        st.D_index,
        st.failure_index,
    ]
    for c in st.C_sequence or []:
        candidates.extend([c.get("index"), c.get("rebound_high_index")])
    valid_candidates = [int(v) for v in candidates if v is not None]
    start = max(0, min(valid_candidates) - 25)
    end = min(len(rows) - 1, max(valid_candidates) + 35)
    window = rows[start : end + 1]
    if not window:
        return

    width, height = 1180, 640
    left, right, top, bottom = 70, width - 35, 55, height - 80
    prices = [row.close for row in window]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.08 or max(hi * 0.02, 1)
    lo -= pad
    hi += pad
    points = [
        f"{x_for_index(i, start, end, left, right):.1f},{price_to_y(rows[i].close, lo, hi, top, bottom):.1f}"
        for i in range(start, end + 1)
    ]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<rect x="{left}" y="{top}" width="{right-left}" height="{bottom-top}" fill="#ffffff" stroke="#cbd5e1"/>',
        svg_text(24, 28, f"{sig.symbol} {sig.timeframe} bottom divergence | {st.structure_status}", "#0f172a", 18),
        svg_text(24, 48, f"B={sig.B_time} BM={st.BM_time or '-'} CM={st.CM_time or '-'} D={st.D_time or '-'}", "#475569", 12),
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#2563eb" stroke-width="2"/>',
    ]

    for frac in (0, 0.25, 0.5, 0.75, 1):
        y = top + (bottom - top) * frac
        price = hi - (hi - lo) * frac
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(svg_text(12, y + 4, f"{price:.2f}", "#64748b", 11))

    def marker(index: int | None, price: float | None, label: str, color: str) -> None:
        if index is None or price is None or index < start or index > end:
            return
        x = x_for_index(index, start, end, left, right)
        y = price_to_y(price, lo, hi, top, bottom)
        draw_marker(parts, x, y, label, color)

    marker(sig.golden_A_index, sig.golden_A_price, "GA", "#64748b")
    marker(sig.golden_B_index, sig.golden_B_price, "GB", "#64748b")
    marker(sig.B_index, sig.B_price, "B", "#dc2626")
    marker(st.BM_index, st.BM_price, "BM", "#ca8a04")
    marker(st.CM_index, st.CM_price, "CM", "#7c3aed")
    marker(st.BM_break_index, st.BM_break_price, "BM Break", "#f97316")
    marker(st.D_index, st.D_price, "D", "#16a34a")
    marker(st.failure_index, st.failure_price, "FAIL", "#991b1b")
    for c in st.C_sequence or []:
        marker(c.get("index"), c.get("price"), c.get("label", "C"), "#0284c7")
        marker(c.get("rebound_high_index"), c.get("rebound_high_price"), c.get("rebound_high_label", "CH"), "#9333ea")

    for idx, row_idx in enumerate([start, end]):
        x = x_for_index(row_idx, start, end, left, right)
        anchor = "start" if idx == 0 else "end"
        parts.append(
            f'<text x="{x:.1f}" y="{height-42}" fill="#64748b" font-size="11" font-family="Arial" text-anchor="{anchor}">'
            f"{escape_xml(rows[row_idx].datetime)}</text>"
        )
    parts.append("</svg>")
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    chart_path.write_text("\n".join(parts), encoding="utf-8")


def scan_and_mark() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    CHART_ROOT.mkdir(parents=True, exist_ok=True)
    for old_chart in CHART_ROOT.glob("*.svg"):
        old_chart.unlink()
    for ordinal, (sig, st, rows) in enumerate(scan_saved_data(), start=1):
        chart_name = f"{ordinal:05d}_{safe_symbol(sig.symbol)}_{sig.timeframe}_{st.structure_status}.svg"
        chart_path = CHART_ROOT / chart_name
        render_svg(sig, st, rows, chart_path)
        record = flatten_record(sig, st)
        record["chart_file"] = str(chart_path.relative_to(ROOT))
        records.append(record)
    write_csv(records, OUTPUT_ROOT / "ad_signals.csv")
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch S&P 500 2026 bars and mark A-D bottom divergences.")
    parser.add_argument(
        "--provider",
        choices=["twelve-data", "yahoo"],
        default="twelve-data",
        help="Market data provider. Twelve Data requires --apikey or TWELVE_DATA_API_KEY.",
    )
    parser.add_argument("--apikey", default=None, help="Twelve Data API key. Defaults to TWELVE_DATA_API_KEY.")
    parser.add_argument("--limit", type=int, default=None, help="Limit symbols for a quick smoke test.")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent download workers.")
    parser.add_argument("--start", default=START_DATE, help="Start date, inclusive, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="End date, exclusive, YYYY-MM-DD. Defaults to now.")
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing local CSV files only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    global START_DATE
    START_DATE = args.start
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    if not args.skip_fetch:
        twelve_data_api_key = args.apikey or os.environ.get("TWELVE_DATA_API_KEY") or CREDENTIALS_TWELVE_DATA_API_KEY
        if args.provider == "twelve-data" and not twelve_data_api_key:
            raise SystemExit("Missing Twelve Data API key. Set TWELVE_DATA_API_KEY or pass --apikey.")
        symbols = fetch_sp500_symbols(args.limit)
        print(f"provider={args.provider} symbols={len(symbols)} start={args.start} end={args.end or 'now'}")
        ok_count = 0
        failures: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_symbol, symbol, args.start, args.end, args.provider, twelve_data_api_key): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol, ok, detail = future.result()
                if ok:
                    ok_count += 1
                    print(f"[OK] {symbol} {detail}")
                else:
                    failures.append((symbol, detail))
                    print(f"[FAIL] {symbol} {detail}")
        with (OUTPUT_ROOT / "fetch_failures.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["symbol", "error"])
            writer.writerows(failures)
        print(f"downloaded={ok_count} failed={len(failures)}")

    records = scan_and_mark()
    status_counts: dict[str, int] = {}
    for record in records:
        status = record.get("structure_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
    print(f"signals={len(records)} index={OUTPUT_ROOT / 'ad_signals.csv'} charts={CHART_ROOT}")
    for status, count in sorted(status_counts.items()):
        print(f"{status or 'UNKNOWN'}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
