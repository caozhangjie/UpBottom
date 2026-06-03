from __future__ import annotations

import argparse
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper for bottom_backtest.py and waterline_backtest.py.",
        add_help=False,
    )
    parser.add_argument("--strategy", choices=["bottom", "waterline", "both"], default="both")
    parser.add_argument("--help", "-h", action="store_true")
    args, remaining = parser.parse_known_args()
    args.remaining = remaining
    return args


def run(module_file: str, extra_args: list[str]) -> None:
    command = [sys.executable, module_file, *extra_args]
    print("run=" + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()
    if args.help:
        print("Use bottom_backtest.py for BM/C/D bottom-divergence comparisons.")
        print("Use waterline_backtest.py for waterline strategy backtests.")
        print("This wrapper accepts --strategy bottom|waterline|both and forwards all other args.")
        return 0
    if args.strategy in {"bottom", "both"}:
        run("bottom_backtest.py", args.remaining)
    if args.strategy in {"waterline", "both"}:
        run("waterline_backtest.py", args.remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
