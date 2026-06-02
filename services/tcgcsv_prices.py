import time, requests
from collections import defaultdict
from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError, RequestException

TCGCSV_BASE_URL = "https://tcgcsv.com/tcgplayer"
TCGCSV_LAST_UPDATED_URL = "https://tcgcsv.com/last-updated.txt"
TCGCSV_MAGIC_CATEGORY_ID = 1
TCGCSV_RATE_LIMIT_DELAY = .15

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

def get_grouped_local_price_targets(manager, session, local_rows):
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

    if missing_product_ids:
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

def update_single_card_price_from_tcgcsv(manager, scryfall_id):
    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    tcgcsv_timestamp = tcgcsv_get_text(session, TCGCSV_LAST_UPDATED_URL)

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

    grouped_targets = get_grouped_local_price_targets(manager, session, rows)

    updated_any = False
    
    for group_id, targets_by_scryfall_id in grouped_targets.items():
        prices = get_tcgcsv_prices_for_group(session, group_id)
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