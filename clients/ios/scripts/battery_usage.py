"""Estimate app battery use from matching iOS Settings > Battery readings."""

import argparse
import json
import math


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--used",
        required=True,
        type=float,
        help="Total battery used in the selected window, in percentage points",
    )
    parser.add_argument(
        "--share",
        required=True,
        type=float,
        help="EarRecorder share of usage in the same window, in percent",
    )
    parser.add_argument(
        "--hours", type=float, help="Hours in the selected window, for its hourly average"
    )
    args = parser.parse_args()
    if not math.isfinite(args.used) or args.used < 0:
        parser.error("--used must be finite and nonnegative")
    if not 0 <= args.share <= 100:
        parser.error("--share must be between 0 and 100")
    used = args.used * (args.share / 100)
    result = {"estimated_app_battery_percentage_points": round(used, 3)}
    if args.hours is not None:
        if not math.isfinite(args.hours) or args.hours <= 0:
            parser.error("--hours must be finite and positive")
        result["estimated_percentage_points_per_window_hour"] = round(used / args.hours, 3)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
