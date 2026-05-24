"""Build a pipeline-ready symbol list from the US 1610 stock universe."""

from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.request
from pathlib import Path


NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"
OUTPUT_FIELDS = ["symbol", "source_symbol", "english_name", "chinese_name", "sector", "sub_industry"]
MANUAL_METADATA = {
    "APLS": {
        "name": "Apellis Pharmaceuticals Inc.",
        "sector": "Health Care",
        "industry": "Biotechnology: Pharmaceutical Preparations",
    },
    "BF.B": {
        "name": "Brown-Forman Corporation",
        "sector": "Consumer Staples",
        "industry": "Beverages",
    },
    "BK": {
        "name": "The Bank of New York Mellon Corporation",
        "sector": "Financials",
        "industry": "Major Banks",
    },
    "BRK.B": {
        "name": "Berkshire Hathaway Inc.",
        "sector": "Financials",
        "industry": "Property-Casualty Insurers",
    },
    "CTRA": {
        "name": "Coterra Energy Inc.",
        "sector": "Energy",
        "industry": "Oil & Gas Production",
    },
    "CUK": {
        "name": "Carnival plc",
        "sector": "Consumer Discretionary",
        "industry": "Marine Transportation",
    },
    "EGO": {
        "name": "Eldorado Gold Corporation",
        "sector": "Basic Materials",
        "industry": "Precious Metals",
    },
    "EGP": {
        "name": "EastGroup Properties Inc.",
        "sector": "Real Estate",
        "industry": "Real Estate Investment Trusts",
    },
    "TPH": {
        "name": "Tri Pointe Homes Inc.",
        "sector": "Consumer Discretionary",
        "industry": "Homebuilding",
    },
}


def safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "-").replace("/", "_")


def read_universe(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("symbol")]


def fetch_nasdaq_metadata() -> dict[str, dict[str, str]]:
    request = urllib.request.Request(
        NASDAQ_SCREENER_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("data", {}).get("rows") or []
    metadata = {str(row.get("symbol") or "").upper(): row for row in rows if row.get("symbol")}
    metadata.update(MANUAL_METADATA)
    return metadata


def clean_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    suffixes = [
        "Common Stock",
        "Common Shares",
        "Ordinary Shares",
        "Class A Common Stock",
        "Class B Common Stock",
        "Class C Common Stock",
    ]
    for suffix in suffixes:
        text = re.sub(rf"\s+{re.escape(suffix)}\.?$", "", text, flags=re.IGNORECASE).strip()
    return text


def build_symbols(universe_rows: list[dict[str, str]], nasdaq_metadata: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in universe_rows:
        source_symbol = str(item.get("symbol") or "").strip().upper()
        if not source_symbol or source_symbol in seen:
            continue
        seen.add(source_symbol)
        info = nasdaq_metadata.get(source_symbol) or {}
        out.append(
            {
                "symbol": safe_symbol(source_symbol),
                "source_symbol": source_symbol,
                "english_name": clean_name(str(info.get("name") or "")),
                "chinese_name": "",
                "sector": str(info.get("sector") or ""),
                "sub_industry": str(info.get("industry") or ""),
            }
        )
    return out


def write_symbols(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build symbols_us_1610.csv with Nasdaq metadata.")
    parser.add_argument("--input", type=Path, default=Path("us_1610_stock_universe.csv"))
    parser.add_argument("--output", type=Path, default=Path("symbols_us_1610.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    universe_rows = read_universe(args.input)
    nasdaq_metadata = fetch_nasdaq_metadata()
    rows = build_symbols(universe_rows, nasdaq_metadata)
    write_symbols(rows, args.output)
    enriched = sum(1 for row in rows if row["english_name"] or row["sector"] or row["sub_industry"])
    print(f"universe={len(universe_rows)} symbols={len(rows)} enriched={enriched} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
