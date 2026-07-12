import csv
import os
import re
import requests
from collections import defaultdict
from dotenv import load_dotenv
from db.db_manager import CardDB

load_dotenv()

# This module contains the finish-specific / non-standard foiling mapping code
# that used to live in tcgcsv_prices.py.
#
# The tiny wrapper functions below deliberately import tcgcsv_prices lazily.
# That lets tcgcsv_prices.py re-export these helpers for backward compatibility
# without creating a circular import during module loading.
def _tcgcsv_prices_module():
    try:
        from . import tcgcsv_prices
    except ImportError:
        import tcgcsv_prices
    return tcgcsv_prices


def normalize_finish(value):
    return _tcgcsv_prices_module().normalize_finish(value)


def int_or_none(value):
    return _tcgcsv_prices_module().int_or_none(value)


def load_local_group_prices(snapshot_path=None):
    return _tcgcsv_prices_module().load_local_group_prices(snapshot_path=snapshot_path)


def build_group_map_from_tcgcsv(session, wanted_product_ids, progress_callback=None):
    return _tcgcsv_prices_module().build_group_map_from_tcgcsv(
        session,
        wanted_product_ids,
        progress_callback=progress_callback,
    )


def get_tcgcsv_products_for_group(session, group_id):
    return _tcgcsv_prices_module().get_tcgcsv_products_for_group(session, group_id)


def build_product_to_group_map_from_local_snapshot(snapshot_path=None):
    return _tcgcsv_prices_module().build_product_to_group_map_from_local_snapshot(
        snapshot_path=snapshot_path
    )


def choose_tcgcsv_price(price_obj):
    return _tcgcsv_prices_module().choose_tcgcsv_price(price_obj)


def price_finish_from_subtype(subtype_name):
    return _tcgcsv_prices_module().price_finish_from_subtype(subtype_name)


def resolve_local_price_snapshot_path(snapshot_path=None):
    return _tcgcsv_prices_module().resolve_local_price_snapshot_path(
        snapshot_path=snapshot_path,
        refresh_if_due=False,
    )


def normalize_local_price_snapshot_row(row):
    return _tcgcsv_prices_module().normalize_local_price_snapshot_row(row)


TCGCSV_HEADERS = {
    "User-Agent": "MTGSitePriceUpdater/1.0",
    "Accept": "application/json",
}

TCGCSV_HISTORY_DIR = os.environ.get("TCGCSV_HISTORY_DIR", "tcg_history")
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

AUTO_FINISH_OVERRIDE_NOTE_PREFIX = "Auto-mapped from local TCGCSV price CSV"

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


def local_price_index_path(snapshot_path):
    return f"{snapshot_path}.idx.sqlite"


def _open_local_price_index_db(index_path):
    return CardDB(db_path=index_path)


def get_existing_local_price_index(snapshot_path=None):
    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)
    if not snapshot_path or not os.path.exists(snapshot_path):
        return None

    index_path = local_price_index_path(snapshot_path)
    if os.path.exists(index_path):
        return index_path

    return None


def _iter_indexed_price_rows(index_path, group_id=None, product_id=None):
    if not index_path or not os.path.exists(index_path):
        return

    query = (
        "SELECT product_id, group_id, subtype_name, market_price, mid_price, "
        "low_price, high_price, direct_low_price FROM price_rows"
    )
    params = []
    where_clauses = []

    if group_id is not None:
        where_clauses.append("group_id = ?")
        params.append(str(group_id))

    if product_id is not None:
        where_clauses.append("product_id = ?")
        params.append(int(product_id))

    if where_clauses:
        query = f"{query} WHERE {' AND '.join(where_clauses)}"

    manager = _open_local_price_index_db(index_path)
    try:
        cursor = manager.cursor.execute(query, tuple(params))
        for row in cursor:
            yield {
                "productId": int_or_none(row["product_id"]),
                "group_id": str(row["group_id"]) if row["group_id"] is not None else None,
                "subTypeName": row["subtype_name"] or "",
                "marketPrice": row["market_price"],
                "midPrice": row["mid_price"],
                "lowPrice": row["low_price"],
                "highPrice": row["high_price"],
                "directLowPrice": row["direct_low_price"],
            }
    finally:
        manager.close()


def _iter_local_price_rows(snapshot_path=None, group_id=None, product_id=None):
    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)
    index_path = get_existing_local_price_index(snapshot_path=snapshot_path)

    if index_path and os.path.exists(index_path):
        for row in _iter_indexed_price_rows(
            index_path=index_path,
            group_id=group_id,
            product_id=product_id,
        ):
            yield row
        return

    for row in _iter_local_price_rows_with_group_id(
        snapshot_path=snapshot_path,
        group_id=group_id,
    ):
        if product_id is not None and _row_product_id(row) != int_or_none(product_id):
            continue
        yield row


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

    base_rows = []
    base_group_ids = set()

    for row in _iter_local_price_rows(
        snapshot_path=snapshot_path,
        product_id=base_product_id,
    ):
        if _row_product_id(row) != base_product_id:
            continue

        base_rows.append(row)
        row_group_id = _row_group_id(row)
        if row_group_id is not None:
            base_group_ids.add(row_group_id)

    if not base_rows:
        return _finish_lookup_result(
            "base_product_not_found",
            f"Base product ID {base_product_id} was not found in the local TCGCSV snapshot.",
            base_product_id,
            requested_finish,
        )

    group_ids = sorted(base_group_ids)

    if len(group_ids) != 1:
        return _finish_lookup_result(
            "ambiguous",
            f"Base product ID {base_product_id} did not resolve to exactly one group_id.",
            base_product_id,
            requested_finish,
            candidates=_dedupe_candidates_by_product_id(base_rows),
        )

    group_id = group_ids[0]

    matching_finish_rows = []

    for row in _iter_local_price_rows(
        snapshot_path=snapshot_path,
        group_id=group_id,
    ):
        if subtype_matches_finish(_row_subtype_name(row), requested_finish):
            matching_finish_rows.append(row)

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


def search_tcgcsv_products_for_finish(manager, scryfall_id, finish):
    """
    Returns candidate TCGplayer products for a special finish.
    Use this for the modal button: Find matching TCGplayer product.
    """
    with requests.Session() as session:
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


def _iter_local_price_rows_with_group_id(snapshot_path=None, group_id=None):
    """
    Stream normalized rows from the local TCGCSV price snapshot.

    Optional group_id filtering avoids loading all groups for small lookups.
    """
    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)

    if not os.path.exists(snapshot_path):
        return

    target_group_id = str(group_id) if group_id is not None else None

    with open(snapshot_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for raw_row in reader:
            normalized = normalize_local_price_snapshot_row(raw_row)
            if normalized is None:
                continue

            row_group_id = str(normalized.get("group_id"))
            if target_group_id is not None and row_group_id != target_group_id:
                continue

            normalized["group_id"] = row_group_id
            yield normalized


def _find_group_id_for_product_ids(snapshot_path, product_ids):
    wanted_ids = []
    seen_ids = set()

    for value in product_ids:
        product_id = int_or_none(value)
        if product_id is None or product_id in seen_ids:
            continue

        wanted_ids.append(product_id)
        seen_ids.add(product_id)

    if not wanted_ids:
        return {}

    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)
    index_path = get_existing_local_price_index(snapshot_path=snapshot_path)

    if index_path and os.path.exists(index_path):
        placeholders = ",".join("?" for _ in wanted_ids)
        query = (
            "SELECT product_id, group_id FROM price_rows "
            f"WHERE product_id IN ({placeholders}) AND group_id IS NOT NULL"
        )

        found = {}
        manager = _open_local_price_index_db(index_path)
        try:
            for row in manager.cursor.execute(query, tuple(wanted_ids)):
                found[int(row["product_id"])] = str(row["group_id"])
        finally:
            manager.close()

        return found

    wanted_set = set(wanted_ids)
    found = {}

    for row in _iter_local_price_rows_with_group_id(snapshot_path=snapshot_path):
        product_id = _row_product_id(row)
        row_group_id = _row_group_id(row)

        if product_id not in wanted_set or row_group_id is None:
            continue

        found[product_id] = str(row_group_id)
        if len(found) == len(wanted_set):
            break

    return found


def _iter_local_products_for_group(products_path, group_id):
    if not os.path.exists(products_path):
        return

    target_group_id = str(group_id)

    with open(products_path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)

        for row in reader:
            row_group_id = row.get("group_id") or row.get("groupId")
            product_id = int_or_none(row.get("productId"))

            if product_id is None or not row_group_id:
                continue

            if str(row_group_id) != target_group_id:
                continue

            yield {
                "productId": product_id,
                "groupId": target_group_id,
                "name": row.get("name") or "",
                "cleanName": row.get("cleanName") or "",
                "url": row.get("url") or "",
                "imageUrl": row.get("imageUrl") or "",
            }


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

    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)

    if not group_id:
        product_to_group = _find_group_id_for_product_ids(
            snapshot_path=snapshot_path,
            product_ids=[base_product_id, etched_product_id],
        )

        if base_product_id is not None:
            group_id = product_to_group.get(base_product_id)

        if not group_id and etched_product_id is not None:
            group_id = product_to_group.get(etched_product_id)

    if not group_id:
        return []

    group_id = str(group_id)

    requested_finish = normalize_finish(finish)
    requested_is_special = requested_finish in SPECIAL_FINISHES

    candidates = []

    for price_row in _iter_local_price_rows(
        snapshot_path=snapshot_path,
        group_id=group_id,
    ):
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

    snapshot_path = resolve_local_price_snapshot_path(snapshot_path=snapshot_path)

    group_id = row["tcgcsv_group_id"]

    if not group_id:
        product_to_group = _find_group_id_for_product_ids(
            snapshot_path=snapshot_path,
            product_ids=[base_product_id, etched_product_id],
        )

        if base_product_id is not None:
            group_id = product_to_group.get(base_product_id)

        if not group_id and etched_product_id is not None:
            group_id = product_to_group.get(etched_product_id)

    if not group_id:
        return []

    group_id = str(group_id)

    products = list(_iter_local_products_for_group(products_path, group_id))
    if not products:
        return []

    prices_by_id = {}

    for price_row in _iter_local_price_rows(
        snapshot_path=snapshot_path,
        group_id=group_id,
    ):
        product_id = int_or_none(price_row.get("productId"))
        if product_id is None or product_id in prices_by_id:
            continue

        selected_price = choose_tcgcsv_price(price_row)
        if selected_price is None:
            continue

        row_with_selected_price = dict(price_row)
        row_with_selected_price["selectedPrice"] = selected_price
        prices_by_id[product_id] = row_with_selected_price

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

        best_price_row = prices_by_id.get(product_id)

        if not best_price_row:
            continue

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
            "selectedPrice": best_price_row.get("selectedPrice"),
            "source": "local_products_cache_plus_local_price_csv",
        })

    return sorted(candidates, key=lambda candidate: (
        candidate["productId"] != base_product_id,
        candidate["productId"],
    ))
