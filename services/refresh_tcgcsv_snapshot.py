import argparse
import os
import sys

# Allow running as `python scripts/refresh_tcgcsv_snapshot.py` from repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.tcgcsv_prices import refresh_daily_price_snapshot_if_needed


def main():
    parser = argparse.ArgumentParser(
        description="Download and cache today's TCGCSV prices to a local CSV snapshot."
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output CSV path. Defaults to var/data/tcgcsv/daily_prices_latest.csv",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a full snapshot refresh even when remote last-updated is unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report whether a refresh would run; do not download prices.",
    )
    args = parser.parse_args()

    result = refresh_daily_price_snapshot_if_needed(
        snapshot_path=args.output if args.output else None,
        force=args.force,
        dry_run=args.dry_run,
    )

    if result["status"] == "updated":
        print(f"TCGCSV snapshot updated: {result['snapshot_path']}")
    elif result["status"] == "unchanged":
        print(
            "TCGCSV snapshot unchanged; skipping download "
            f"(remote last-updated: {result['remote_last_updated']})."
        )
    else:
        print(
            "TCGCSV dry-run complete: "
            f"status={result['status']}, remote={result['remote_last_updated']}"
        )


if __name__ == "__main__":
    main()
