from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from constants import DATA_ROOT, FEISHU_WEBHOOKS
from intraday_tmp import cleanup_tmp_minutes, ensure_tmp_minutes
from push_utils import today_text

import bottom_trade_push
import waterline_push


def run_command(args: list[str]) -> None:
    print("run=" + " ".join(args), flush=True)
    subprocess.run(args, check=True)


def fetch_and_scan(args: argparse.Namespace) -> None:
    if args.skip_fetch_scan:
        print("skip_fetch_scan=true", flush=True)
        return
    command = [
        sys.executable,
        "fetch_sp500_2026_and_mark.py",
        "--provider",
        args.provider,
        "--start",
        args.start,
        "--overlap-days",
        str(args.overlap_days),
        "--workers",
        str(args.workers),
    ]
    if args.symbols_file:
        command.extend(["--symbols-file", str(args.symbols_file)])
    if args.fetch_timeframes:
        command.append("--fetch-timeframes")
        command.extend(args.fetch_timeframes)
    if args.apikey:
        command.extend(["--apikey", args.apikey])
    run_command(command)


def prepare_tmp_minutes(args: argparse.Namespace) -> None:
    if args.skip_tmp_download:
        print("skip_tmp_download=true", flush=True)
        return
    symbols = set()
    waterline_args = argparse.Namespace(
        date=args.date,
        symbols=None,
        symbols_file=args.symbols_file,
        daily_dir=args.daily_dir,
        start=args.waterline_start,
        volume_lookback=args.volume_lookback,
        volume_multiple=args.volume_multiple,
        candle_k=args.candle_k,
    )
    symbols.update(waterline_push.collect_required_tmp_symbols(waterline_args, args.date))
    symbols.update(bottom_trade_push.collect_required_tmp_symbols(args.date))
    if not symbols:
        print("tmp_1min_symbols=0", flush=True)
        return
    api_key = args.apikey or os.environ.get("TWELVE_DATA_API_KEY")
    if not api_key:
        try:
            from credentials import TWELVE_DATA_API_KEY as credentials_api_key
        except Exception:
            credentials_api_key = None
        api_key = credentials_api_key
    if not api_key:
        raise SystemExit("Missing Twelve Data API key for tmp 1min download.")
    counts = ensure_tmp_minutes(sorted(symbols), args.date, api_key, args.min_minutes)
    print("tmp_1min_counts=" + " ".join(f"{symbol}={counts[symbol]}" for symbol in sorted(counts)), flush=True)


def run_bottom_history(args: argparse.Namespace) -> None:
    command = [sys.executable, "bottom_history_push.py"]
    if args.dry_run:
        command.append("--dry-run")
    if args.force:
        command.append("--force")
    run_command(command)


def run_bottom_trade(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "bottom_trade_push.py",
        "--date",
        args.date,
        "--exit-below-ratio",
        str(args.exit_below_ratio),
        "--ma-window",
        str(args.ma_window),
        "--min-minutes",
        str(args.min_minutes),
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.force:
        command.append("--force")
    run_command(command)


def run_waterline(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "waterline_push.py",
        "--date",
        args.date,
        "--start",
        args.waterline_start,
        "--daily-dir",
        str(args.daily_dir),
        "--above-ratio",
        str(args.above_ratio),
        "--min-minutes",
        str(args.min_minutes),
    ]
    if args.symbols_file:
        command.extend(["--symbols-file", str(args.symbols_file)])
    if args.dry_run:
        command.append("--dry-run")
    if args.force:
        command.append("--force")
    run_command(command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the daily UpBottom fetch, scan, push, and tmp cleanup workflow.")
    parser.add_argument("--date", default=today_text())
    parser.add_argument(
        "--step",
        choices=["all", "fetch-scan", "prepare-tmp", "bottom-history", "bottom-trade", "waterline", "cleanup"],
        default="all",
        help="Run a single workflow step for more robust crontab scheduling.",
    )
    parser.add_argument("--provider", choices=["twelve-data", "yahoo"], default="twelve-data")
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--waterline-start", default="2000-01-01")
    parser.add_argument("--symbols-file", type=Path, default=None)
    parser.add_argument("--daily-dir", type=Path, default=DATA_ROOT / "1day")
    parser.add_argument("--fetch-timeframes", nargs="+", default=["1day"])
    parser.add_argument("--overlap-days", type=int, default=10)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--apikey", default=None)
    parser.add_argument("--skip-fetch-scan", action="store_true")
    parser.add_argument("--skip-tmp-download", action="store_true")
    parser.add_argument("--keep-tmp", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bottom-trade-delay-minutes", type=float, default=0.0)
    parser.add_argument("--waterline-delay-minutes", type=float, default=0.0)
    parser.add_argument("--exit-below-ratio", type=float, default=0.5)
    parser.add_argument("--ma-window", type=int, default=5)
    parser.add_argument("--min-minutes", type=int, default=300)
    parser.add_argument("--volume-lookback", type=int, default=10)
    parser.add_argument("--volume-multiple", type=float, default=2.0)
    parser.add_argument("--candle-k", type=float, default=1.2)
    parser.add_argument("--above-ratio", type=float, default=0.8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(
        f"workflow date={args.date} dry_run={args.dry_run} "
        f"webhooks={{bottom_history:{bool(FEISHU_WEBHOOKS.get('bottom_history'))},"
        f"bottom_buy:{bool(FEISHU_WEBHOOKS.get('bottom_buy'))},"
        f"bottom_sell:{bool(FEISHU_WEBHOOKS.get('bottom_sell'))},"
        f"waterline_signal:{bool(FEISHU_WEBHOOKS.get('waterline_signal'))},"
        f"waterline_trade:{bool(FEISHU_WEBHOOKS.get('waterline_trade'))}}}",
        flush=True,
    )
    if args.step == "fetch-scan":
        fetch_and_scan(args)
    elif args.step == "prepare-tmp":
        prepare_tmp_minutes(args)
    elif args.step == "bottom-history":
        run_bottom_history(args)
    elif args.step == "bottom-trade":
        run_bottom_trade(args)
    elif args.step == "waterline":
        run_waterline(args)
    elif args.step == "cleanup":
        removed = cleanup_tmp_minutes()
        print(f"tmp_1min_removed={removed}", flush=True)
    else:
        try:
            fetch_and_scan(args)
            prepare_tmp_minutes(args)
            if args.bottom_trade_delay_minutes > 0:
                time.sleep(args.bottom_trade_delay_minutes * 60)
            run_bottom_trade(args)
            if args.waterline_delay_minutes > 0:
                time.sleep(args.waterline_delay_minutes * 60)
            run_waterline(args)
        finally:
            if args.keep_tmp:
                print("keep_tmp=true", flush=True)
            else:
                removed = cleanup_tmp_minutes()
                print(f"tmp_1min_removed={removed}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
