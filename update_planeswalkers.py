import os
import sys
import shutil
import time
from collections import OrderedDict

import requests

from db.db_manager import CardDB


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", os.path.join("db", "mtg_inventory.db"))
IMAGE_PATH = os.environ.get("IMAGE_PATH", "var/data")

EXCLUDED_BACKBONE_SETS = {"plst"}

headers = {
    "User-Agent": "Mozilla/5.0 (MTG-Collection-Tracker/1.0)"
}


# ---------------------------------------------------------------------------
# Scryfall/image helpers
# ---------------------------------------------------------------------------

def get_front_image_url(card):
    """Return the best normal-sized front image URL for regular and DFC cards."""
    if card.get("image_uris"):
        return card["image_uris"].get("normal", "")

    if card.get("card_faces"):
        front_face = card["card_faces"][0]
        if front_face.get("image_uris"):
            return front_face["image_uris"].get("normal", "")

    return ""


def get_front_face_fields(card):
    """Fill root-level card fields that are commonly missing on double-faced cards."""
    mana_cost = card.get("mana_cost")
    type_line = card.get("type_line")
    oracle_text = card.get("oracle_text")

    if card.get("card_faces"):
        front_face = card["card_faces"][0]

        if not mana_cost:
            mana_cost = front_face.get("mana_cost")

        if not type_line:
            type_line = front_face.get("type_line")

        if not oracle_text:
            front_text = front_face.get("oracle_text", "")
            back_text = ""

            if len(card["card_faces"]) > 1:
                back_text = card["card_faces"][1].get("oracle_text", "")

            oracle_text = f"{front_text} // {back_text}" if back_text else front_text

    return mana_cost, type_line, oracle_text


def download_card_image(card):
    """
    Download a card image using the same storage pattern as ScryfallFetcher:
    img/cards/<set_code>/<scryfall_id>.jpg

    Returns the local DB path whether the image already existed or was newly downloaded.
    """
    scryfall_id = card.get("id")
    set_code = card.get("set", "").lower()

    if not scryfall_id or not set_code:
        return ""

    image_url = get_front_image_url(card)
    if not image_url:
        return ""

    local_img_path = f"img/cards/{set_code}/{scryfall_id}.jpg"
    full_img_fs_path = os.path.join(IMAGE_PATH, local_img_path)

    if os.path.exists(full_img_fs_path):
        return local_img_path

    os.makedirs(os.path.dirname(full_img_fs_path), exist_ok=True)

    try:
        with requests.get(image_url, stream=True, headers=headers, timeout=20) as img_res:
            if img_res.status_code == 200:
                with open(full_img_fs_path, "wb") as f:
                    shutil.copyfileobj(img_res.raw, f)

                # Be polite to Scryfall/CDN.
                time.sleep(0.1)
                return local_img_path

            print(f"Image download failed for {card.get('name')}: HTTP {img_res.status_code}")
            return ""

    except requests.RequestException as e:
        print(f"Image download error for {card.get('name')}: {e}")
        return ""


def fetch_named_card_exact(name, set_code=None):
    """
    Fetch a single exact-name card from Scryfall.

    set_code is used for special cases like the Urza meld pieces, where we want
    the Brother's War versions specifically.
    """
    url = "https://api.scryfall.com/cards/named"
    params = {"exact": name}

    if set_code:
        params["set"] = set_code

    response = requests.get(url, params=params, headers=headers, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(
            f"Scryfall named lookup failed for {name}: "
            f"HTTP {response.status_code} - {response.text[:300]}"
        )

    time.sleep(0.1)
    return response.json()


def fetch_planeswalker_cards():
    """
    Pull the planeswalker backbone from Scryfall in historical release order.

    Urza, Planeswalker is a melded result rather than a collectible front-facing
    card, so it is replaced with its two meld components.

    Reprint-only/meta sets such as PLST are excluded so cards like Valki do not
    get separated from their real release set.
    """
    excluded_set_terms = " ".join(
        f"-set:{set_code}" for set_code in sorted(EXCLUDED_BACKBONE_SETS)
    )

    query = (
        "type:planeswalker "
        "(game:paper) "
        "legal:commander "
        "is:default "
        "prefer:oldest "
        "language:english "
        f"{excluded_set_terms}"
    ).strip()

    url = "https://api.scryfall.com/cards/search"
    params = {
        "q": query,
        "order": "released",
        "dir": "asc",
    }

    while url:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(
                f"Scryfall search failed: HTTP {response.status_code} - {response.text[:300]}"
            )

        payload = response.json()

        for card in payload.get("data", []):
            card_set = card.get("set", "").lower()

            if card_set in EXCLUDED_BACKBONE_SETS:
                print(f"Skipping excluded backbone set card: {card.get('name')} [{card_set.upper()}]")
                continue

            if card.get("name") == "Urza, Planeswalker":
                yield fetch_named_card_exact("Urza, Lord Protector", set_code="bro")
                yield fetch_named_card_exact("The Mightstone and Weakstone", set_code="bro")
            else:
                yield card

        url = payload.get("next_page") if payload.get("has_more") else None
        params = None

        if url:
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def ensure_planeswalker_sort_index_column(cursor):
    """Add sort_index to planeswalker_tracker if it does not already exist."""
    cursor.execute("PRAGMA table_info(planeswalker_tracker)")
    columns = {row["name"] for row in cursor.fetchall()}

    if "sort_index" not in columns:
        cursor.execute(
            """
            ALTER TABLE planeswalker_tracker
            ADD COLUMN sort_index INTEGER
            """
        )
        print("Added sort_index column to planeswalker_tracker.")


def remove_urza_planeswalker_tracker_row(cursor):
    """
    Remove the melded Urza, Planeswalker tracker row.

    This intentionally does not delete card_printings or card_definitions.
    It only removes the incorrect tracker/backbone row.
    """
    cursor.execute(
        """
        DELETE FROM planeswalker_tracker
        WHERE name = ?
        """,
        ("Urza, Planeswalker",),
    )

    if cursor.rowcount:
        print("Removed Urza, Planeswalker from planeswalker_tracker.")


def upsert_card_definition_and_printing(cursor, card, local_img_path):
    """Create or update the card_definitions and card_printings records used by the site."""
    scryfall_id = card.get("id")
    oracle_id = card.get("oracle_id")

    if not oracle_id and card.get("card_faces"):
        oracle_id = card["card_faces"][0].get("oracle_id")

    if not scryfall_id or not oracle_id:
        print(f"Skipping definition/printing for {card.get('name')}: missing Scryfall ID or Oracle ID.")
        return False

    mana_cost, type_line, oracle_text = get_front_face_fields(card)

    name = card.get("name")
    cmc = card.get("cmc", 0.0)
    color = "".join(card.get("colors", []))
    color_identity = "".join(card.get("color_identity", []))
    set_code = card.get("set", "").lower()
    prices = card.get("prices", {})

    cursor.execute(
        """
        INSERT OR IGNORE INTO card_definitions
        (oracle_id, name, mana_cost, cmc, type_line, oracle_text, color, color_identity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            oracle_id,
            name,
            mana_cost,
            cmc,
            type_line,
            oracle_text,
            color,
            color_identity,
        ),
    )

    cursor.execute(
        """
        UPDATE card_definitions
        SET
            name = COALESCE(?, name),
            mana_cost = COALESCE(?, mana_cost),
            cmc = COALESCE(?, cmc),
            type_line = COALESCE(?, type_line),
            oracle_text = COALESCE(?, oracle_text),
            color = COALESCE(?, color),
            color_identity = COALESCE(?, color_identity)
        WHERE oracle_id = ?
        """,
        (
            name,
            mana_cost,
            cmc,
            type_line,
            oracle_text,
            color,
            color_identity,
            oracle_id,
        ),
    )

    cursor.execute(
        """
        INSERT OR REPLACE INTO card_printings
        (
            scryfall_id,
            oracle_id,
            set_code,
            collector_number,
            rarity,
            image_url,
            flavor_text,
            current_price,
            current_price_foil
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scryfall_id,
            oracle_id,
            set_code,
            card.get("collector_number"),
            card.get("rarity"),
            local_img_path,
            card.get("flavor_text"),
            prices.get("usd"),
            prices.get("usd_foil"),
        ),
    )

    return True


def upsert_planeswalker_tracker(cursor, card, sort_index):
    """Insert tracker cards and update existing rows with current default printing and sort order."""
    oracle_id = card.get("oracle_id")
    scryfall_id = card.get("id")
    name = card.get("name")
    release_date = card.get("released_at")

    if not oracle_id or not scryfall_id:
        print(f"Skipping tracker row for {name}: missing Scryfall ID or Oracle ID.")
        return False

    cursor.execute(
        """
        INSERT OR IGNORE INTO planeswalker_tracker
        (oracle_id, default_scryfall_id, name, release_date, sort_index)
        VALUES (?, ?, ?, ?, ?)
        """,
        (oracle_id, scryfall_id, name, release_date, sort_index),
    )

    inserted = cursor.rowcount > 0

    cursor.execute(
        """
        UPDATE planeswalker_tracker
        SET
            default_scryfall_id = ?,
            name = ?,
            release_date = ?,
            sort_index = ?
        WHERE oracle_id = ?
        """,
        (scryfall_id, name, release_date, sort_index, oracle_id),
    )

    return inserted


def repair_existing_planeswalker_images(cursor):
    """
    Repair tracker rows whose default printing exists but has a blank/missing image_url.
    This catches older imports where card_printings was present but image_url was not.
    """
    cursor.execute(
        """
        SELECT
            pt.default_scryfall_id,
            pt.name,
            cp.set_code,
            cp.image_url
        FROM planeswalker_tracker pt
        LEFT JOIN card_printings cp
            ON pt.default_scryfall_id = cp.scryfall_id
        WHERE pt.default_scryfall_id IS NOT NULL
          AND (cp.image_url IS NULL OR cp.image_url = '')
        """
    )

    rows = cursor.fetchall()
    repaired = 0

    for row in rows:
        scryfall_id = row["default_scryfall_id"]
        url = f"https://api.scryfall.com/cards/{scryfall_id}"

        try:
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code != 200:
                print(f"Could not repair {row['name']}: HTTP {response.status_code}")
                continue

            card = response.json()
            local_img_path = download_card_image(card)
            if not local_img_path:
                print(f"No image available for repair: {row['name']}")
                continue

            upsert_card_definition_and_printing(cursor, card, local_img_path)
            repaired += 1

        except requests.RequestException as e:
            print(f"Repair request failed for {row['name']}: {e}")

    return repaired


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------

def set_group_key(card):
    """
    Return the set grouping key.

    Set code is enough here because the backbone is intended to group cards by
    their release set, then sort alphabetically inside that set.
    """
    return card.get("set", "").lower()


def walker_sort_key(card):
    """
    Sort cards alphabetically within a set, with special handling for meld ordering.

    The Urza meld pieces should display together in meld order, not pure
    alphabetical order.
    """
    name = card.get("name", "")

    special_order = {
        "Urza, Lord Protector": "urza, planeswalker 01",
        "The Mightstone and Weakstone": "urza, planeswalker 02",
    }

    return special_order.get(name, name.lower())


def collect_cards_by_set():
    """
    Fetch all tracker cards and group them by set in first-seen release order.

    This is more reliable than flushing only when the set code changes, because
    it still behaves correctly if Scryfall ever interleaves cards with the same
    release date.
    """
    grouped_cards = OrderedDict()

    for card in fetch_planeswalker_cards():
        group_key = set_group_key(card)

        if group_key not in grouped_cards:
            grouped_cards[group_key] = []

        grouped_cards[group_key].append(card)

    return grouped_cards


def process_tracker_card(cursor, card, sort_index):
    """Process one card through the image, tracker, definition, and printing pipeline."""
    local_img_path = download_card_image(card)

    if local_img_path:
        image_processed = True
    else:
        print(f"No local image stored for {card.get('name')}")
        image_processed = False

    inserted = upsert_planeswalker_tracker(cursor, card, sort_index)

    if inserted:
        print(f"Added tracker card: {card.get('name')}")

    if local_img_path:
        upsert_card_definition_and_printing(cursor, card, local_img_path)

    return inserted, image_processed


# ---------------------------------------------------------------------------
# Main update
# ---------------------------------------------------------------------------

def update_planeswalker_backbone():
    manager = CardDB()

    # Make row["column_name"] access reliable for this script.
    manager.conn.row_factory = __import__("sqlite3").Row
    cursor = manager.conn.cursor()

    added_count = 0
    updated_count = 0
    image_count = 0
    sort_index = 1

    try:
        ensure_planeswalker_sort_index_column(cursor)
        remove_urza_planeswalker_tracker_row(cursor)

        print("Fetching Planeswalker backbone from Scryfall...")
        grouped_cards = collect_cards_by_set()

        for set_code, cards in grouped_cards.items():
            print(f"Processing set: {set_code.upper()}")

            for card in sorted(cards, key=walker_sort_key):
                inserted, image_processed = process_tracker_card(cursor, card, sort_index)

                if inserted:
                    added_count += 1
                else:
                    updated_count += 1

                if image_processed:
                    image_count += 1

                sort_index += 1

        repaired_count = repair_existing_planeswalker_images(cursor)

        manager.commit()

        print(
            "Update complete. "
            f"Added {added_count} new tracker cards. "
            f"Updated {updated_count} existing tracker rows. "
            f"Processed {image_count} image paths. "
            f"Repaired {repaired_count} missing image records. "
            f"Assigned sort_index values 1 through {sort_index - 1}."
        )

    except Exception:
        manager.conn.rollback()
        raise

    finally:
        manager.close()


if __name__ == "__main__":
    update_planeswalker_backbone()
