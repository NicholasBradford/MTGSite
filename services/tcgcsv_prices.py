import csv
import json
import os
import time, requests
from collections import defaultdict
from datetime import datetime
from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError, RequestException

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

TCGCSV_SOURCE_LOCAL_ONLY = "local_only"
TCGCSV_SOURCE_REMOTE_FALLBACK_LOCAL = "remote_fallback_local"

_TCGCSV_COOLDOWN_UNTIL = 0.0
_LOCAL_SNAPSHOT_CACHE = {"mtime": None, "group_prices": {}}

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


def load_local_group_prices(snapshot_path=TCGCSV_LOCAL_PRICE_SNAPSHOT):
    """
    Loads a local daily snapshot generated by scripts/refresh_tcgcsv_snapshot.py.
    Returns a groupId -> [price objects] mapping that mirrors remote TCGCSV shape.
    """
    if not os.path.exists(snapshot_path):
        return {}

    mtime = os.path.getmtime(snapshot_path)
    if _LOCAL_SNAPSHOT_CACHE["mtime"] == mtime:
        return _LOCAL_SNAPSHOT_CACHE["group_prices"]

    grouped = defaultdict(list)

    with open(snapshot_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            product_id = row.get("product_id")
            group_id = row.get("group_id")

            if not product_id or not group_id:
                continue

            try:
                product_id = int(product_id)
            except (TypeError, ValueError):
                continue

            grouped[str(group_id)].append({
                "productId": product_id,
                "subTypeName": row.get("sub_type_name") or "",
                "marketPrice": row.get("market_price"),
                "midPrice": row.get("mid_price"),
                "lowPrice": row.get("low_price"),
            })

    _LOCAL_SNAPSHOT_CACHE["mtime"] = mtime
    _LOCAL_SNAPSHOT_CACHE["group_prices"] = dict(grouped)
    return _LOCAL_SNAPSHOT_CACHE["group_prices"]


def get_tcgcsv_prices_for_group_with_fallback(
    session,
    group_id,
    data_source=TCGCSV_SOURCE_LOCAL_ONLY,
):
    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        return load_local_group_prices().get(str(group_id), [])

    try:
        return get_tcgcsv_prices_for_group(session, group_id)
    except Exception:
        local = load_local_group_prices().get(str(group_id), [])
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
):
    wanted_product_ids = set()

    for row in local_rows:
        if row["tcgplayer_id"]:
            wanted_product_ids.add(row["tcgplayer_id"])
        if row["tcgplayer_etched_id"]:
            wanted_product_ids.add(row["tcgplayer_etched_id"])

    product_to_group = {}

    for row in local_rows:
        if row["tcgcsv_group_id"] and row["tcgplayer_id"]:
            product_to_group[row["tcgplayer_id"]] = row["tcgcsv_group_id"]
        if row["tcgcsv_group_id"] and row["tcgplayer_etched_id"]:
            product_to_group[row["tcgplayer_etched_id"]] = row["tcgcsv_group_id"]

    missing_product_ids = wanted_product_ids - set(product_to_group.keys())

    if missing_product_ids and allow_remote_group_lookup:
        found_map = build_group_map_from_tcgcsv(session, missing_product_ids)
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
        normal_product_id = row["tcgplayer_id"]
        etched_product_id = row["tcgplayer_etched_id"]
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

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        tcgcsv_timestamp = get_local_snapshot_last_updated() or datetime.utcnow().isoformat(timespec="seconds")
    else:
        try:
            tcgcsv_timestamp = tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)
        except Exception:
            tcgcsv_timestamp = datetime.utcnow().isoformat(timespec="seconds")

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
    )

    updated_any = False
    
    for group_id, targets_by_scryfall_id in grouped_targets.items():
        prices = get_tcgcsv_prices_for_group_with_fallback(
            session,
            group_id,
            data_source=data_source,
        )
        prices_by_product_id = defaultdict(dict)

        for price_obj in prices:
            product_id = price_obj.get("productId")
            finish = price_finish_from_subtype(price_obj.get("subTypeName"))
            selected_price = choose_tcgcsv_price(price_obj)

            if product_id and selected_price is not None:
                prices_by_product_id[product_id][finish] = selected_price

        for target_key, target in targets_by_scryfall_id.items():
            if target["scryfall_id"] != scryfall_id:
                continue

            normal_product_id = target.get("normal_product_id")
            etched_product_id = target.get("etched_product_id")
            inventory_finish = target.get("finish")
            has_price_override = target.get("has_price_override", False)

            nonfoil_price = None
            foil_price = None

            if normal_product_id:
                product_prices = prices_by_product_id.get(normal_product_id, {})

                if has_price_override and is_foil_like_finish(inventory_finish):
                    foil_price = product_prices.get("foil") or product_prices.get("nonfoil")
                else:
                    nonfoil_price = product_prices.get("nonfoil")
                    foil_price = product_prices.get("foil")

            if etched_product_id:
                etched_prices = prices_by_product_id.get(etched_product_id, {})
                foil_price = etched_prices.get("foil") or etched_prices.get("nonfoil") or foil_price

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
                    source
                )
                VALUES (?, ?, ?, ?)
            """, (scryfall_id, nonfoil_price, foil_price, "tcgcsv"))

            updated_any = True

    return updated_any


def update_prices_for_scryfall_ids_from_tcgcsv(
    manager,
    scryfall_ids,
    data_source=TCGCSV_SOURCE_LOCAL_ONLY,
    allow_remote_group_lookup=False,
):
    """
    Batch updates prices for explicit card printings, even if they are not yet
    present in inventory.
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

    if data_source == TCGCSV_SOURCE_LOCAL_ONLY:
        tcgcsv_timestamp = get_local_snapshot_last_updated() or datetime.utcnow().isoformat(timespec="seconds")
    else:
        try:
            tcgcsv_timestamp = tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)
        except Exception:
            tcgcsv_timestamp = datetime.utcnow().isoformat(timespec="seconds")

    grouped_targets = get_grouped_local_price_targets(
        manager,
        session,
        rows,
        allow_remote_group_lookup=allow_remote_group_lookup,
    )

    updated_cards = 0

    for group_id, targets_by_key in grouped_targets.items():
        prices = get_tcgcsv_prices_for_group_with_fallback(
            session,
            group_id,
            data_source=data_source,
        )
        prices_by_product_id = defaultdict(dict)

        for price_obj in prices:
            product_id = price_obj.get("productId")
            finish = price_finish_from_subtype(price_obj.get("subTypeName"))
            selected_price = choose_tcgcsv_price(price_obj)

            if product_id and selected_price is not None:
                prices_by_product_id[product_id][finish] = selected_price

        for target in targets_by_key.values():
            scryfall_id = target.get("scryfall_id")
            normal_product_id = target.get("normal_product_id")
            etched_product_id = target.get("etched_product_id")

            nonfoil_price = None
            foil_price = None

            if normal_product_id:
                product_prices = prices_by_product_id.get(normal_product_id, {})
                nonfoil_price = product_prices.get("nonfoil")
                foil_price = product_prices.get("foil")

            if etched_product_id:
                etched_prices = prices_by_product_id.get(etched_product_id, {})
                foil_price = etched_prices.get("foil") or etched_prices.get("nonfoil") or foil_price

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
                    source
                )
                VALUES (?, ?, ?, ?)
            """, (scryfall_id, nonfoil_price, foil_price, "tcgcsv"))

            updated_cards += 1

        time.sleep(TCGCSV_RATE_LIMIT_DELAY)

    return updated_cards