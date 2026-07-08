#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import traceback
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Iterator, List, Sequence, Tuple

import py7zr

DEFAULT_START = dt.date.today()
DEFAULT_CACHE = Path.home() / ".tcgcsv" / "archives"
BASE_ARCHIVE_URL = "https://tcgcsv.com/archive/tcgplayer/prices-{date}.ppmd.7z"
METADATA_COLUMNS = ["date", "category_id", "group_id"]
DEFAULT_CATEGORY_IDS = ("1",)

TCGCSV_HEADERS = {
    # TCGCSV asks consumers to identify their application.
    # Generic/missing user agents may be blocked.
    "User-Agent": "MTGSitePriceUpdater/1.0",

    # This endpoint returns a binary .7z archive, not JSON.
    "Accept": "application/octet-stream,*/*;q=0.8",
}

SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"


def is_probably_7z(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(SEVEN_ZIP_MAGIC)) == SEVEN_ZIP_MAGIC
    except OSError:
        return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download TCGCSV historical price archives, extract category 1 by default, "
            "and merge rows into a CSV file."
        )
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help=(
            "Single archive date to fetch (YYYY-MM-DD). If omitted and no range is supplied, "
            "the script tries today and can fall back to yesterday."
        ),
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Inclusive start date (YYYY-MM-DD). Use with --end for backfills/ranges.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Inclusive end date (YYYY-MM-DD). Defaults to yesterday when --start is supplied.",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "json", "sqlite"],
        default="csv",
        help="Output format. Only CSV is implemented today.",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        default="./tcg_history",
        help="Directory to place the exported data.",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=str(DEFAULT_CACHE),
        help="Where downloaded archives are cached (defaults to ~/.tcgcsv/archives).",
    )
    parser.add_argument(
        "--category-id",
        action="append",
        default=None,
        help=(
            "TCGCSV category ID to export. Defaults to 1, which is Magic: The Gathering. "
            "Repeat this option to include more than one category."
        ),
    )
    parser.add_argument(
        "--no-fallback-previous-day",
        action="store_true",
        help=(
            "Disable single-day fallback. By default, if the requested single-day archive "
            "is missing, the script tries the previous day's archive."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the output CSV even if it already exists.",
    )
    return parser.parse_args(argv)


def parse_iso_date(value: str | None, fallback: dt.date | None = None) -> dt.date:
    if value is None:
        if fallback is None:
            raise ValueError("No date supplied and no fallback available")
        return fallback
    return dt.date.fromisoformat(value)


def date_range(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    delta = (end - start).days
    for offset in range(delta + 1):
        yield start + dt.timedelta(days=offset)


def archive_cache_path(date_str: str, cache_dir: Path) -> Path:
    return cache_dir / f"prices-{date_str}.ppmd.7z"


def download_archive(
    date_str: str,
    cache_dir: Path,
    retries: int = 3,
    timeout: int = 60,
) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_cache_path(date_str, cache_dir)
    archive_name = archive_path.name
    partial_path = archive_path.with_suffix(archive_path.suffix + ".part")

    if archive_path.exists():
        if archive_path.stat().st_size > 0 and is_probably_7z(archive_path):
            print(f"[cache] {archive_name}")
            return archive_path
        print(f"[warn] removing invalid cached archive: {archive_path}")
        archive_path.unlink(missing_ok=True)

    url = BASE_ARCHIVE_URL.format(date=date_str)
    for attempt in range(1, retries + 1):
        try:
            print(f"[download] {url}")
            request = urllib.request.Request(url, headers=TCGCSV_HEADERS, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response, partial_path.open("wb") as fh:
                shutil.copyfileobj(response, fh)

            if partial_path.stat().st_size == 0:
                raise ValueError("Downloaded file is empty")
            if not is_probably_7z(partial_path):
                raise ValueError(
                    "Downloaded response is not a 7z archive. "
                    "This usually means TCGCSV returned an HTML error page instead."
                )

            partial_path.replace(archive_path)
            return archive_path

        except urllib.error.HTTPError as exc:
            partial_path.unlink(missing_ok=True)
            if exc.code == 404:
                print(f"[warn] archive missing for {date_str} (HTTP 404)")
                return None
            print(f"[warn] HTTP error {exc.code} while downloading {date_str}: {exc}")

        except Exception as exc:  # noqa: BLE001 - broad for retry simplicity
            partial_path.unlink(missing_ok=True)
            wait = min(5, attempt)
            print(f"[warn] attempt {attempt} failed for {date_str}: {exc}. retrying in {wait}s")
            time.sleep(wait)

    print(f"[error] giving up on {date_str} after {retries} attempts")
    archive_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)
    return None


def choose_single_day_archive(
    requested_date: dt.date,
    cache_dir: Path,
    fallback_previous_day: bool = True,
) -> tuple[dt.date, Path] | tuple[None, None]:
    """Try the requested date; if missing, optionally try requested_date - 1 day.

    This is intended for a daily sync job. Existing cache files are reused by
    download_archive(), so the previous day is not redownloaded when already cached.
    """
    requested_str = requested_date.isoformat()
    archive_path = download_archive(requested_str, cache_dir)
    if archive_path is not None:
        return requested_date, archive_path

    if not fallback_previous_day:
        return None, None

    fallback_date = requested_date - dt.timedelta(days=1)
    fallback_str = fallback_date.isoformat()
    print(f"[fallback] {requested_str} is unavailable; trying {fallback_str}")

    fallback_archive_path = download_archive(fallback_str, cache_dir)
    if fallback_archive_path is None:
        return None, None

    return fallback_date, fallback_archive_path


def extract_archive(archive_path: Path) -> Path | None:
    temp_dir = Path(tempfile.mkdtemp(prefix="tcg_history_"))
    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            archive.extractall(path=temp_dir)
        return temp_dir
    except Exception as exc:
        print(f"[error] failed to extract {archive_path.name}: {exc}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


def locate_date_root(extracted_dir: Path, date_str: str) -> Path:
    candidate = extracted_dir / date_str
    return candidate if candidate.exists() else extracted_dir


def iter_price_rows(
    date_dir: Path,
    date_str: str,
    category_ids: Sequence[str] = DEFAULT_CATEGORY_IDS,
) -> Iterator[Tuple[Sequence[str], dict, Path]]:
    category_id_set = {str(category_id) for category_id in category_ids}
    category_roots = [date_dir / category_id for category_id in category_id_set]
    existing_category_roots = [path for path in category_roots if path.exists()]

    if existing_category_roots:
        price_paths = (
            price_path
            for category_root in existing_category_roots
            for price_path in category_root.rglob("prices")
        )
    else:
        # Fallback for archive layouts where category_id is not immediately under date_dir.
        price_paths = date_dir.rglob("prices")

    for price_path in price_paths:
        rel_parts = price_path.relative_to(date_dir).parts
        if len(rel_parts) < 3 or not price_path.is_file():
            continue

        category_id = rel_parts[-3]
        if category_id not in category_id_set:
            continue

        group_id = rel_parts[-2]
        try:
            with price_path.open("r", newline="", encoding="utf-8") as handle:
                content = handle.read().strip()
                if not content:
                    continue

                try:
                    data = json.loads(content)
                    if not data.get("success", False):
                        continue

                    results = data.get("results", [])
                    for item in results:
                        enriched = {
                            "date": date_str,
                            "category_id": category_id,
                            "group_id": group_id,
                        }
                        # Flatten the nested price data.
                        enriched.update(item)
                        yield list(item.keys()), enriched, price_path

                except json.JSONDecodeError as exc:
                    print(f"[warn] failed to parse JSON in {price_path}: {exc}")
                    continue

        except Exception as exc:  # noqa: BLE001 - continue on parse errors
            print(f"[warn] failed to read {price_path}: {exc}")
            continue


def ensure_csv_writer(
    metadata: Sequence[str],
    first_payload_fields: Sequence[str],
    file_handle,
) -> csv.DictWriter:
    fieldnames = list(metadata) + list(first_payload_fields)
    writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
    writer.writeheader()
    return writer


def export_csv(
    out_path: Path,
    rows: Iterator[Tuple[Sequence[str], dict, Path]],
) -> Tuple[int, Sequence[str]]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    payload_header: List[str] | None = None

    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer: csv.DictWriter | None = None
        for file_header, row, source_path in rows:
            if payload_header is None:
                payload_header = list(file_header)
                writer = ensure_csv_writer(METADATA_COLUMNS, payload_header, fp)
            elif payload_header != list(file_header):
                raise RuntimeError(
                    "Encountered mismatched columns between price files. "
                    f"First header: {payload_header}, new header from {source_path}: {file_header}"
                )
            assert writer is not None
            writer.writerow(row)
            total_rows += 1
    return total_rows, payload_header or []


def cleanup(path: Path | None) -> None:
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def build_output_path(
    outdir: Path,
    category_ids: Sequence[str],
    start_date: dt.date,
    end_date: dt.date,
) -> Path:
    category_label = "_".join(str(category_id) for category_id in category_ids)
    if start_date == end_date:
        return outdir / f"prices_category_{category_label}_{start_date.isoformat()}.csv"
    return outdir / f"prices_category_{category_label}_{start_date.isoformat()}_{end_date.isoformat()}.csv"


def csv_exists(output_path: Path) -> bool:
    return output_path.exists() and output_path.stat().st_size > 0


def skip_existing_csv(output_path: Path, force: bool = False) -> bool:
    if force:
        return False
    if csv_exists(output_path):
        print(f"[skip] output CSV already exists: {output_path}")
        print("[skip] use --force to rebuild it")
        return True
    return False


def run_pipeline(args: argparse.Namespace) -> int:
    today = dt.date.today()
    default_end = today - dt.timedelta(days=1)

    outdir = Path(args.outdir).expanduser()
    cache_dir = Path(args.cache_dir).expanduser()
    category_ids = tuple(str(category_id) for category_id in (args.category_id or DEFAULT_CATEGORY_IDS))

    if args.format != "csv":
        raise SystemExit("Only --format csv is available in this simplified tool.")

    if args.date and (args.start or args.end):
        raise SystemExit("Use either --date for a single daily sync or --start/--end for a range, not both.")

    total_processed = 0

    def rows_from_archive(current_date: dt.date, archive_path: Path) -> Iterator[Tuple[Sequence[str], dict, Path]]:
        nonlocal total_processed
        date_str = current_date.isoformat()
        extracted_dir = extract_archive(archive_path)
        if extracted_dir is None:
            print(f"[skip] failed to extract {archive_path.name}")
            return
        date_root = locate_date_root(extracted_dir, date_str)
        try:
            row_count_before = total_processed
            for payload in iter_price_rows(date_root, date_str, category_ids):
                total_processed += 1
                yield payload
            print(f"[done] processed {total_processed - row_count_before} rows from {date_str}")
        finally:
            cleanup(extracted_dir)

    # Single-day daily mode: default to today, with fallback to yesterday.
    if args.date or not (args.start or args.end):
        requested_date = parse_iso_date(args.date, today)
        requested_output_path = build_output_path(outdir, category_ids, requested_date, requested_date)

        # Strongest short-circuit: if the final CSV already exists, do not even
        # check the archive URL. This keeps routine daily runs cheap and safe.
        if skip_existing_csv(requested_output_path, force=args.force):
            return 0

        requested_str = requested_date.isoformat()
        archive_path = download_archive(requested_str, cache_dir)
        actual_date = requested_date

        if archive_path is None:
            if args.no_fallback_previous_day:
                raise SystemExit(f"No archive available for {requested_str}.")

            fallback_date = requested_date - dt.timedelta(days=1)
            fallback_str = fallback_date.isoformat()
            fallback_output_path = build_output_path(outdir, category_ids, fallback_date, fallback_date)
            print(f"[fallback] {requested_str} is unavailable; trying {fallback_str}")

            # If yesterday's CSV already exists, there is nothing left to do.
            # Do not redownload or re-extract yesterday's archive.
            if skip_existing_csv(fallback_output_path, force=args.force):
                return 0

            archive_path = download_archive(fallback_str, cache_dir)
            actual_date = fallback_date

        if archive_path is None:
            raise SystemExit(f"No archive available for {requested_str} or the previous day.")

        output_path = build_output_path(outdir, category_ids, actual_date, actual_date)
        if skip_existing_csv(output_path, force=args.force):
            return 0

        total_rows, payload_header = export_csv(output_path, rows_from_archive(actual_date, archive_path))
        print(
            f"\nExported {total_rows} rows for {actual_date.isoformat()} "
            f"for category/categories {', '.join(category_ids)} into {output_path}"
        )
        if actual_date != requested_date:
            print(f"[note] requested {requested_date.isoformat()}, but used fallback {actual_date.isoformat()}")
        if not payload_header:
            print("[warn] No rows were exported. Check archive layout or category IDs.")
        return 0

    # Range/backfill mode: preserve skip-on-missing behavior.
    start_date = parse_iso_date(args.start, DEFAULT_START)
    end_date = parse_iso_date(args.end, default_end)
    if start_date > end_date:
        raise SystemExit("--start cannot be after --end")

    output_path = build_output_path(outdir, category_ids, start_date, end_date)
    if skip_existing_csv(output_path, force=args.force):
        return 0

    def range_row_generator() -> Iterator[Tuple[Sequence[str], dict, Path]]:
        for current_date in date_range(start_date, end_date):
            date_str = current_date.isoformat()
            print(f"\n=== {date_str} ===")
            archive_path = download_archive(date_str, cache_dir)
            if archive_path is None:
                print(f"[skip] no archive for {date_str}")
                continue
            yield from rows_from_archive(current_date, archive_path)

    total_rows, payload_header = export_csv(output_path, range_row_generator())
    print(
        f"\nExported {total_rows} rows from {start_date.isoformat()} to {end_date.isoformat()} "
        f"for category/categories {', '.join(category_ids)} into {output_path}"
    )
    if not payload_header:
        print("[warn] No rows were exported. Check date range, archive layout, or category IDs.")
    return 0

def parse_iso_date_from_output_path(path: Path | None) -> str | None:
    if path is None:
        return None

    stem = path.stem
    # Expected: prices_category_1_2026-07-05
    date_part = stem.split("_")[-1]

    try:
        return dt.date.fromisoformat(date_part).isoformat()
    except ValueError:
        return None

def parse_iso_date_from_output_path(path: Path | None) -> str | None:
    if path is None:
        return None

    stem = path.stem
    # Expected: prices_category_1_2026-07-05
    date_part = stem.split("_")[-1]

    try:
        return dt.date.fromisoformat(date_part).isoformat()
    except ValueError:
        return None
def export_prices_for_date(
    target_date: dt.date | str,
    outdir: str | Path,
    category_ids: Sequence[str | int] = DEFAULT_CATEGORY_IDS,
    cache_dir: str | Path | None = None,
    fallback_previous_day: bool = True,
    force: bool = False,
) -> dict:
    """
    App-callable wrapper around the existing TCGCSV archive export pipeline.

    This lets the Flask app / PyInstaller executable create a dated CSV without
    spawning a second Python process.

    It preserves the CLI behavior because main() can still call run_pipeline().
    """
    if isinstance(target_date, str):
        target_date = dt.date.fromisoformat(target_date)
    elif isinstance(target_date, dt.datetime):
        target_date = target_date.date()

    outdir = Path(outdir).expanduser()
    cache_dir = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE

    normalized_category_ids = tuple(str(category_id) for category_id in category_ids)

    args = argparse.Namespace(
        date=target_date.isoformat(),
        start=None,
        end=None,
        format="csv",
        outdir=str(outdir),
        cache_dir=str(cache_dir),
        category_id=list(normalized_category_ids),
        no_fallback_previous_day=not fallback_previous_day,
        force=force,
    )

    stdout_buffer = StringIO()
    stderr_buffer = StringIO()

    requested_output_path = build_output_path(
        outdir=outdir,
        category_ids=normalized_category_ids,
        start_date=target_date,
        end_date=target_date,
    )

    fallback_output_path = build_output_path(
        outdir=outdir,
        category_ids=normalized_category_ids,
        start_date=target_date - dt.timedelta(days=1),
        end_date=target_date - dt.timedelta(days=1),
    )

    returncode = 0

    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            returncode = run_pipeline(args)

    except SystemExit as exc:
        if isinstance(exc.code, int):
            returncode = exc.code
        else:
            returncode = 1
            stderr_buffer.write(str(exc.code))

    except Exception:
        returncode = 1
        stderr_buffer.write(traceback.format_exc())

    candidate_paths = [requested_output_path]

    if fallback_previous_day:
        candidate_paths.append(fallback_output_path)

    written_path = next(
        (
            path
            for path in candidate_paths
            if path.exists() and path.stat().st_size > 0
        ),
        None,
    )

    return {
        "attempted": True,
        "updated": written_path is not None,
        "requested_date": target_date.isoformat(),
        "date": (
            parse_iso_date_from_output_path(written_path)
            if written_path
            else target_date.isoformat()
        ),
        "path": str(written_path) if written_path else None,
        "stdout": stdout_buffer.getvalue(),
        "stderr": stderr_buffer.getvalue(),
        "returncode": returncode,
    }

def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_pipeline(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
