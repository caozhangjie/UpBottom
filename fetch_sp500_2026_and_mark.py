"""Fetch stock data, scan A-D bottom divergences, and mark charts.

Outputs:
- data/{dataset}/{1day,4h}/{SYMBOL}_{timeframe}_indicators.csv
- outputs/{dataset}/ad_signals.csv
- outputs/{dataset}/charts/*.png

Market data can come from Yahoo Finance's chart endpoint or Twelve Data's
time_series endpoint. Yahoo 4h is built from 1h bars; Twelve Data 4h is fetched
directly.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Iterable
from zoneinfo import ZoneInfo

from ad_structure_v05_core import (
    ABSignal,
    ADStructure,
    Row,
    detect_ab_signals,
    ema,
    evaluate_ad_structure,
    flatten_record,
    load_rows,
    write_csv,
)

try:
    from credentials import TWELVE_DATA_API_KEY as CREDENTIALS_TWELVE_DATA_API_KEY
except ImportError:
    CREDENTIALS_TWELVE_DATA_API_KEY = None

try:
    from credentials import STOCK_CN_NAMES as CREDENTIALS_STOCK_CN_NAMES
except ImportError:
    CREDENTIALS_STOCK_CN_NAMES = {}


ROOT = Path(os.environ.get("UPBOTTOM_ROOT") or Path(__file__).resolve().parent)
DATASET_NAME = os.environ.get("UPBOTTOM_DATASET", "stocks_2025_10")
DATA_ROOT = ROOT / "data" / DATASET_NAME
OUTPUT_ROOT = ROOT / "outputs" / DATASET_NAME
CHART_ROOT = OUTPUT_ROOT / "charts"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TWELVE_DATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
DEFAULT_SP500_PROXY_ETF = "SPY"
SSGA_HOLDINGS_XLSX_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{symbol}.xlsx"
DATAHUB_SP500_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
START_DATE = "2025-10-01"
TIMEFRAMES = ("1day", "4h")
EASTERN = ZoneInfo("America/New_York")
TWELVE_DATA_MIN_REQUEST_INTERVAL = 0.5
TWELVE_DATA_MAX_ATTEMPTS = 4
_twelve_data_lock = Lock()
_last_twelve_data_request_at = 0.0


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


def http_get_bytes(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def yahoo_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def twelve_data_symbol(symbol: str) -> str:
    return symbol.replace(".", ".")


def safe_symbol(symbol: str) -> str:
    return yahoo_symbol(symbol).replace("/", "_")


def is_us_equity_ticker(symbol: str) -> bool:
    return re.fullmatch(r"[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?", symbol.strip().upper()) is not None


def write_metadata_csv(metadata: dict[str, dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "source_symbol", "english_name", "chinese_name", "sector", "sub_industry"]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for symbol in sorted(metadata):
            writer.writerow(metadata[symbol])


def load_metadata_csv(path: Path, limit: int | None = None) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]
    metadata = {str(row.get("symbol") or ""): row for row in rows if row.get("symbol")}
    for symbol, chinese_name in CREDENTIALS_STOCK_CN_NAMES.items():
        safe = safe_symbol(symbol)
        item = metadata.setdefault(
            safe,
            {
                "symbol": safe,
                "source_symbol": symbol,
                "english_name": "",
                "sector": "",
                "sub_industry": "",
            },
        )
        item["chinese_name"] = str(chinese_name)
    return metadata


def metadata_from_symbols(symbols: list[str]) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for symbol in symbols:
        text = symbol.strip()
        if not text or text.startswith("#"):
            continue
        safe = safe_symbol(text)
        metadata[safe] = {
            "symbol": safe,
            "source_symbol": text,
            "english_name": "",
            "chinese_name": str(CREDENTIALS_STOCK_CN_NAMES.get(text) or CREDENTIALS_STOCK_CN_NAMES.get(safe) or ""),
            "sector": "",
            "sub_industry": "",
        }
    return metadata


def load_symbols_file(path: Path, limit: int | None = None) -> dict[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig").splitlines()
    if not text:
        return {}
    first = text[0].strip().lower()
    if "," not in first:
        symbols = [line.strip().split()[0] for line in text if line.strip() and not line.lstrip().startswith("#")]
        if limit:
            symbols = symbols[:limit]
        return metadata_from_symbols(symbols)

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]
    metadata: dict[str, dict[str, str]] = {}
    for row in rows:
        raw_symbol = str(row.get("source_symbol") or row.get("symbol") or row.get("ticker") or "").strip()
        if not raw_symbol:
            continue
        safe = safe_symbol(str(row.get("symbol") or raw_symbol).strip())
        source_symbol = str(row.get("source_symbol") or raw_symbol).strip()
        metadata[safe] = {
            "symbol": safe,
            "source_symbol": source_symbol,
            "english_name": str(row.get("english_name") or row.get("name") or ""),
            "chinese_name": str(
                row.get("chinese_name")
                or CREDENTIALS_STOCK_CN_NAMES.get(source_symbol)
                or CREDENTIALS_STOCK_CN_NAMES.get(safe)
                or ""
            ),
            "sector": str(row.get("sector") or row.get("industry") or ""),
            "sub_industry": str(row.get("sub_industry") or ""),
        }
    return metadata


def xlsx_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    value = cell.find("a:v", ns)
    if value is None or value.text is None:
        return ""
    text = value.text
    if cell.get("t") == "s":
        return shared_strings[int(text)]
    return text


def read_first_xlsx_sheet_rows(content: bytes) -> list[list[str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", ns):
                shared_strings.append("".join(node.text or "" for node in item.findall(".//a:t", ns)))

        sheet_name = next(name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        sheet = ET.fromstring(zf.read(sheet_name))
        rows: list[list[str]] = []
        for row in sheet.findall(".//a:row", ns):
            values = [xlsx_cell_text(cell, shared_strings) for cell in row.findall("a:c", ns)]
            rows.append(values)
    return rows


def fetch_ssga_holdings_metadata(etf_symbol: str, limit: int | None = None) -> dict[str, dict[str, str]]:
    url = SSGA_HOLDINGS_XLSX_URL.format(symbol=urllib.parse.quote(etf_symbol.lower()))
    rows = read_first_xlsx_sheet_rows(http_get_bytes(url))
    try:
        header_index = next(i for i, row in enumerate(rows) if row[:2] == ["Name", "Ticker"])
    except StopIteration as exc:
        raise RuntimeError(f"Could not find holdings header in State Street {etf_symbol} xlsx.") from exc

    metadata: dict[str, dict[str, str]] = {}
    for row in rows[header_index + 1 :]:
        if len(row) < 2:
            continue
        english_name = row[0].strip()
        source_symbol = row[1].strip()
        if not is_us_equity_ticker(source_symbol):
            continue
        safe = safe_symbol(source_symbol)
        weight = row[4].strip() if len(row) > 4 else ""
        metadata[safe] = {
            "symbol": safe,
            "source_symbol": source_symbol,
            "english_name": english_name,
            "chinese_name": str(
                CREDENTIALS_STOCK_CN_NAMES.get(source_symbol)
                or CREDENTIALS_STOCK_CN_NAMES.get(safe)
                or ""
            ),
            "sector": row[5].strip() if len(row) > 5 and row[5].strip() != "-" else "",
            "sub_industry": f"State Street {etf_symbol.upper()} holding weight={weight}",
        }
        if limit and len(metadata) >= limit:
            break
    if not metadata:
        raise RuntimeError(f"State Street {etf_symbol} xlsx returned no stock holdings.")
    return metadata


def fetch_datahub_sp500_metadata(limit: int | None = None) -> dict[str, dict[str, str]]:
    text = http_get_bytes(DATAHUB_SP500_CSV_URL).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if limit:
        rows = rows[:limit]
    metadata: dict[str, dict[str, str]] = {}
    for row in rows:
        source_symbol = str(row.get("Symbol") or "").strip()
        if not source_symbol:
            continue
        safe = safe_symbol(source_symbol)
        metadata[safe] = {
            "symbol": safe,
            "source_symbol": source_symbol,
            "english_name": str(row.get("Security") or ""),
            "chinese_name": str(
                CREDENTIALS_STOCK_CN_NAMES.get(source_symbol)
                or CREDENTIALS_STOCK_CN_NAMES.get(safe)
                or ""
            ),
            "sector": str(row.get("GICS Sector") or ""),
            "sub_industry": str(row.get("GICS Sub-Industry") or ""),
        }
    if not metadata:
        raise RuntimeError("DataHub S&P 500 CSV returned no symbols.")
    return metadata


def fetch_default_sp500_metadata(proxy_etf: str, limit: int | None = None) -> dict[str, dict[str, str]]:
    try:
        return fetch_ssga_holdings_metadata(proxy_etf, limit)
    except Exception as ssga_exc:
        try:
            return fetch_datahub_sp500_metadata(limit)
        except Exception as datahub_exc:
            raise RuntimeError(
                f"Could not fetch default S&P 500 universe from State Street {proxy_etf} "
                f"or DataHub fallback. State Street error: {ssga_exc}. "
                f"DataHub error: {datahub_exc}."
            ) from datahub_exc


def get_sp500_metadata(
    limit: int | None = None,
    refresh: bool = False,
    proxy_etf: str = DEFAULT_SP500_PROXY_ETF,
) -> dict[str, dict[str, str]]:
    cache_path = OUTPUT_ROOT / "sp500_metadata.csv"
    if not refresh:
        cached = load_metadata_csv(cache_path, limit)
        if cached:
            return cached
    metadata = fetch_default_sp500_metadata(proxy_etf, limit)
    write_metadata_csv(metadata, cache_path)
    return metadata


def fetch_sp500_symbols(limit: int | None = None) -> list[str]:
    metadata = get_sp500_metadata(limit)
    symbols = [item["source_symbol"] for _, item in sorted(metadata.items())]
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

    global _last_twelve_data_request_at
    payload: dict | None = None
    for attempt in range(1, TWELVE_DATA_MAX_ATTEMPTS + 1):
        with _twelve_data_lock:
            wait = TWELVE_DATA_MIN_REQUEST_INTERVAL - (time.monotonic() - _last_twelve_data_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_twelve_data_request_at = time.monotonic()
        try:
            payload = http_get_json(url)
        except (TimeoutError, urllib.error.URLError, http.client.IncompleteRead, ConnectionError) as exc:
            if attempt == TWELVE_DATA_MAX_ATTEMPTS:
                raise
            time.sleep(min(5 * attempt, 20))
            continue
        if payload.get("status") == "error" and "run out of API credits" in str(payload.get("message", "")):
            if attempt == TWELVE_DATA_MAX_ATTEMPTS:
                break
            time.sleep(65)
            continue
        break

    if payload is None:
        raise RuntimeError(f"Twelve Data returned no payload for {symbol} {interval}")
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


def merge_rows(existing: list[Row], incoming: list[Row]) -> list[Row]:
    merged = {row.datetime: row for row in existing}
    for row in incoming:
        merged[row.datetime] = row
    return [merged[key] for key in sorted(merged)]


def incremental_start(rows: list[Row], fallback_start: str, overlap_days: int) -> str:
    if not rows:
        return fallback_start
    try:
        start_date = datetime.fromisoformat(rows[-1].datetime[:10]) - timedelta(days=max(overlap_days, 0))
    except ValueError:
        return rows[-1].datetime[:10]
    return max(fallback_start, start_date.date().isoformat())


def fetch_symbol(
    symbol: str,
    start: str,
    end: str | None,
    provider: str,
    twelve_data_api_key: str | None,
    overlap_days: int,
) -> tuple[str, bool, str]:
    try:
        file_symbol = safe_symbol(symbol)
        daily_path = DATA_ROOT / "1day" / f"{file_symbol}_1day_indicators.csv"
        four_hour_path = DATA_ROOT / "4h" / f"{file_symbol}_4h_indicators.csv"
        if provider == "twelve-data":
            if not twelve_data_api_key:
                raise RuntimeError("Missing Twelve Data API key. Set TWELVE_DATA_API_KEY or pass --apikey.")
            existing_daily = load_rows(daily_path, min_date=start) if daily_path.exists() else []
            existing_four_hour = load_rows(four_hour_path, min_date=start) if four_hour_path.exists() else []
            daily = merge_rows(
                existing_daily,
                fetch_twelve_data_bars(
                    symbol,
                    "1day",
                    incremental_start(existing_daily, start, overlap_days),
                    end,
                    twelve_data_api_key,
                ),
            )
            four_hour = merge_rows(
                existing_four_hour,
                fetch_twelve_data_bars(
                    symbol,
                    "4h",
                    incremental_start(existing_four_hour, start, overlap_days),
                    end,
                    twelve_data_api_key,
                ),
            )
        else:
            daily = fetch_yahoo_bars(symbol, "1d", start, end)
            hourly = fetch_yahoo_bars(symbol, "1h", start, end)
            four_hour = resample_1h_to_4h(hourly)
        write_rows(daily_path, daily)
        write_rows(four_hour_path, four_hour)
        return symbol, True, f"1day={len(daily)} 4h={len(four_hour)}"
    except Exception as exc:
        return symbol, False, str(exc)


def data_files(symbol_filter: set[str] | None = None) -> Iterable[tuple[str, str, Path]]:
    for timeframe in TIMEFRAMES:
        folder = DATA_ROOT / timeframe
        if not folder.exists():
            continue
        suffix = f"_{timeframe}_indicators.csv"
        for path in sorted(folder.glob(f"*{suffix}")):
            symbol = path.name[: -len(suffix)]
            if symbol_filter is not None and symbol not in symbol_filter:
                continue
            yield symbol, timeframe, path


def scan_saved_data(symbol_filter: set[str] | None = None) -> list[tuple[ABSignal, ADStructure, list[Row]]]:
    out: list[tuple[ABSignal, ADStructure, list[Row]]] = []
    for symbol, timeframe, path in data_files(symbol_filter):
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


def value_to_y(value: float, lo: float, hi: float, top: float, bottom: float) -> float:
    if hi <= lo:
        return (top + bottom) / 2
    return bottom - ((value - lo) / (hi - lo)) * (bottom - top)


def render_png(sig: ABSignal, st: ADStructure, rows: list[Row], chart_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

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

    closes = [row.close for row in rows]
    dif = [fast - slow for fast, slow in zip(ema(closes, 12), ema(closes, 26))]
    dea = ema(dif, 9)
    hist = [d - e for d, e in zip(dif, dea)]

    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, (price_ax, macd_ax) = plt.subplots(
        2,
        1,
        figsize=(15, 9),
        dpi=140,
        sharex=True,
        gridspec_kw={"height_ratios": [3.5, 1.25]},
    )
    fig.patch.set_facecolor("#f8fafc")
    price_ax.set_facecolor("#ffffff")
    macd_ax.set_facecolor("#ffffff")

    x_values = list(range(start, end + 1))
    candle_width = 0.56
    for x, row_idx in zip(x_values, range(start, end + 1)):
        row = rows[row_idx]
        up = row.close >= row.open
        color = "#16a34a" if up else "#dc2626"
        price_ax.vlines(x, row.low, row.high, color=color, linewidth=0.9, zorder=2)
        body_low = min(row.open, row.close)
        body_height = max(abs(row.close - row.open), max(row.close * 0.0008, 0.01))
        price_ax.add_patch(
            Rectangle(
                (x - candle_width / 2, body_low),
                candle_width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.8,
                alpha=0.22 if up else 0.88,
                zorder=3,
            )
        )

        hist_color = "#16a34a" if hist[row_idx] >= 0 else "#dc2626"
        macd_ax.bar(x, hist[row_idx], width=candle_width, color=hist_color, alpha=0.32, linewidth=0)

    macd_ax.plot(x_values, dif[start : end + 1], color="#2563eb", linewidth=1.25, label="DIF")
    macd_ax.plot(x_values, dea[start : end + 1], color="#f97316", linewidth=1.15, label="DEA")
    macd_ax.axhline(0, color="#94a3b8", linewidth=0.8)

    def mark_price(index: int | None, price: float | None, label: str, color: str, dy: float = 12) -> None:
        if index is None or price is None or index < start or index > end:
            return
        price_ax.scatter(index, price, s=42, color=color, edgecolor="#111827", linewidth=0.6, zorder=5)
        price_ax.annotate(
            label,
            (index, price),
            xytext=(0, dy),
            textcoords="offset points",
            ha="center",
            va="bottom" if dy >= 0 else "top",
            color=color,
            fontsize=9,
            fontweight="bold",
            zorder=6,
        )

    black = "#111827"
    purple = "#7c3aed"
    yellow = "#facc15"
    blue = "#2563eb"
    mark_price(sig.golden_A_index, sig.golden_A_price, "GA", black)
    mark_price(st.BM_index, st.BM_price, "BM", purple)
    mark_price(sig.B_index, sig.B_price, "B", yellow, dy=-16)
    mark_price(sig.golden_B_index, sig.golden_B_price, "GB", black)
    mark_price(st.BM_break_index, st.BM_break_price, "BM break", blue)
    mark_price(st.CM_index, st.CM_price, "CM", purple)
    c_point = (st.C_sequence or [None])[0]
    if c_point:
        mark_price(c_point.get("index"), c_point.get("price"), "C", yellow, dy=-16)
    mark_price(st.D_index, st.D_price, "D", "#16a34a")
    fail_label = "C fail" if st.failure_type == "C_FAIL" else "B fail" if st.failure_type == "B_FAIL" else "FAIL"
    mark_price(st.failure_index, st.failure_price, fail_label, "#991b1b", dy=-16)

    if start <= sig.macd_A_index <= end and start <= sig.macd_B_index <= end:
        macd_ax.plot(
            [sig.macd_A_index, sig.macd_B_index],
            [sig.macd_A_value, sig.macd_B_value],
            color=purple,
            linewidth=1.4,
            linestyle="--",
            zorder=4,
        )
        macd_ax.scatter(
            [sig.macd_A_index, sig.macd_B_index],
            [sig.macd_A_value, sig.macd_B_value],
            s=34,
            color=purple,
            edgecolor=black,
            linewidth=0.5,
            zorder=5,
        )
        macd_ax.annotate(
            "Bull div",
            ((sig.macd_A_index + sig.macd_B_index) / 2, max(sig.macd_A_value, sig.macd_B_value)),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            color=purple,
            fontsize=9,
            fontweight="bold",
        )

    title = f"{sig.symbol} {sig.timeframe} | {st.structure_status}"
    subtitle = f"B={sig.B_time}  BM={st.BM_time or '-'}  CM={st.CM_time or '-'}  C={(c_point or {}).get('time', '-')}"
    price_ax.set_title(f"{title}\n{subtitle}", loc="left", fontsize=12, color="#0f172a")
    price_ax.grid(True, color="#e2e8f0", linewidth=0.7)
    macd_ax.grid(True, color="#e2e8f0", linewidth=0.7)
    macd_ax.legend(loc="upper left", frameon=False, fontsize=8)
    tick_step = max(1, len(x_values) // 8)
    ticks = x_values[::tick_step]
    macd_ax.set_xticks(ticks)
    macd_ax.set_xticklabels([rows[i].datetime[:10] for i in ticks], rotation=0, fontsize=8)
    price_ax.set_ylabel("Price")
    macd_ax.set_ylabel("MACD")
    price_ax.margins(x=0.01, y=0.08)
    macd_ax.margins(x=0.01, y=0.18)
    fig.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, facecolor=fig.get_facecolor())
    plt.close(fig)


def scan_and_mark(symbol_filter: set[str] | None = None, render_charts: bool = False) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    if render_charts:
        CHART_ROOT.mkdir(parents=True, exist_ok=True)
        for old_chart in list(CHART_ROOT.glob("*.svg")) + list(CHART_ROOT.glob("*.png")):
            old_chart.unlink()
    for ordinal, (sig, st, rows) in enumerate(scan_saved_data(symbol_filter), start=1):
        record = flatten_record(sig, st)
        if render_charts:
            chart_name = f"{ordinal:05d}_{safe_symbol(sig.symbol)}_{sig.timeframe}_{st.structure_status}.png"
            chart_path = CHART_ROOT / chart_name
            render_png(sig, st, rows, chart_path)
            record["chart_file"] = str(chart_path.relative_to(ROOT))
        else:
            record["chart_file"] = ""
        records.append(record)
    write_csv(records, OUTPUT_ROOT / "ad_signals.csv")
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch stock bars and mark A-D bottom divergences.")
    parser.add_argument(
        "--provider",
        choices=["twelve-data", "yahoo"],
        default="twelve-data",
        help="Market data provider. Twelve Data requires --apikey or TWELVE_DATA_API_KEY.",
    )
    parser.add_argument("--apikey", default=None, help="Twelve Data API key. Defaults to TWELVE_DATA_API_KEY.")
    parser.add_argument("--limit", type=int, default=None, help="Limit symbols for a quick smoke test.")
    parser.add_argument(
        "--universe-source",
        choices=["sp500"],
        default="sp500",
        help=(
            "Stock universe source when --symbols-file is not provided. "
            "sp500 uses local cache, State Street SPY holdings, or the DataHub CSV fallback."
        ),
    )
    parser.add_argument(
        "--universe-etf",
        default="SPY",
        help=(
            "State Street ETF symbol used as the default sp500 proxy when metadata must be refreshed or created. "
            "SPY is the tested default."
        ),
    )
    parser.add_argument(
        "--symbols-file",
        type=Path,
        default=None,
        help=(
            "Optional stock universe file. Plain text files use one symbol per line. "
            "CSV files may include symbol/source_symbol/english_name/chinese_name/sector/sub_industry."
        ),
    )
    parser.add_argument("--workers", type=int, default=8, help="Concurrent download workers.")
    parser.add_argument("--start", default=START_DATE, help="Start date, inclusive, YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="End date, exclusive, YYYY-MM-DD. Defaults to now.")
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=10,
        help="For incremental updates, re-fetch this many calendar days before the last local bar and merge by timestamp.",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Refresh S&P 500 names and industry metadata instead of using the local metadata cache.",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing local CSV files only.")
    parser.add_argument(
        "--render-charts",
        action="store_true",
        help="Render PNG charts for manual validation. Disabled by default for cloud/batch runs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    global START_DATE
    START_DATE = args.start
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, dict[str, str]] | None = None
    symbol_filter: set[str] | None = None
    twelve_data_api_key = args.apikey or os.environ.get("TWELVE_DATA_API_KEY") or CREDENTIALS_TWELVE_DATA_API_KEY

    if args.symbols_file:
        metadata = load_symbols_file(args.symbols_file, args.limit)
        write_metadata_csv(metadata, OUTPUT_ROOT / "stock_metadata.csv")
        symbol_filter = set(metadata)

    if not args.skip_fetch:
        if args.provider == "twelve-data" and not twelve_data_api_key:
            raise SystemExit("Missing Twelve Data API key. Set TWELVE_DATA_API_KEY or pass --apikey.")
        if metadata is None:
            try:
                metadata = get_sp500_metadata(
                    args.limit,
                    refresh=args.refresh_metadata,
                    proxy_etf=args.universe_etf,
                )
            except Exception as exc:
                raise SystemExit(str(exc)) from exc
        symbols = [item["source_symbol"] for _, item in sorted(metadata.items())]
        if not symbols:
            raise SystemExit("No symbols found. Check --symbols-file or metadata cache.")
        print(
            f"provider={args.provider} universe={args.universe_source} symbols={len(symbols)} start={args.start} "
            f"end={args.end or 'now'} overlap_days={args.overlap_days}"
        )
        ok_count = 0
        failures: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    fetch_symbol,
                    symbol,
                    args.start,
                    args.end,
                    args.provider,
                    twelve_data_api_key,
                    args.overlap_days,
                ): symbol
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

    records = scan_and_mark(symbol_filter, render_charts=args.render_charts)
    status_counts: dict[str, int] = {}
    for record in records:
        status = record.get("structure_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
    chart_status = str(CHART_ROOT) if args.render_charts else "disabled"
    print(f"signals={len(records)} index={OUTPUT_ROOT / 'ad_signals.csv'} charts={chart_status}")
    for status, count in sorted(status_counts.items()):
        print(f"{status or 'UNKNOWN'}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
