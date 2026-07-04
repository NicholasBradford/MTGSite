import csv
import json
import os
import time, requests
import subprocess
import sys
import re
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from collections import defaultdict
from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError, RequestException

load_dotenv()

TCGCSV_BASE_URL = "https://tcgcsv.com/tcgplayer"
TCGCSV_LAST_UPDATED_URL = "https://tcgcsv.com/last-updated.txt"
TCGCSV_MAGIC_CATEGORY_ID = 1
TCGCSV_RATE_LIMIT_DELAY = .15
TCGCSV_BULK_RATE_LIMIT_DELAY = .10
TCGCSV_BULK_TIMEOUT_SECONDS = 6
TCGCSV_BULK_MAX_ATTEMPTS = 1
TCGCSV_MIN_GROUPS_WITH_PRICES = 100
TCGCSV_MIN_ROWS_FOR_VALID_SNAPSHOT = 10000
TCGCSV_REQUEST_COOLDOWN_SECONDS = 300
TCGCSV_SNAPSHOT_DIR = os.path.join("var", "data", "tcgcsv")
TCGCSV_LOCAL_PRICE_SNAPSHOT = os.path.join(TCGCSV_SNAPSHOT_DIR, "daily_prices_latest.csv")
TCGCSV_LOCAL_SNAPSHOT_METADATA = os.path.join(TCGCSV_SNAPSHOT_DIR, "snapshot_metadata.json")
TCGCSV_HISTORY_DIR = os.environ.get("TCGCSV_HISTORY_DIR", "tcg_history")
TCGCSV_HISTORY_FILE_PREFIX = "prices_category_1_"
TCGCSV_HISTORY_FILE_SUFFIX = ".csv"
TCGCSV_LOCAL_TIMEZONE = os.environ.get("TCGCSV_LOCAL_TIMEZONE", "America/Chicago")
TCGCSV_DAILY_RELEASE_HOUR_LOCAL = int(os.environ.get("TCGCSV_DAILY_RELEASE_HOUR_LOCAL", "15"))
TCGCSV_FETCH_SCRIPT_PATH = os.environ.get("TCGCSV_FETCH_SCRIPT_PATH", "_get_tcgcsv.py")
TCGCSV_FETCH_TIMEOUT_SECONDS = int(os.environ.get("TCGCSV_FETCH_TIMEOUT_SECONDS", "600"))
TCGCSV_FETCH_RETRY_COOLDOWN_SECONDS = int(os.environ.get("TCGCSV_FETCH_RETRY_COOLDOWN_SECONDS", "900"))

TCGCSV_LOCAL_PRODUCTS_CACHE = os.path.join(
    TCGCSV_HISTORY_DIR,
    "products_category_1_latest.csv",
)

_LOCAL_PRODUCTS_CACHE = {
    "path": None,
    "mtime": None,
    "by_product_id": {},
    "by_group_id": {},
}

_TCGCSV_HISTORY_REFRESH_CACHE = {
    "checked_at": 0.0,
    "date": None,
    "after_release": None,
    "result": None,
}

TCGCSV_SOURCE_LOCAL_ONLY = "local_only"
TCGCSV_SOURCE_REMOTE_FALLBACK_LOCAL = "remote_fallback_local"
AUTO_FINISH_OVERRIDE_NOTE_PREFIX = "Auto-mapped from local TCGCSV price CSV"

_TCGCSV_COOLDOWN_UNTIL = 0.0
_LOCAL_SNAPSHOT_CACHE = {
    "path": None,
    "mtime": None,
    "group_prices": {},
}

TCGCSV_HEADERS = {
    "User-Agent": "MTGSitePriceUpdater/1.0",
    "Accept": "application/json",
}

SPECIAL_FINISHES = {
    "rainbow foil",
    "surge foil",
    "galaxy foil",
    "etched foil",
    "textured foil",
    "double rainbow foil",
}

SUPPORTED_TCGCSV_FINISHES = {
    "foil",
    "rainbow foil",
    "surge foil",
    "galaxy foil",
    "etched foil",
    "textured foil",
    "double rainbow foil",
}

PRICE_FIELD_ALIASES = {
    "low_price": ("lowPrice", "low_price", "lowprice"),
    "mid_price": ("midPrice", "mid_price", "midprice"),
    "high_price": ("highPrice", "high_price", "highprice"),
    "market_price": ("marketPrice", "market_price", "marketprice"),
    "direct_low_price": ("directLowPrice", "direct_low_price", "directlowprice"),
}

def tcgcsv_get_json(session, url, timeout=30):
    response = tcgcsv_request(session, url, timeout=timeout)
    return response.json()


def tcgcsv_get_text(session, url, timeout=15):
    response = tcgcsv_request(session, url, timeout=timeout)
    return response.text.strip()


def _read_json_file(path):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"

    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    os.replace(tmp_path, path)


def get_local_snapshot_metadata(metadata_path=TCGCSV_LOCAL_SNAPSHOT_METADATA):
    return _read_json_file(metadata_path)


def local_snapshot_exists(snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT):
    return os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0


def get_local_snapshot_last_updated(
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    metadata_path=TCGCSV_LOCAL_SNAPSHOT_METADATA,
):
    metadata = get_local_snapshot_metadata(metadata_path=metadata_path)
    if metadata.get("last_updated") and metadata.get("snapshot_path") == snapshot_path:
        return metadata.get("last_updated")

    if not os.path.exists(snapshot_path):
        return None

    try:
        with open(snapshot_path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            first_row = next(reader, None)
            if first_row:
                return first_row.get("snapshot_last_updated")
    except Exception:
        return None

    return None

def parse_tcgcsv_history_file_date(path):
    filename = os.path.basename(path)

    if not filename.startswith(TCGCSV_HISTORY_FILE_PREFIX):
        return None

    if not filename.endswith(TCGCSV_HISTORY_FILE_SUFFIX):
        return None

    date_part = filename[
        len(TCGCSV_HISTORY_FILE_PREFIX):-len(TCGCSV_HISTORY_FILE_SUFFIX)
    ]

    # Only accept single-day files for this logic.
    # Example: prices_category_1_2026-06-26.csv
    if "_" in date_part:
        return None

    try:
        return datetime.strptime(date_part, "%Y-%m-%d").date()
    except ValueError:
        return None


def find_tcgcsv_history_file_for_date(price_date, history_dir=TCGCSV_HISTORY_DIR):
    filename = (
        f"{TCGCSV_HISTORY_FILE_PREFIX}"
        f"{price_date.isoformat()}"
        f"{TCGCSV_HISTORY_FILE_SUFFIX}"
    )
    path = os.path.join(history_dir, filename)

    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    return None


def get_tcgcsv_history_file_candidates(history_dir=TCGCSV_HISTORY_DIR):
    """
    Return available single-day tcg_history CSVs as:
        (price_date, modified_time, path)

    Sorted newest first by CSV date, then file modified time.
    """
    if not os.path.isdir(history_dir):
        return []

    candidates = []

    for filename in os.listdir(history_dir):
        path = os.path.join(history_dir, filename)

        if not os.path.isfile(path):
            continue

        if os.path.getsize(path) <= 0:
            continue

        file_date = parse_tcgcsv_history_file_date(path)
        if file_date is None:
            continue

        candidates.append((file_date, os.path.getmtime(path), path))

    return sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)


def find_latest_tcgcsv_history_file(history_dir=TCGCSV_HISTORY_DIR):
    candidates = get_tcgcsv_history_file_candidates(history_dir=history_dir)
    if not candidates:
        return None

    return candidates[0][2]


def find_prior_tcgcsv_history_files(
    before_date,
    history_dir=TCGCSV_HISTORY_DIR,
    limit=None,
):
    """
    Return local history CSVs older than before_date, newest first.

    This anchors backfill to the newest CSV being imported. For example, if the
    newest available file is 2026-06-26, backfill looks at 2026-06-25, then
    2026-06-24, and so on if files exist.
    """
    candidates = [
        (file_date, modified_time, path)
        for file_date, modified_time, path in get_tcgcsv_history_file_candidates(history_dir=history_dir)
        if file_date < before_date
    ]

    paths = [path for _, _, path in candidates]
    if limit is not None:
        return paths[:limit]

    return paths

def fetch_tcgcsv_history_csv_for_date(target_date):
    """
    Run _get_tcgcsv.py for exactly one target date.

    This helper deliberately passes --no-fallback-previous-day so this parent
    module controls fallback behavior and status reporting stays accurate.
    """
    script_path = TCGCSV_FETCH_SCRIPT_PATH

    if not os.path.exists(script_path):
        return {
            "attempted": False,
            "updated": False,
            "status": "fetch_script_missing",
            "message": f"Could not find TCGCSV fetch script: {script_path}",
            "date": target_date.isoformat(),
            "path": find_latest_tcgcsv_history_file(),
        }

    command = [
        sys.executable,
        script_path,
        "--date",
        target_date.isoformat(),
        "--outdir",
        TCGCSV_HISTORY_DIR,
        "--no-fallback-previous-day",
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TCGCSV_FETCH_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as error:
        return {
            "attempted": True,
            "updated": False,
            "status": "fetch_failed",
            "message": f"Failed to run _get_tcgcsv.py for {target_date.isoformat()}: {error}",
            "date": target_date.isoformat(),
            "path": find_latest_tcgcsv_history_file(),
        }

    path_after_fetch = find_tcgcsv_history_file_for_date(target_date)

    if path_after_fetch:
        return {
            "attempted": True,
            "updated": True,
            "status": "csv_downloaded",
            "message": f"Downloaded local CSV for {target_date.isoformat()}: {path_after_fetch}",
            "date": target_date.isoformat(),
            "path": path_after_fetch,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    return {
        "attempted": True,
        "updated": False,
        "status": "csv_unavailable",
        "message": (
            f"No CSV was created for {target_date.isoformat()}; "
            f"continuing with newest available local CSV: {find_latest_tcgcsv_history_file()}"
        ),
        "date": target_date.isoformat(),
        "path": find_latest_tcgcsv_history_file(),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }

def refresh_current_day_history_csv_if_due(now=None):
    """
    Before the daily TCGCSV release cutoff, ensure yesterday's archive CSV exists.

    After the release cutoff, try today's archive CSV first. If today's archive
    is unavailable, ensure yesterday's archive CSV exists.

    This does not replace the normal local CSV resolver. It only gives
    _get_tcgcsv.py a chance to create a newer CSV before the sync uses the
    newest available local file.
    """
    timezone = ZoneInfo(TCGCSV_LOCAL_TIMEZONE)
    local_now = now or datetime.now(timezone)
    today = local_now.date()
    yesterday = today - timedelta(days=1)

    def ensure_yesterday_csv(reason_status):
        existing_yesterday_path = find_tcgcsv_history_file_for_date(yesterday)

        if existing_yesterday_path:
            return {
                "attempted": False,
                "updated": False,
                "status": f"{reason_status}_previous_day_csv_exists",
                "message": f"Previous-day local CSV already exists: {existing_yesterday_path}",
                "date": yesterday.isoformat(),
                "path": existing_yesterday_path,
            }

        result = fetch_tcgcsv_history_csv_for_date(yesterday)

        return {
            **result,
            "status": f"{reason_status}_{result['status']}",
            "message": (
                f"Previous-day CSV was missing for {yesterday.isoformat()}. "
                f"{result['message']}"
            ),
        }

    if local_now.hour < TCGCSV_DAILY_RELEASE_HOUR_LOCAL:
        return ensure_yesterday_csv("before_release_cutoff")

    existing_today_path = find_tcgcsv_history_file_for_date(today)

    if existing_today_path:
        return {
            "attempted": False,
            "updated": False,
            "status": "current_day_csv_exists",
            "message": f"Current-day local CSV already exists: {existing_today_path}",
            "date": today.isoformat(),
            "path": existing_today_path,
        }

    today_result = fetch_tcgcsv_history_csv_for_date(today)
    today_path_after_fetch = find_tcgcsv_history_file_for_date(today)

    if today_path_after_fetch:
        return {
            **today_result,
            "status": "current_day_csv_downloaded",
            "message": f"Downloaded current-day local CSV: {today_path_after_fetch}",
            "date": today.isoformat(),
            "path": today_path_after_fetch,
        }

    yesterday_result = ensure_yesterday_csv("current_day_unavailable")

    return {
        **yesterday_result,
        "current_day_fetch": {
            "date": today.isoformat(),
            "status": today_result.get("status"),
            "message": today_result.get("message"),
            "stdout": today_result.get("stdout"),
            "stderr": today_result.get("stderr"),
            "returncode": today_result.get("returncode"),
        },
    }

def ensure_current_day_history_csv_if_due(now=None, force=False):
    """
    Give _get_tcgcsv.py a chance to create today's tcg_history CSV before
    resolving local prices. Results are cached briefly so one price sync does
    not spawn the fetch script repeatedly.
    """
    timezone = ZoneInfo(TCGCSV_LOCAL_TIMEZONE)
    local_now = now or datetime.now(timezone)
    today_iso = local_now.date().isoformat()
    after_release = local_now.hour >= TCGCSV_DAILY_RELEASE_HOUR_LOCAL

    if (
        not force
        and _TCGCSV_HISTORY_REFRESH_CACHE["date"] == today_iso
        and _TCGCSV_HISTORY_REFRESH_CACHE["after_release"] == after_release
        and _TCGCSV_HISTORY_REFRESH_CACHE["result"] is not None
        and time.monotonic() - _TCGCSV_HISTORY_REFRESH_CACHE["checked_at"] < TCGCSV_FETCH_RETRY_COOLDOWN_SECONDS
    ):
        return _TCGCSV_HISTORY_REFRESH_CACHE["result"]

    result = refresh_current_day_history_csv_if_due(now=local_now)

    _TCGCSV_HISTORY_REFRESH_CACHE["checked_at"] = time.monotonic()
    _TCGCSV_HISTORY_REFRESH_CACHE["date"] = today_iso
    _TCGCSV_HISTORY_REFRESH_CACHE["after_release"] = after_release
    _TCGCSV_HISTORY_REFRESH_CACHE["result"] = result

    if result.get("updated"):
        _LOCAL_SNAPSHOT_CACHE["path"] = None
        _LOCAL_SNAPSHOT_CACHE["mtime"] = None
        _LOCAL_SNAPSHOT_CACHE["group_prices"] = {}

    return result

def resolve_local_price_snapshot_path(snapshot_path=None, refresh_if_due=True):
    if snapshot_path:
        return snapshot_path
    
    if refresh_if_due:
        ensure_current_day_history_csv_if_due()

    history_path = find_latest_tcgcsv_history_file()
    if history_path:
        return history_path

    return TCGCSV_LOCAL_PRICE_SNAPSHOT


def get_price_date_for_snapshot(snapshot_path):
    history_date = parse_tcgcsv_history_file_date(snapshot_path)
    if history_date:
        return history_date.isoformat()

    snapshot_last_updated = get_local_snapshot_last_updated(snapshot_path=snapshot_path)
    if snapshot_last_updated:
        return snapshot_last_updated[:10]

    return datetime.utcnow().date().isoformat()

def get_remote_tcgcsv_last_updated(session=None, timeout=15):
    local_session = session or requests.Session()
    local_session.headers.update(TCGCSV_HEADERS)
    return tcgcsv_get_text(local_session, TCGCSV_LAST_UPDATED_URL, timeout=timeout)


def should_refresh_local_snapshot(
    remote_last_updated,
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    metadata_path=TCGCSV_LOCAL_SNAPSHOT_METADATA,
):
    if not local_snapshot_exists(snapshot_path=snapshot_path):
        return True

    local_last_updated = get_local_snapshot_last_updated(
        snapshot_path=snapshot_path,
        metadata_path=metadata_path,
    )

    if not local_last_updated:
        return True

    return str(remote_last_updated).strip() != str(local_last_updated).strip()


def write_snapshot_metadata(
    last_updated,
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    metadata_path=TCGCSV_LOCAL_SNAPSHOT_METADATA,
):
    metadata = {
        "last_updated": last_updated,
        "snapshot_path": snapshot_path,
        "captured_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata


def normalize_tcgcsv_price(value):
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def choose_tcgcsv_price(price_obj):
    """
    Prefer TCGplayer marketPrice because it is closer to recent selling price.
    Fall back to midPrice, then lowPrice, because marketPrice can be null.
    """
    return (
        normalize_tcgcsv_price(price_obj.get("marketPrice"))
        or normalize_tcgcsv_price(price_obj.get("midPrice"))
        or normalize_tcgcsv_price(price_obj.get("lowPrice"))
    )


def price_finish_from_subtype(subtype_name):
    """
    TCGCSV/TCGplayer subtype names vary by game/product.
    For MTG, the common useful buckets are Normal, Foil, and Etched Foil.
    Your app currently tracks nonfoil vs foil, so etched should behave as foil
    unless you later split etched into its own finish.
    """
    subtype = (subtype_name or "").strip().lower()

    if "etched" in subtype:
        return "foil"

    if "foil" in subtype:
        return "foil"

    return "nonfoil"


def get_tcgcsv_magic_groups(session):
    url = f"{TCGCSV_BASE_URL}/{TCGCSV_MAGIC_CATEGORY_ID}/groups"
    payload = tcgcsv_get_json(session, url)
    return payload.get("results", [])


def get_tcgcsv_products_for_group(session, group_id):
    url = f"{TCGCSV_BASE_URL}/{TCGCSV_MAGIC_CATEGORY_ID}/{group_id}/products"
    payload = tcgcsv_get_json(session, url)
    return payload.get("results", [])


def get_tcgcsv_prices_for_group(session, group_id):
    url = f"{TCGCSV_BASE_URL}/{TCGCSV_MAGIC_CATEGORY_ID}/{group_id}/prices"
    payload = tcgcsv_get_json(session, url)
    return payload.get("results", [])


def _fetch_group_prices_for_bulk(
    session,
    group_id,
    timeout=TCGCSV_BULK_TIMEOUT_SECONDS,
    max_attempts=TCGCSV_BULK_MAX_ATTEMPTS,
):
    """
    Isolated retry logic for bulk snapshot downloads.

    Deliberately does NOT read or set the global _TCGCSV_COOLDOWN_UNTIL so
    that one unresponsive group cannot silently abort the remaining 400+
    group iterations via the cooldown fast-path.
    """
    url = f"{TCGCSV_BASE_URL}/{TCGCSV_MAGIC_CATEGORY_ID}/{group_id}/prices"

    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(
                url,
                timeout=timeout,
                headers={**TCGCSV_HEADERS, "Connection": "close"},
            )
            if response.status_code == 200:
                return response.json().get("results", [])
        except (ConnectionError, Timeout, ChunkedEncodingError, RequestException):
            pass

        if attempt < max_attempts:
            time.sleep(min(1.5 * attempt, 4))

    return []

def load_local_group_prices(snapshot_path=None):
    snapshot_path = resolve_local_price_snapshot_path(snapshot_path)

    if not os.path.exists(snapshot_path):
        return {}

    mtime = os.path.getmtime(snapshot_path)

    if (
        _LOCAL_SNAPSHOT_CACHE["path"] == snapshot_path
        and _LOCAL_SNAPSHOT_CACHE["mtime"] == mtime
    ):
        return _LOCAL_SNAPSHOT_CACHE["group_prices"]

    grouped = defaultdict(list)

    with open(snapshot_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            normalized = normalize_local_price_snapshot_row(row)
            if normalized is None:
                continue

            group_id = normalized.pop("group_id")
            grouped[group_id].append(normalized)

    _LOCAL_SNAPSHOT_CACHE["path"] = snapshot_path
    _LOCAL_SNAPSHOT_CACHE["mtime"] = mtime
    _LOCAL_SNAPSHOT_CACHE["group_prices"] = dict(grouped)

    return _LOCAL_SNAPSHOT_CACHE["group_prices"]

def int_or_none(value):
    if value in (None, ""):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_product_to_group_map_from_local_snapshot(snapshot_path=None):
    """
    Build productId -> group_id from the local CSV.

    This lets local-only mode resolve missing tcgcsv_group_id without touching
    the live TCGCSV API.
    """
    grouped_prices = load_local_group_prices(snapshot_path=snapshot_path)
    product_to_group = {}

    for group_id, prices in grouped_prices.items():
        for price_obj in prices:
            product_id = int_or_none(price_obj.get("productId"))
            if product_id is not None:
                product_to_group[product_id] = str(group_id)

    return product_to_group

def get_tcgcsv_history_point_count(manager, scryfall_id):
    row = manager.cursor.execute("""
        SELECT COUNT(DISTINCT scraped_at) AS point_count
        FROM price_history
        WHERE scryfall_id = ?
          AND source = 'tcgcsv'
          AND scraped_at IS NOT NULL
    """, (scryfall_id,)).fetchone()

    return row["point_count"] if row else 0


def build_prices_by_product_id(prices):
    prices_by_product_id = defaultdict(dict)

    for price_obj in prices:
        product_id = price_obj.get("productId")
        finish = price_finish_from_subtype(price_obj.get("subTypeName"))
        selected_price = choose_tcgcsv_price(price_obj)

        if product_id and selected_price is not None:
            prices_by_product_id[product_id][finish] = selected_price

    return prices_by_product_id


def prices_for_target(target, prices_by_product_id):
    normal_product_id = int_or_none(target.get("normal_product_id"))
    etched_product_id = int_or_none(target.get("etched_product_id"))
    inventory_finish = target.get("finish")
    has_price_override = bool(target.get("has_price_override", False))

    nonfoil_price = None
    foil_price = None

    if normal_product_id is not None:
        product_prices = prices_by_product_id.get(normal_product_id, {})

        if has_price_override and is_foil_like_finish(inventory_finish):
            foil_price = product_prices.get("foil") or product_prices.get("nonfoil")
        else:
            nonfoil_price = product_prices.get("nonfoil")
            foil_price = product_prices.get("foil")

    if etched_product_id is not None:
        etched_prices = prices_by_product_id.get(etched_product_id, {})
        foil_price = etched_prices.get("foil") or etched_prices.get("nonfoil") or foil_price

    return nonfoil_price, foil_price


def backfill_prior_history_if_needed(
    manager,
    session,
    grouped_targets,
    updated_scryfall_ids,
    current_snapshot_path,
    min_history_points=2,
):
    """
    Backfill price_history from older local CSVs until updated cards have at
    least min_history_points TCGCSV dates.

    This is anchored to the current snapshot file, not today's date. If the
    current import uses tcg_history/prices_category_1_2026-06-26.csv, this
    searches older available files such as 2026-06-25, then 2026-06-24.
    """
    current_price_date = parse_tcgcsv_history_file_date(current_snapshot_path)
    if current_price_date is None:
        return 0

    prior_snapshot_paths = find_prior_tcgcsv_history_files(current_price_date)
    if not prior_snapshot_paths:
        return 0

    remaining_scryfall_ids = set(updated_scryfall_ids)
    backfilled = 0

    for prior_snapshot_path in prior_snapshot_paths:
        if not remaining_scryfall_ids:
            break

        prior_price_date = parse_tcgcsv_history_file_date(prior_snapshot_path)
        if prior_price_date is None:
            continue

        for group_id, targets_by_key in grouped_targets.items():
            if not remaining_scryfall_ids:
                break

            prices = get_tcgcsv_prices_for_group_with_fallback(
                session,
                group_id,
                data_source=TCGCSV_SOURCE_LOCAL_ONLY,
                snapshot_path=prior_snapshot_path,
            )
            prices_by_product_id = build_prices_by_product_id(prices)

            for target in targets_by_key.values():
                scryfall_id = target.get("scryfall_id")

                if scryfall_id not in remaining_scryfall_ids:
                    continue

                if get_tcgcsv_history_point_count(manager, scryfall_id) >= min_history_points:
                    remaining_scryfall_ids.discard(scryfall_id)
                    continue

                nonfoil_price, foil_price = prices_for_target(target, prices_by_product_id)

                if nonfoil_price is None and foil_price is None:
                    continue

                manager.cursor.execute("""
                    INSERT INTO price_history (
                        scryfall_id,
                        price_usd,
                        price_foil,
                        scraped_at,
                        source
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    scryfall_id,
                    nonfoil_price,
                    foil_price,
                    prior_price_date.isoformat(),
                    "tcgcsv",
                ))

                backfilled += 1

                if get_tcgcsv_history_point_count(manager, scryfall_id) >= min_history_points:
                    remaining_scryfall_ids.discard(scryfall_id)

    return backfilled

def get_tcgcsv_prices_for_group_with_fallback(
    session,
    group_id,
    data_source=TCGCSV_SOURCE_LOCAL_ONLY,
    snapshot_path=None,
):
    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        return load_local_group_prices(snapshot_path=snapshot_path).get(str(group_id), [])

    try:
        return get_tcgcsv_prices_for_group(session, group_id)
    except Exception:
        local = load_local_group_prices(snapshot_path=snapshot_path).get(str(group_id), [])
        if local:
            return local
        raise


def stream_export_daily_price_snapshot(
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    snapshot_last_updated=None,
    max_duration_seconds=None,
):
    """
    Generator that downloads the full TCGCSV MTG price snapshot group-by-group
    and yields (groups_done, groups_total, groups_skipped) progress tuples.

    Writes to a temp file and renames atomically on completion so that a
    mid-run failure never overwrites a good existing snapshot.

    Uses _fetch_group_prices_for_bulk which deliberately bypasses the global
    cooldown so that one failed group cannot silently abort the remaining ~450.
    """
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    tmp_path = snapshot_path + ".tmp"

    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    timestamp = snapshot_last_updated or tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)
    groups = get_tcgcsv_magic_groups(session)
    total = len(groups)
    skipped = 0
    groups_with_prices = 0

    yield (0, total, skipped)

    captured_rows = 0

    started = time.monotonic()

    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "snapshot_last_updated",
                "group_id",
                "product_id",
                "sub_type_name",
                "market_price",
                "mid_price",
                "low_price",
                "selected_price",
                "captured_at",
            ])

            for index, group in enumerate(groups, start=1):
                if max_duration_seconds and (time.monotonic() - started) > max_duration_seconds:
                    raise TimeoutError(
                        f"Snapshot refresh exceeded {max_duration_seconds}s after {index - 1}/{total} groups"
                    )

                group_id = group.get("groupId")
                if not group_id:
                    skipped += 1
                    yield (index, total, skipped)
                    continue

                prices = _fetch_group_prices_for_bulk(session, group_id)

                if not prices:
                    skipped += 1
                else:
                    groups_with_prices += 1
                    now = datetime.utcnow().isoformat(timespec="seconds")
                    for price_obj in prices:
                        writer.writerow([
                            timestamp,
                            group_id,
                            price_obj.get("productId"),
                            price_obj.get("subTypeName") or "",
                            price_obj.get("marketPrice"),
                            price_obj.get("midPrice"),
                            price_obj.get("lowPrice"),
                            choose_tcgcsv_price(price_obj),
                            now,
                        ])
                        captured_rows += 1

                time.sleep(TCGCSV_BULK_RATE_LIMIT_DELAY)
                yield (index, total, skipped)

        if (
            groups_with_prices < TCGCSV_MIN_GROUPS_WITH_PRICES
            or captured_rows < TCGCSV_MIN_ROWS_FOR_VALID_SNAPSHOT
        ):
            raise RuntimeError(
                "TCGCSV snapshot appears incomplete "
                f"(groups_with_prices={groups_with_prices}, rows={captured_rows})."
            )

        os.replace(tmp_path, snapshot_path)
        _LOCAL_SNAPSHOT_CACHE["path"] = None
        _LOCAL_SNAPSHOT_CACHE["mtime"] = None
        write_snapshot_metadata(last_updated=timestamp, snapshot_path=snapshot_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def export_daily_price_snapshot(
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    snapshot_last_updated=None,
    max_duration_seconds=None,
):
    """
    Downloads today's TCGCSV price payload into a local CSV snapshot.
    Drains the streaming generator to completion and returns the snapshot path.
    """
    for _ in stream_export_daily_price_snapshot(
        snapshot_path=snapshot_path,
        snapshot_last_updated=snapshot_last_updated,
        max_duration_seconds=max_duration_seconds,
    ):
        pass

    return snapshot_path


def refresh_daily_price_snapshot_if_needed(
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    force=False,
    dry_run=False,
):
    snapshot_path = snapshot_path or TCGCSV_LOCAL_PRICE_SNAPSHOT

    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    remote_last_updated = get_remote_tcgcsv_last_updated(session=session)
    needs_refresh = force or should_refresh_local_snapshot(
        remote_last_updated=remote_last_updated,
        snapshot_path=snapshot_path,
    )

    if dry_run:
        return {
            "status": "would_update" if needs_refresh else "unchanged",
            "snapshot_path": snapshot_path,
            "remote_last_updated": remote_last_updated,
            "updated": False,
        }

    if not needs_refresh:
        return {
            "status": "unchanged",
            "snapshot_path": snapshot_path,
            "remote_last_updated": remote_last_updated,
            "updated": False,
        }

    written_path = export_daily_price_snapshot(
        snapshot_path=snapshot_path,
        snapshot_last_updated=remote_last_updated,
    )

    return {
        "status": "updated",
        "snapshot_path": written_path,
        "remote_last_updated": remote_last_updated,
        "updated": True,
    }


def stream_refresh_daily_price_snapshot_if_needed(
    snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT,
    force=False,
    max_duration_seconds=None,
):
    """
    Generator variant of refresh_daily_price_snapshot_if_needed.

    Yields (done, total, skipped, remote_last_updated, status) tuples where
    status is 'downloading', 'unchanged', or 'complete'.

    Callers (e.g. the SSE market sync route) drive the generator to stream
    real-time download progress to the browser.
    """
    snapshot_path = snapshot_path or TCGCSV_LOCAL_PRICE_SNAPSHOT

    remote_last_updated = get_remote_tcgcsv_last_updated()
    needs_refresh = force or should_refresh_local_snapshot(
        remote_last_updated=remote_last_updated,
        snapshot_path=snapshot_path,
    )

    if not needs_refresh:
        yield (0, 0, 0, remote_last_updated, "unchanged")
        return

    for (done, total, skipped) in stream_export_daily_price_snapshot(
        snapshot_path=snapshot_path,
        snapshot_last_updated=remote_last_updated,
        max_duration_seconds=max_duration_seconds,
    ):
        yield (done, total, skipped, remote_last_updated, "downloading")

    yield (0, 0, 0, remote_last_updated, "complete")


def get_local_cards_needing_prices(manager):
    return manager.cursor.execute("""
        SELECT DISTINCT
            cp.scryfall_id,
            i.finish,
            COALESCE(o.tcgplayer_id, cp.tcgplayer_id) AS tcgplayer_id,
            cp.tcgplayer_etched_id,
            COALESCE(o.tcgcsv_group_id, cp.tcgcsv_group_id) AS tcgcsv_group_id,
            CASE WHEN o.tcgplayer_id IS NOT NULL THEN 1 ELSE 0 END AS has_price_override
        FROM card_printings cp
        JOIN inventory i
            ON cp.scryfall_id = i.scryfall_id
        LEFT JOIN tcgplayer_price_overrides o
            ON cp.scryfall_id = o.scryfall_id
            AND LOWER(REPLACE(i.finish, '_', ' ')) = LOWER(REPLACE(o.finish, '_', ' '))
        WHERE cp.scryfall_id IN (
            SELECT i2.scryfall_id
            FROM inventory i2
            WHERE i2.scryfall_id IS NOT NULL

            UNION

            SELECT w.scryfall_id
            FROM wishlist w
            WHERE w.scryfall_id IS NOT NULL

            UNION

            SELECT edc.scryfall_id
            FROM edh_deck_cards edc
            WHERE edc.scryfall_id IS NOT NULL

            UNION

            SELECT p.default_scryfall_id AS scryfall_id
            FROM planeswalker_tracker p
            WHERE p.default_scryfall_id IS NOT NULL
        )
        AND (
            COALESCE(o.tcgplayer_id, cp.tcgplayer_id) IS NOT NULL
            OR cp.tcgplayer_etched_id IS NOT NULL
        )
    """).fetchall()


def build_group_map_from_tcgcsv(session, wanted_product_ids, progress_callback=None):
    """
    Builds productId -> groupId for the product IDs we actually care about.

    TCGCSV prices are fetched per group, so we need to know which group each
    TCGplayer product belongs to. We avoid a permanent all-products table here
    to keep the first implementation small.
    """
    product_to_group = {}

    groups = get_tcgcsv_magic_groups(session)
    total_groups = len(groups)

    for index, group in enumerate(groups):
        group_id = group.get("groupId")

        if not group_id:
            continue

        if progress_callback and index % 25 == 0:
            progress_callback(index, total_groups)

        try:
            products = get_tcgcsv_products_for_group(session, group_id)
        except Exception:
            time.sleep(TCGCSV_RATE_LIMIT_DELAY)
            continue

        for product in products:
            product_id = product.get("productId")

            if product_id in wanted_product_ids:
                product_to_group[product_id] = group_id

        if wanted_product_ids.issubset(set(product_to_group.keys())):
            break

        time.sleep(TCGCSV_RATE_LIMIT_DELAY)

    return product_to_group

def _normalize_tcgcsv_text(value):
    if value is None:
        return ""
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    ).strip()


def _canonical_finish_for_tcgcsv_match(value):
    """
    Converts inventory finishes and TCGCSV subTypeName values into the same
    conservative finish vocabulary.

    Important:
    - "rainbow foil" does NOT match "double rainbow foil"
    - "foil" / "normal foil" does NOT match etched, surge, galaxy, etc.
    """
    raw_text = _normalize_tcgcsv_text(value)
    normalized_text = _normalize_tcgcsv_text(normalize_finish(value))
    text = f"{raw_text} {normalized_text}".strip()
    tokens = set(text.split())
    compact = text.replace(" ", "")

    if not text:
        return ""

    if "nonfoil" in compact or ("non" in tokens and "foil" in tokens):
        return "normal"

    if "double" in tokens and "rainbow" in tokens and "foil" in tokens:
        return "double rainbow foil"

    if "rainbow" in tokens and "foil" in tokens:
        return "rainbow foil"

    if "surge" in tokens and "foil" in tokens:
        return "surge foil"

    if "galaxy" in tokens and "foil" in tokens:
        return "galaxy foil"

    if "textured" in tokens and "foil" in tokens:
        return "textured foil"

    if "etched" in tokens:
        return "etched foil"

    if "traditional" in tokens and "foil" in tokens:
        return "foil"

    if "normal" in tokens and "foil" in tokens:
        return "foil"

    if text == "foil" or tokens == {"foil"}:
        return "foil"

    if text == "normal" or tokens == {"normal"}:
        return "normal"

    return text


def subtype_matches_finish(subtype_name, requested_finish):
    """
    Returns True only when a TCGCSV subTypeName safely matches the requested
    inventory finish.

    Examples:
    - Rainbow Foil matches rainbow foil
    - Double Rainbow Foil matches double rainbow foil
    - Double Rainbow Foil does NOT match rainbow foil
    - Etched / Etched Foil match etched foil
    - Foil / Normal Foil match normal foil
    - Etched Foil does NOT match normal foil
    """
    requested = _canonical_finish_for_tcgcsv_match(requested_finish)
    subtype = _canonical_finish_for_tcgcsv_match(subtype_name)

    if requested not in SUPPORTED_TCGCSV_FINISHES:
        return False

    return subtype == requested

def normalize_finish(value):
    return (value or "nonfoil").strip().lower().replace("_", " ")


def is_foil_like_finish(value):
    finish = normalize_finish(value)
    return "foil" in finish or "etched" in finish or "rainbow" in finish


def product_finish_from_name(product_name):
    name = normalize_finish(product_name)

    if any(marker in name for marker in SPECIAL_FINISHES):
        return "foil"

    if "foil" in name:
        return "foil"

    return "nonfoil"

def _row_get(row, *keys, default=None):
    """
    Reads from dict-like rows, sqlite3.Row, or similar objects while tolerating
    productId/product_id style naming differences.
    """
    for key in keys:
        try:
            value = row[key]
            if value is not None:
                return value
        except (KeyError, IndexError, TypeError):
            pass

    try:
        row_keys = row.keys()
    except AttributeError:
        return default

    lower_key_map = {str(key).lower(): key for key in row_keys}

    for key in keys:
        matched_key = lower_key_map.get(str(key).lower())
        if matched_key is None:
            continue

        try:
            value = row[matched_key]
            if value is not None:
                return value
        except (KeyError, IndexError, TypeError):
            pass

    return default


def _coerce_int(value):
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_price(value):
    if value is None:
        return None

    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
        if value == "" or value.lower() in {"none", "null", "nan"}:
            return None

    try:
        price = float(value)
    except (TypeError, ValueError):
        return None

    return price if price > 0 else None


def _row_product_id(row):
    return _coerce_int(
        _row_get(row, "productId", "product_id", "productid", "tcgplayer_id")
    )


def _row_group_id(row):
    return _coerce_int(
        _row_get(row, "group_id", "groupId", "groupid", "tcgcsv_group_id")
    )


def _row_subtype_name(row):
    return str(
        _row_get(
            row,
            "subTypeName",
            "subtypeName",
            "subtype_name",
            "subtypename",
            default=""
        ) or ""
    ).strip()


def _row_price(row, canonical_price_key):
    return _coerce_price(
        _row_get(row, *PRICE_FIELD_ALIASES[canonical_price_key])
    )


def _best_usable_price(row):
    for price_key in (
        "market_price",
        "mid_price",
        "low_price",
        "direct_low_price",
        "high_price",
    ):
        price = _row_price(row, price_key)
        if price is not None:
            return price

    return None


def _row_has_usable_price(row):
    return _best_usable_price(row) is not None


def _candidate_from_price_row(row):
    return {
        "product_id": _row_product_id(row),
        "group_id": _row_group_id(row),
        "subtype_name": _row_subtype_name(row),
        "low_price": _row_price(row, "low_price"),
        "mid_price": _row_price(row, "mid_price"),
        "high_price": _row_price(row, "high_price"),
        "market_price": _row_price(row, "market_price"),
        "direct_low_price": _row_price(row, "direct_low_price"),
        "usable_price": _best_usable_price(row),
        "has_usable_price": _row_has_usable_price(row),
    }


def _dedupe_candidates_by_product_id(rows):
    """
    TCGCSV should usually have one row per product/finish in this context,
    but this keeps the result stable if duplicated rows appear.
    """
    candidates_by_product_id = {}

    for row in rows:
        candidate = _candidate_from_price_row(row)
        product_id = candidate["product_id"]

        if product_id is None:
            continue

        existing = candidates_by_product_id.get(product_id)

        if (
            existing is None
            or candidate["has_usable_price"] and not existing["has_usable_price"]
        ):
            candidates_by_product_id[product_id] = candidate

    return list(candidates_by_product_id.values())


def _finish_lookup_result(
    status,
    reason,
    base_product_id,
    requested_finish,
    product_id=None,
    group_id=None,
    candidates=None,
):
    return {
        "status": status,
        "reason": reason,
        "base_product_id": base_product_id,
        "requested_finish": _canonical_finish_for_tcgcsv_match(requested_finish),
        "product_id": product_id,
        "group_id": group_id,
        "candidates": candidates or [],
    }


def find_finish_product_id_from_local_prices(
    base_product_id,
    requested_finish,
    snapshot_path=None,
):
    """
    Conservative local-only lookup for finish-specific TCGPlayer product IDs.

    This uses only the local TCGCSV price snapshot. It does not use product names,
    does not call live TCGCSV/Scryfall endpoints, and does not write to the DB.
    """
    base_product_id = _coerce_int(base_product_id)

    if base_product_id is None:
        return _finish_lookup_result(
            "base_product_not_found",
            "Base product ID was empty or not an integer.",
            base_product_id,
            requested_finish,
        )

    price_rows = list(_iter_local_price_rows_with_group_id(snapshot_path=snapshot_path))

    base_rows = [
        row for row in price_rows
        if _row_product_id(row) == base_product_id
    ]

    if not base_rows:
        return _finish_lookup_result(
            "base_product_not_found",
            f"Base product ID {base_product_id} was not found in the local TCGCSV snapshot.",
            base_product_id,
            requested_finish,
        )

    group_ids = sorted({
        _row_group_id(row)
        for row in base_rows
        if _row_group_id(row) is not None
    })

    if len(group_ids) != 1:
        return _finish_lookup_result(
            "ambiguous",
            f"Base product ID {base_product_id} did not resolve to exactly one group_id.",
            base_product_id,
            requested_finish,
            candidates=_dedupe_candidates_by_product_id(base_rows),
        )

    group_id = group_ids[0]

    group_rows = [
        row for row in price_rows
        if _row_group_id(row) == group_id
    ]

    matching_finish_rows = [
        row for row in group_rows
        if subtype_matches_finish(_row_subtype_name(row), requested_finish)
    ]

    candidates = _dedupe_candidates_by_product_id(matching_finish_rows)

    if not candidates:
        return _finish_lookup_result(
            "no_finish_candidate",
            f"No local TCGCSV rows in group {group_id} matched finish {requested_finish!r}.",
            base_product_id,
            requested_finish,
            group_id=group_id,
        )

    same_product_candidates = [
        candidate for candidate in candidates
        if candidate["product_id"] == base_product_id
    ]

    if same_product_candidates:
        usable_same_product_candidates = [
            candidate for candidate in same_product_candidates
            if candidate["has_usable_price"]
        ]

        if usable_same_product_candidates:
            candidate = usable_same_product_candidates[0]

            return _finish_lookup_result(
                "matched_same_product",
                "Base product ID already has a matching finish row with a usable price.",
                base_product_id,
                requested_finish,
                product_id=candidate["product_id"],
                group_id=group_id,
                candidates=candidates,
            )

        return _finish_lookup_result(
            "no_usable_price",
            "Base product ID has a matching finish row, but it has no usable local price.",
            base_product_id,
            requested_finish,
            group_id=group_id,
            candidates=candidates,
        )

    usable_candidates = [
        candidate for candidate in candidates
        if candidate["has_usable_price"]
    ]

    if not usable_candidates:
        return _finish_lookup_result(
            "no_usable_price",
            f"Group {group_id} has matching finish candidate rows, but none has a usable local price.",
            base_product_id,
            requested_finish,
            group_id=group_id,
            candidates=candidates,
        )

    if len(usable_candidates) == 1:
        candidate = usable_candidates[0]

        return _finish_lookup_result(
            "matched_single_candidate",
            "Exactly one matching finish candidate in the base product group has a usable price.",
            base_product_id,
            requested_finish,
            product_id=candidate["product_id"],
            group_id=group_id,
            candidates=candidates,
        )

    return _finish_lookup_result(
        "ambiguous",
        f"Group {group_id} has multiple matching finish candidates with usable prices.",
        base_product_id,
        requested_finish,
        group_id=group_id,
        candidates=usable_candidates,
    )
    
def _finish_filter_sql_and_params(finish):
    if finish:
        return "AND LOWER(REPLACE(i.finish, '_', ' ')) = ?", [normalize_finish(finish)]

    special_finishes = sorted(SPECIAL_FINISHES)
    placeholders = ",".join("?" for _ in special_finishes)

    return (
        f"AND LOWER(REPLACE(i.finish, '_', ' ')) IN ({placeholders})",
        special_finishes,
    )


def get_inventory_special_finish_rows(manager, finish=None):
    """
    Returns distinct inventory card/finish rows that may need finish-specific
    TCGPlayer price overrides.

    This does not mutate the DB.
    """
    finish_sql, finish_params = _finish_filter_sql_and_params(finish)

    return manager.cursor.execute(f"""
        SELECT DISTINCT
            cp.scryfall_id,
            cd.name,
            cp.set_code,
            cp.collector_number,
            i.finish,
            cp.tcgplayer_id AS base_tcgplayer_id,
            cp.tcgcsv_group_id AS base_tcgcsv_group_id,
            o.tcgplayer_id AS existing_override_tcgplayer_id,
            o.tcgcsv_group_id AS existing_override_group_id,
            o.note AS existing_override_note
        FROM inventory i
        JOIN card_printings cp
            ON i.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        LEFT JOIN tcgplayer_price_overrides o
            ON cp.scryfall_id = o.scryfall_id
            AND LOWER(REPLACE(i.finish, '_', ' ')) = LOWER(REPLACE(o.finish, '_', ' '))
        WHERE i.scryfall_id IS NOT NULL
          AND cp.tcgplayer_id IS NOT NULL
          {finish_sql}
        ORDER BY
            LOWER(cd.name),
            cp.set_code,
            cp.collector_number,
            LOWER(i.finish)
    """, tuple(finish_params)).fetchall()
    
def tcgcsv_request(session, url, timeout=30, attempts=4):
    global _TCGCSV_COOLDOWN_UNTIL

    if time.time() < _TCGCSV_COOLDOWN_UNTIL:
        raise RuntimeError(
            "TCGCSV request cooldown active after repeated failures. "
            "Use local snapshot fallback until cooldown expires."
        )

    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                url,
                timeout=timeout,
                headers={
                    **TCGCSV_HEADERS,
                    "Connection": "close",
                }
            )

            if response.status_code == 200:
                return response

            last_error = RuntimeError(
                f"TCGCSV returned status {response.status_code} for {url}"
            )

        except (ConnectionError, Timeout, ChunkedEncodingError, RequestException) as error:
            last_error = error

        sleep_seconds = min(1.5 * attempt, 6)
        time.sleep(sleep_seconds)

    _TCGCSV_COOLDOWN_UNTIL = time.time() + TCGCSV_REQUEST_COOLDOWN_SECONDS
    raise RuntimeError(f"TCGCSV request failed after {attempts} attempts: {last_error}")

def search_tcgcsv_products_for_finish(manager, scryfall_id, finish):
    """
    Returns candidate TCGplayer products for a special finish.
    Use this for the modal button: Find matching TCGplayer product.
    """
    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    row = manager.cursor.execute("""
        SELECT
            cp.scryfall_id,
            cp.tcgplayer_id,
            cp.tcgplayer_etched_id,
            cp.tcgcsv_group_id,
            cd.name
        FROM card_printings cp
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE cp.scryfall_id = ?
    """, (scryfall_id,)).fetchone()

    if not row:
        return []

    group_id = row["tcgcsv_group_id"]

    if not group_id:
        wanted_ids = set()
        if row["tcgplayer_id"]:
            wanted_ids.add(row["tcgplayer_id"])
        if row["tcgplayer_etched_id"]:
            wanted_ids.add(row["tcgplayer_etched_id"])

        if wanted_ids:
            group_map = build_group_map_from_tcgcsv(session, wanted_ids)
            group_id = next(iter(group_map.values()), None)

            if group_id:
                manager.cursor.execute("""
                    UPDATE card_printings
                    SET tcgcsv_group_id = ?
                    WHERE scryfall_id = ?
                """, (group_id, scryfall_id))
                manager.commit()

    if not group_id:
        return []

    products = get_tcgcsv_products_for_group(session, group_id)

    card_name = normalize_finish(row["name"])
    requested_finish = normalize_finish(finish)

    candidates = []

    for product in products:
        product_name = product.get("name") or ""
        clean_name = product.get("cleanName") or ""
        normalized_product_name = normalize_finish(product_name)
        normalized_clean_name = normalize_finish(clean_name)

        # Local card name, usually from card_definitions
        local_name = card_name

        name_matches = (
            local_name in normalized_product_name
            or local_name in normalized_clean_name
            or normalized_product_name in local_name
            or normalized_clean_name in local_name
        )

        finish_matches = (
            requested_finish in normalized_product_name
            or requested_finish in normalized_clean_name
        )

        # For special finishes, do NOT accept finish-only matches.
        # That is how unrelated Rainbow Foil products sneak into the prompt.
        if name_matches and finish_matches:
            candidates.append({
                "productId": product.get("productId"),
                "groupId": product.get("groupId") or group_id,
                "name": product_name,
                "cleanName": clean_name,
                "url": product.get("url"),
                "imageUrl": product.get("imageUrl"),
            })

    return candidates

def get_grouped_local_price_targets(
    manager,
    session,
    local_rows,
    allow_remote_group_lookup=False,
    snapshot_path=None,
):
    wanted_product_ids = set()

    for row in local_rows:
        tcgplayer_id = int_or_none(row["tcgplayer_id"])
        etched_id = int_or_none(row["tcgplayer_etched_id"])

        if tcgplayer_id is not None:
            wanted_product_ids.add(tcgplayer_id)
        if etched_id is not None:
            wanted_product_ids.add(etched_id)

    product_to_group = {}

    for row in local_rows:
        tcgplayer_id = int_or_none(row["tcgplayer_id"])
        etched_id = int_or_none(row["tcgplayer_etched_id"])
        group_id = row["tcgcsv_group_id"]

        if group_id and tcgplayer_id is not None:
            product_to_group[tcgplayer_id] = str(group_id)

        if group_id and etched_id is not None:
            product_to_group[etched_id] = str(group_id)

    missing_product_ids = wanted_product_ids - set(product_to_group.keys())

    if missing_product_ids and snapshot_path:
        local_product_to_group = build_product_to_group_map_from_local_snapshot(
            snapshot_path=snapshot_path
        )

        for product_id in missing_product_ids:
            group_id = local_product_to_group.get(product_id)
            if group_id:
                product_to_group[product_id] = group_id

        missing_product_ids = wanted_product_ids - set(product_to_group.keys())

    if missing_product_ids and allow_remote_group_lookup:
        found_map = build_group_map_from_tcgcsv(session, missing_product_ids)
        found_map = {
            int_or_none(product_id): str(group_id)
            for product_id, group_id in found_map.items()
            if int_or_none(product_id) is not None and group_id
        }

        product_to_group.update(found_map)

        for product_id, group_id in found_map.items():
            manager.cursor.execute("""
                UPDATE card_printings
                SET tcgcsv_group_id = ?
                WHERE tcgplayer_id = ?
                   OR tcgplayer_etched_id = ?
            """, (group_id, product_id, product_id))

            manager.cursor.execute("""
                UPDATE tcgplayer_price_overrides
                SET tcgcsv_group_id = ?
                WHERE tcgplayer_id = ?
                  AND (tcgcsv_group_id IS NULL OR tcgcsv_group_id = '')
            """, (group_id, product_id))

        manager.commit()

    grouped_targets = defaultdict(dict)

    for row in local_rows:
        scryfall_id = row["scryfall_id"]
        finish = row["finish"]
        normal_product_id = int_or_none(row["tcgplayer_id"])
        etched_product_id = int_or_none(row["tcgplayer_etched_id"])
        has_price_override = bool(row["has_price_override"])

        group_id = None

        if normal_product_id:
            group_id = product_to_group.get(normal_product_id)

        if not group_id and etched_product_id:
            group_id = product_to_group.get(etched_product_id)

        if not group_id:
            continue

        target_key = f"{scryfall_id}|{normalize_finish(finish)}"

        grouped_targets[group_id][target_key] = {
            "scryfall_id": scryfall_id,
            "finish": finish,
            "normal_product_id": normal_product_id,
            "etched_product_id": etched_product_id,
            "has_price_override": has_price_override,
        }

    return grouped_targets

def update_single_card_price_from_tcgcsv(
    manager,
    scryfall_id,
    data_source=TCGCSV_SOURCE_LOCAL_ONLY,
    allow_remote_group_lookup=False,
):
    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    current_snapshot_path = None
    tcgcsv_scraped_at = datetime.utcnow().date().isoformat()

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        current_snapshot_path = resolve_local_price_snapshot_path()
        tcgcsv_timestamp = (
            get_local_snapshot_last_updated(snapshot_path=current_snapshot_path)
            or datetime.utcnow().isoformat(timespec="seconds")
        )
        tcgcsv_scraped_at = get_price_date_for_snapshot(current_snapshot_path)
    else:
        try:
            tcgcsv_timestamp = tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)
            tcgcsv_scraped_at = tcgcsv_timestamp[:10]
        except Exception:
            tcgcsv_timestamp = datetime.utcnow().isoformat(timespec="seconds")
            tcgcsv_scraped_at = datetime.utcnow().date().isoformat()

    rows = manager.cursor.execute("""
        SELECT DISTINCT
            cp.scryfall_id,
            i.finish,
            COALESCE(o.tcgplayer_id, cp.tcgplayer_id) AS tcgplayer_id,
            cp.tcgplayer_etched_id,
            COALESCE(o.tcgcsv_group_id, cp.tcgcsv_group_id) AS tcgcsv_group_id,
            CASE WHEN o.tcgplayer_id IS NOT NULL THEN 1 ELSE 0 END AS has_price_override
        FROM card_printings cp
        JOIN inventory i
            ON cp.scryfall_id = i.scryfall_id
        LEFT JOIN tcgplayer_price_overrides o
            ON cp.scryfall_id = o.scryfall_id
            AND LOWER(REPLACE(i.finish, '_', ' ')) = LOWER(REPLACE(o.finish, '_', ' '))
        WHERE cp.scryfall_id = ?
        AND (
            COALESCE(o.tcgplayer_id, cp.tcgplayer_id) IS NOT NULL
            OR cp.tcgplayer_etched_id IS NOT NULL
        )
    """, (scryfall_id,)).fetchall()

    if not rows:
        return False

    grouped_targets = get_grouped_local_price_targets(
        manager,
        session,
        rows,
        allow_remote_group_lookup=allow_remote_group_lookup,
        snapshot_path=current_snapshot_path,
    )

    updated_scryfall_ids = set()

    for group_id, targets_by_key in grouped_targets.items():
        prices = get_tcgcsv_prices_for_group_with_fallback(
            session,
            group_id,
            data_source=data_source,
            snapshot_path=current_snapshot_path,
        )
        prices_by_product_id = build_prices_by_product_id(prices)

        for target in targets_by_key.values():
            if target["scryfall_id"] != scryfall_id:
                continue

            nonfoil_price, foil_price = prices_for_target(target, prices_by_product_id)

            if nonfoil_price is None and foil_price is None:
                continue

            manager.cursor.execute("""
                UPDATE card_printings
                SET current_price = COALESCE(?, current_price),
                    current_price_foil = COALESCE(?, current_price_foil),
                    tcgcsv_last_price_sync = ?
                WHERE scryfall_id = ?
            """, (nonfoil_price, foil_price, tcgcsv_timestamp, scryfall_id))

            manager.cursor.execute("""
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                scryfall_id,
                nonfoil_price,
                foil_price,
                tcgcsv_scraped_at,
                "tcgcsv",
            ))

            updated_scryfall_ids.add(scryfall_id)

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY and current_snapshot_path and updated_scryfall_ids:
        backfill_prior_history_if_needed(
            manager=manager,
            session=session,
            grouped_targets=grouped_targets,
            updated_scryfall_ids=updated_scryfall_ids,
            current_snapshot_path=current_snapshot_path,
            min_history_points=2,
        )

    return bool(updated_scryfall_ids)

def _first_present(row, *names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def normalize_local_price_snapshot_row(row):
    """
    Normalize either local CSV shape into the internal price object shape.

    Supports:
    - old local snapshot: product_id, sub_type_name, market_price, mid_price, low_price
    - archive export: productId, subTypeName, marketPrice, midPrice, lowPrice, highPrice, directLowPrice
    """
    product_id = _first_present(row, "product_id", "productId")
    group_id = _first_present(row, "group_id", "groupId")
    subtype_name = _first_present(row, "sub_type_name", "subTypeName") or ""

    if not product_id or not group_id:
        return None

    try:
        product_id = int(product_id)
    except (TypeError, ValueError):
        return None

    return {
        "group_id": str(group_id),
        "productId": product_id,
        "subTypeName": subtype_name,
        "marketPrice": _first_present(row, "market_price", "marketPrice"),
        "midPrice": _first_present(row, "mid_price", "midPrice"),
        "lowPrice": _first_present(row, "low_price", "lowPrice"),
        "highPrice": _first_present(row, "high_price", "highPrice"),
        "directLowPrice": _first_present(row, "direct_low_price", "directLowPrice"),
    }

def update_prices_for_scryfall_ids_from_tcgcsv(
    manager,
    scryfall_ids,
    data_source=TCGCSV_SOURCE_LOCAL_ONLY,
    allow_remote_group_lookup=False,
):
    """
    Batch updates prices for explicit card printings, even if they are not yet
    present in inventory.

    Local-only mode reads the latest exported tcg_history CSV, writes that file's
    date into price_history.scraped_at, and then tries to backfill the previous
    day for cards that still have fewer than two TCGCSV history points.
    """
    if not scryfall_ids:
        return 0

    unique_ids = [sid for sid in dict.fromkeys(scryfall_ids) if sid]
    if not unique_ids:
        return 0

    placeholders = ",".join("?" for _ in unique_ids)
    query = f"""
        SELECT
            cp.scryfall_id,
            'nonfoil' AS finish,
            cp.tcgplayer_id,
            cp.tcgplayer_etched_id,
            cp.tcgcsv_group_id,
            0 AS has_price_override
        FROM card_printings cp
        WHERE cp.scryfall_id IN ({placeholders})
          AND (cp.tcgplayer_id IS NOT NULL OR cp.tcgplayer_etched_id IS NOT NULL)
    """

    rows = manager.cursor.execute(query, tuple(unique_ids)).fetchall()
    if not rows:
        return 0

    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    current_snapshot_path = None
    tcgcsv_scraped_at = datetime.utcnow().date().isoformat()

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        current_snapshot_path = resolve_local_price_snapshot_path()
        tcgcsv_timestamp = (
            get_local_snapshot_last_updated(snapshot_path=current_snapshot_path)
            or datetime.utcnow().isoformat(timespec="seconds")
        )
        tcgcsv_scraped_at = get_price_date_for_snapshot(current_snapshot_path)
    else:
        try:
            tcgcsv_timestamp = tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)
            tcgcsv_scraped_at = tcgcsv_timestamp[:10]
        except Exception:
            tcgcsv_timestamp = datetime.utcnow().isoformat(timespec="seconds")
            tcgcsv_scraped_at = datetime.utcnow().date().isoformat()

    grouped_targets = get_grouped_local_price_targets(
        manager,
        session,
        rows,
        allow_remote_group_lookup=allow_remote_group_lookup,
        snapshot_path=current_snapshot_path,
    )

    updated_cards = 0
    updated_scryfall_ids = set()

    for group_id, targets_by_key in grouped_targets.items():
        prices = get_tcgcsv_prices_for_group_with_fallback(
            session,
            group_id,
            data_source=data_source,
            snapshot_path=current_snapshot_path,
        )
        prices_by_product_id = build_prices_by_product_id(prices)

        for target in targets_by_key.values():
            scryfall_id = target.get("scryfall_id")
            nonfoil_price, foil_price = prices_for_target(target, prices_by_product_id)

            if nonfoil_price is None and foil_price is None:
                continue

            manager.cursor.execute("""
                UPDATE card_printings
                SET current_price = COALESCE(?, current_price),
                    current_price_foil = COALESCE(?, current_price_foil),
                    tcgcsv_last_price_sync = ?
                WHERE scryfall_id = ?
            """, (nonfoil_price, foil_price, tcgcsv_timestamp, scryfall_id))

            manager.cursor.execute("""
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at,
                    source
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                scryfall_id,
                nonfoil_price,
                foil_price,
                tcgcsv_scraped_at,
                "tcgcsv",
            ))

            updated_cards += 1
            updated_scryfall_ids.add(scryfall_id)

        time.sleep(TCGCSV_RATE_LIMIT_DELAY)

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY and current_snapshot_path:
        backfill_prior_history_if_needed(
            manager=manager,
            session=session,
            grouped_targets=grouped_targets,
            updated_scryfall_ids=updated_scryfall_ids,
            current_snapshot_path=current_snapshot_path,
            min_history_points=2,
        )

    return updated_cards

def _iter_local_price_rows_with_group_id(snapshot_path=None):
    """
    Flatten load_local_group_prices() from:
        {group_id: [price_row, price_row]}

    into rows that also carry group_id, because load_local_group_prices()
    intentionally pops group_id off the normalized row before grouping.
    """
    grouped_prices = load_local_group_prices(snapshot_path=snapshot_path)

    for group_id, rows in grouped_prices.items():
        for row in rows:
            row_with_group = dict(row)
            row_with_group["group_id"] = group_id
            yield row_with_group
            
def preview_local_finish_product_mappings(manager, finish="rainbow foil", snapshot_path=None):
    """
    Preview special-finish TCGPlayer product mappings from local TCGCSV data.

    This does not write to the DB.
    """
    rows = get_inventory_special_finish_rows(manager, finish=finish)
    results = []

    for row in rows:
        normalized_finish = normalize_finish(row["finish"])
        existing_override_id = int_or_none(row["existing_override_tcgplayer_id"])

        if existing_override_id is not None:
            lookup = {
                "status": "existing_override",
                "reason": "A finish-specific TCGPlayer price override already exists.",
                "base_product_id": int_or_none(row["base_tcgplayer_id"]),
                "requested_finish": normalized_finish,
                "product_id": existing_override_id,
                "group_id": int_or_none(row["existing_override_group_id"]),
                "candidates": [],
            }
        else:
            lookup = find_finish_product_id_from_local_prices(
                base_product_id=row["base_tcgplayer_id"],
                requested_finish=normalized_finish,
                snapshot_path=snapshot_path,
            )

        results.append({
            "scryfall_id": row["scryfall_id"],
            "name": row["name"],
            "set_code": row["set_code"],
            "collector_number": row["collector_number"],
            "finish": normalized_finish,
            "base_tcgplayer_id": int_or_none(row["base_tcgplayer_id"]),
            "base_tcgcsv_group_id": int_or_none(row["base_tcgcsv_group_id"]),
            "existing_override_tcgplayer_id": existing_override_id,
            "existing_override_group_id": int_or_none(row["existing_override_group_id"]),
            "status": lookup["status"],
            "reason": lookup["reason"],
            "mapped_tcgplayer_id": lookup["product_id"],
            "mapped_tcgcsv_group_id": lookup["group_id"],
            "candidates": lookup["candidates"],
        })

    return results

def auto_insert_finish_price_overrides(
    manager,
    finish="rainbow foil",
    snapshot_path=None,
    dry_run=True,
):
    """
    Insert safe local-only special-finish mappings into tcgplayer_price_overrides.

    Safe statuses:
    - matched_same_product
    - matched_single_candidate

    This deliberately does not overwrite existing manual overrides.
    """
    preview_rows = preview_local_finish_product_mappings(
        manager=manager,
        finish=finish,
        snapshot_path=snapshot_path,
    )

    safe_statuses = {"matched_same_product", "matched_single_candidate"}

    inserted = 0
    skipped_existing = 0
    skipped_unsafe = 0
    would_insert = []

    for row in preview_rows:
        if row["status"] == "existing_override":
            skipped_existing += 1
            continue

        if row["status"] not in safe_statuses:
            skipped_unsafe += 1
            continue

        mapped_tcgplayer_id = int_or_none(row["mapped_tcgplayer_id"])
        mapped_group_id = int_or_none(row["mapped_tcgcsv_group_id"])

        if mapped_tcgplayer_id is None:
            skipped_unsafe += 1
            continue

        note = (
            f"{AUTO_FINISH_OVERRIDE_NOTE_PREFIX}: "
            f"{row['status']} for {row['finish']}"
        )

        insert_payload = {
            "scryfall_id": row["scryfall_id"],
            "finish": row["finish"],
            "tcgplayer_id": mapped_tcgplayer_id,
            "tcgcsv_group_id": mapped_group_id,
            "note": note,
            "name": row["name"],
            "set_code": row["set_code"],
            "collector_number": row["collector_number"],
        }

        would_insert.append(insert_payload)

        if dry_run:
            continue

        manager.cursor.execute("""
            INSERT INTO tcgplayer_price_overrides (
                scryfall_id,
                finish,
                tcgplayer_id,
                tcgcsv_group_id,
                note
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scryfall_id, finish) DO NOTHING
        """, (
            row["scryfall_id"],
            row["finish"],
            mapped_tcgplayer_id,
            mapped_group_id,
            note,
        ))

        if manager.cursor.rowcount:
            inserted += 1
        else:
            skipped_existing += 1

    if not dry_run:
        manager.commit()

    return {
        "dry_run": dry_run,
        "finish": normalize_finish(finish) if finish else None,
        "previewed": len(preview_rows),
        "inserted": inserted,
        "would_insert": len(would_insert),
        "skipped_existing": skipped_existing,
        "skipped_unsafe": skipped_unsafe,
        "rows": preview_rows,
        "insert_rows": would_insert,
    }
    
def search_local_tcgcsv_price_candidates_for_finish(
    manager,
    scryfall_id,
    finish,
    snapshot_path=None,
    nearby_window=25,
    max_candidates=75,
):
    """
    Local-price-CSV-only candidate finder for manual special-finish mapping.

    This does not use live TCGCSV product endpoints.

    Important limitation:
    The local price CSV has no product names, so this can only return productId,
    group_id, subTypeName, and prices. It is intended for manual/admin review,
    not automatic mapping.
    """
    row = manager.cursor.execute("""
        SELECT
            cp.scryfall_id,
            cp.tcgplayer_id,
            cp.tcgplayer_etched_id,
            cp.tcgcsv_group_id,
            cd.name
        FROM card_printings cp
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE cp.scryfall_id = ?
    """, (scryfall_id,)).fetchone()

    if not row:
        return []

    base_product_id = int_or_none(row["tcgplayer_id"])
    etched_product_id = int_or_none(row["tcgplayer_etched_id"])
    group_id = row["tcgcsv_group_id"]

    if not group_id:
        product_to_group = build_product_to_group_map_from_local_snapshot(
            snapshot_path=snapshot_path
        )

        if base_product_id is not None:
            group_id = product_to_group.get(base_product_id)

        if not group_id and etched_product_id is not None:
            group_id = product_to_group.get(etched_product_id)

    if not group_id:
        return []

    group_id = str(group_id)
    grouped_prices = load_local_group_prices(snapshot_path=snapshot_path)
    group_rows = grouped_prices.get(group_id, [])

    if not group_rows:
        return []

    requested_finish = normalize_finish(finish)
    requested_is_special = requested_finish in SPECIAL_FINISHES

    candidates = []

    for price_row in group_rows:
        product_id = int_or_none(price_row.get("productId"))
        if product_id is None:
            continue

        subtype_name = price_row.get("subTypeName") or ""
        selected_price = choose_tcgcsv_price(price_row)

        if selected_price is None:
            continue

        exact_finish_match = subtype_matches_finish(subtype_name, requested_finish)

        # TCGCSV often stores Secret Lair rainbow foils as generic "Foil".
        # This is only for manual candidate display, not automatic insertion.
        generic_foil_fallback = (
            requested_is_special
            and not exact_finish_match
            and price_finish_from_subtype(subtype_name) == "foil"
        )

        if not exact_finish_match and not generic_foil_fallback:
            continue

        distance_from_base = None
        if base_product_id is not None:
            distance_from_base = abs(product_id - base_product_id)

            # Keeps Secret Lair groups from dumping hundreds/thousands of foil rows.
            # The admin can still manually paste an exact override separately if needed.
            if (
                generic_foil_fallback
                and nearby_window is not None
                and distance_from_base > nearby_window
            ):
                continue

        match_quality = "exact_subtype" if exact_finish_match else "generic_foil_same_group"

        candidates.append({
            "productId": product_id,
            "groupId": group_id,
            "name": (
                f"Local CSV product {product_id} "
                f"({subtype_name or 'Unknown subtype'}, "
                f"market={price_row.get('marketPrice')}, "
                f"mid={price_row.get('midPrice')}, "
                f"low={price_row.get('lowPrice')})"
            ),
            "cleanName": f"Local CSV product {product_id}",
            "url": None,
            "imageUrl": None,
            "subTypeName": subtype_name,
            "marketPrice": price_row.get("marketPrice"),
            "midPrice": price_row.get("midPrice"),
            "lowPrice": price_row.get("lowPrice"),
            "selectedPrice": selected_price,
            "matchQuality": match_quality,
            "distanceFromBaseProductId": distance_from_base,
            "warning": (
                "Local price CSV does not include product names. "
                "Confirm this product ID manually before saving."
            ),
        })

    candidates.sort(key=lambda candidate: (
        0 if candidate["matchQuality"] == "exact_subtype" else 1,
        candidate["distanceFromBaseProductId"]
            if candidate["distanceFromBaseProductId"] is not None
            else 999999,
        candidate["productId"],
    ))

    return candidates[:max_candidates]

def load_local_products_cache(products_path=TCGCSV_LOCAL_PRODUCTS_CACHE):
    if not os.path.exists(products_path):
        return {}, {}

    mtime = os.path.getmtime(products_path)

    if (
        _LOCAL_PRODUCTS_CACHE["path"] == products_path
        and _LOCAL_PRODUCTS_CACHE["mtime"] == mtime
    ):
        return (
            _LOCAL_PRODUCTS_CACHE["by_product_id"],
            _LOCAL_PRODUCTS_CACHE["by_group_id"],
        )

    by_product_id = {}
    by_group_id = defaultdict(list)

    with open(products_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            product_id = int_or_none(row.get("productId"))
            group_id = row.get("group_id") or row.get("groupId")

            if product_id is None or not group_id:
                continue

            product = {
                "productId": product_id,
                "groupId": str(group_id),
                "name": row.get("name") or "",
                "cleanName": row.get("cleanName") or "",
                "url": row.get("url") or "",
                "imageUrl": row.get("imageUrl") or "",
                "modifiedOn": row.get("modifiedOn") or "",
                "extendedData_json": row.get("extendedData_json") or "[]",
            }

            by_product_id[product_id] = product
            by_group_id[str(group_id)].append(product)

    _LOCAL_PRODUCTS_CACHE["path"] = products_path
    _LOCAL_PRODUCTS_CACHE["mtime"] = mtime
    _LOCAL_PRODUCTS_CACHE["by_product_id"] = by_product_id
    _LOCAL_PRODUCTS_CACHE["by_group_id"] = dict(by_group_id)

    return by_product_id, dict(by_group_id)

def search_local_tcgcsv_products_for_finish(
    manager,
    scryfall_id,
    finish,
    snapshot_path=None,
    products_path=TCGCSV_LOCAL_PRODUCTS_CACHE,
):
    row = manager.cursor.execute("""
        SELECT
            cp.scryfall_id,
            cp.tcgplayer_id,
            cp.tcgplayer_etched_id,
            cp.tcgcsv_group_id,
            cd.name
        FROM card_printings cp
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE cp.scryfall_id = ?
    """, (scryfall_id,)).fetchone()

    if not row:
        return []

    card_name = normalize_finish(row["name"])
    requested_finish = normalize_finish(finish)
    base_product_id = int_or_none(row["tcgplayer_id"])
    etched_product_id = int_or_none(row["tcgplayer_etched_id"])

    product_to_group = build_product_to_group_map_from_local_snapshot(
        snapshot_path=snapshot_path
    )

    group_id = row["tcgcsv_group_id"]

    if not group_id and base_product_id is not None:
        group_id = product_to_group.get(base_product_id)

    if not group_id and etched_product_id is not None:
        group_id = product_to_group.get(etched_product_id)

    if not group_id:
        return []

    group_id = str(group_id)

    products_by_id, products_by_group = load_local_products_cache(products_path)
    products = products_by_group.get(group_id, [])

    price_rows = load_local_group_prices(snapshot_path=snapshot_path).get(group_id, [])
    prices_by_id = defaultdict(list)

    for price_row in price_rows:
        product_id = int_or_none(price_row.get("productId"))
        if product_id is not None:
            prices_by_id[product_id].append(price_row)

    candidates = []

    for product in products:
        product_id = int_or_none(product.get("productId"))
        if product_id is None:
            continue

        product_name = normalize_finish(product.get("name"))
        clean_name = normalize_finish(product.get("cleanName"))

        name_matches = (
            card_name in product_name
            or card_name in clean_name
            or product_name in card_name
            or clean_name in card_name
        )

        finish_matches = (
            requested_finish in product_name
            or requested_finish in clean_name
        )

        if not name_matches or not finish_matches:
            continue

        usable_price_rows = [
            price_row
            for price_row in prices_by_id.get(product_id, [])
            if choose_tcgcsv_price(price_row) is not None
        ]

        if not usable_price_rows:
            continue

        best_price_row = usable_price_rows[0]

        candidates.append({
            "productId": product_id,
            "groupId": group_id,
            "name": product.get("name") or "",
            "cleanName": product.get("cleanName") or "",
            "url": product.get("url"),
            "imageUrl": product.get("imageUrl"),
            "subTypeName": best_price_row.get("subTypeName"),
            "marketPrice": best_price_row.get("marketPrice"),
            "midPrice": best_price_row.get("midPrice"),
            "lowPrice": best_price_row.get("lowPrice"),
            "selectedPrice": choose_tcgcsv_price(best_price_row),
            "source": "local_products_cache_plus_local_price_csv",
        })

    return sorted(candidates, key=lambda candidate: (
        candidate["productId"] != base_product_id,
        candidate["productId"],
    ))