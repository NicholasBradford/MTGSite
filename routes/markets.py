
import json
import time
import requests


from functools import wraps
from flask import Blueprint, Response, render_template, abort, request
from flask_login import login_required, current_user
from collections import defaultdict
from services.tcgcsv_prices import *
import datetime as dt

from db.db_manager import CardDB, get_db


markets_bp = Blueprint("markets", __name__)


# =========================================================
# Config
# =========================================================

SCRYFALL_COLLECTION_ENDPOINT = "https://api.scryfall.com/cards/collection"
SCRYFALL_CHUNK_SIZE = 75
SCRYFALL_RATE_LIMIT_DELAY = 0.5

MARKET_MOVER_LIMIT = 24
MARKET_OPPORTUNITY_LIMIT = 6
MARKET_TRADE_ALERT_LIMIT = 24
MARKET_MISSING_PRICE_LIMIT = 24

MIN_CURRENT_PRICE = 2.00
MIN_DOLLAR_MOVE = 1.00
MIN_PERCENT_MOVE = 25.0
MIN_OWNED_IMPACT = 3.00

FOIL_LIKE_FINISH_SQL = """
    LOWER(REPLACE(COALESCE({finish_column}, ''), '_', ' ')) IN (
        'foil',
        'etched foil',
        'rainbow foil',
        'surge foil',
        'galaxy foil',
        'textured foil',
        'double rainbow foil'
    )
"""

def movement_threshold_params():
    return (
        MIN_CURRENT_PRICE,
        MIN_CURRENT_PRICE,
        MIN_OWNED_IMPACT,
        MIN_DOLLAR_MOVE,
        MIN_PERCENT_MOVE,
    )


def movement_threshold_params_with_limit(limit):
    return (
        MIN_CURRENT_PRICE,
        MIN_CURRENT_PRICE,
        MIN_OWNED_IMPACT,
        MIN_DOLLAR_MOVE,
        MIN_PERCENT_MOVE,
        limit,
    )

VALID_MARKET_FILTERS = {
    "all",
    "owned",
    "tradeable",
    "wishlist",
    "decks",
    "planeswalkers",
}

VALID_MARKET_SORTS = {
    "dollar_change",
    "percent_change",
    "current_value",
    "owned_impact",
    "newest",
}

MARKET_SORT_SQL = {
    "dollar_change": "ABS(new_price - old_price) DESC, ABS((new_price - old_price) * qty) DESC",
    "percent_change": "ABS(percent_delta) DESC, ABS(new_price - old_price) DESC",
    "current_value": "new_price DESC, qty DESC",
    "owned_impact": "ABS(owned_impact) DESC, ABS(new_price - old_price) DESC",
    "newest": "latest_scraped_at DESC, ABS(new_price - old_price) DESC",
}


# =========================================================
# Safety / Access Helpers
# =========================================================

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403) # Forbidden
        return f(*args, **kwargs)
    return decorated_function

# =========================================================
# Small DB Helpers
# =========================================================

def fetch_one_dict(manager, query, params=()):
    row = manager.cursor.execute(query, params).fetchone()
    return dict(row) if row else {}


def fetch_all_dicts(manager, query, params=()):
    rows = manager.cursor.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def money(value):
    if value is None:
        value = 0

    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def percent(value):
    if value is None:
        value = 0

    try:
        return f"{float(value):,.1f}%"
    except (TypeError, ValueError):
        return "0.0%"
    
def get_market_filter():
    market_filter = request.args.get("market_filter", "all").strip().lower()

    if market_filter not in VALID_MARKET_FILTERS:
        return "all"

    return market_filter


def get_market_sort():
    market_sort = request.args.get("market_sort", "owned_impact").strip().lower()

    if market_sort not in VALID_MARKET_SORTS:
        return "owned_impact"

    return market_sort


def get_market_sort_sql(market_sort):
    return MARKET_SORT_SQL.get(
        market_sort,
        MARKET_SORT_SQL["owned_impact"]
    )


def get_market_filter_sql(market_filter):
    """
    Returns a SQL fragment that can be safely appended to MarketRows queries.

    Wishlist/decks/planeswalkers are accepted by the frontend now, but their
    actual data joins should be added later once those schema relationships
    are wired into the market helpers.
    """
    if market_filter == "tradeable":
        return "AND is_tradeable = 1"

    if market_filter == "owned":
        return "AND qty > 0"

    return ""

def get_wishlist_drops(manager, limit=24, market_sort="owned_impact"):
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager, trailing_comma=True)

    wishlist_is_foil_sql = """
        LOWER(REPLACE(COALESCE(w.finish, ''), '_', ' ')) IN (
            'foil',
            'etched',
            'rainbow foil'
        )
        """

    query = f"""
        {cte_sql}

        WishlistRows AS (
            SELECT
                NULL AS instance_id,
                w.scryfall_id,
                w.finish,
                NULL AS location_id,
                0 AS qty,
                0 AS is_tradeable,
                0 AS is_surplus,

                cp.image_url,
                cp.set_code,
                cp.collector_number,
                cd.name,

                CASE
                    WHEN {wishlist_is_foil_sql}
                    THEN pp.old_price_foil
                    ELSE pp.old_price_usd
                END AS old_price,

                CASE
                    WHEN {wishlist_is_foil_sql}
                    THEN pp.new_price_foil
                    ELSE pp.new_price_usd
                END AS new_price,

                ROUND(
                    CASE
                        WHEN {wishlist_is_foil_sql}
                        THEN pp.new_price_foil - pp.old_price_foil
                        ELSE pp.new_price_usd - pp.old_price_usd
                    END,
                    2
                ) AS price_delta,

                ROUND(
                    CASE
                        WHEN {wishlist_is_foil_sql}
                            AND pp.old_price_foil IS NOT NULL
                            AND pp.old_price_foil > 0
                        THEN ((pp.new_price_foil - pp.old_price_foil) / pp.old_price_foil) * 100

                        WHEN NOT ({wishlist_is_foil_sql})
                            AND pp.old_price_usd IS NOT NULL
                            AND pp.old_price_usd > 0
                        THEN ((pp.new_price_usd - pp.old_price_usd) / pp.old_price_usd) * 100

                        ELSE 0
                    END,
                    1
                ) AS percent_delta,

                ROUND(
                    CASE
                        WHEN {wishlist_is_foil_sql}
                        THEN pp.new_price_foil - pp.old_price_foil
                        ELSE pp.new_price_usd - pp.old_price_usd
                    END,
                    2
                ) AS owned_impact,

                pp.latest_scraped_at,
                pp.previous_scraped_at,
                w.priority,
                w.notes
            FROM wishlist w
            JOIN PricePairs pp
                ON w.scryfall_id = pp.scryfall_id
            LEFT JOIN card_printings cp
                ON w.scryfall_id = cp.scryfall_id
            LEFT JOIN card_definitions cd
                ON cp.oracle_id = cd.oracle_id
        )

        SELECT *
        FROM WishlistRows
        WHERE
            old_price IS NOT NULL
            AND new_price IS NOT NULL
            AND old_price > 0
            AND new_price < old_price
            AND (
                new_price >= ?
                OR old_price >= ?
                OR ABS((new_price - old_price) * qty) >= ?
            )
            AND (
                ABS(new_price - old_price) >= ?
                OR ABS(((new_price - old_price) / old_price) * 100) >= ?
            )
        ORDER BY {sort_sql}
        LIMIT ?
    """

    cards = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in cards:
        decorate_market_card(card)

    return cards

def get_deck_market_alerts(manager, limit=24, market_sort="owned_impact"):
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            NULL AS instance_id,
            edc.scryfall_id,
            'nonfoil' AS finish,
            NULL AS location_id,
            edc.quantity AS qty,
            0 AS is_tradeable,
            0 AS is_surplus,

            cp.image_url,
            cp.set_code,
            cp.collector_number,
            cd.name,
            d.deck_name,

            pp.old_price_usd AS old_price,
            pp.new_price_usd AS new_price,

            ROUND(pp.new_price_usd - pp.old_price_usd, 2) AS price_delta,

            ROUND(
                CASE
                    WHEN pp.old_price_usd IS NOT NULL AND pp.old_price_usd > 0
                    THEN ((pp.new_price_usd - pp.old_price_usd) / pp.old_price_usd) * 100
                    ELSE 0
                END,
                1
            ) AS percent_delta,

            ROUND((pp.new_price_usd - pp.old_price_usd) * edc.quantity, 2) AS owned_impact,

            pp.latest_scraped_at,
            pp.previous_scraped_at
        FROM edh_deck_cards edc
        JOIN edh_decks d
            ON edc.deck_id = d.deck_id
        JOIN PricePairs pp
            ON edc.scryfall_id = pp.scryfall_id
        LEFT JOIN card_printings cp
            ON edc.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE
            pp.old_price_usd IS NOT NULL
            AND pp.new_price_usd IS NOT NULL
            AND pp.old_price_usd > 0
            AND (
                pp.new_price_usd >= ?
                OR pp.old_price_usd >= ?
                OR ABS((pp.new_price_usd - pp.old_price_usd) * edc.quantity) >= ?
            )
            AND (
                ABS(pp.new_price_usd - pp.old_price_usd) >= ?
                OR ABS(((pp.new_price_usd - pp.old_price_usd) / pp.old_price_usd) * 100) >= ?
            )
        ORDER BY {sort_sql}
        LIMIT ?
    """

    cards = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in cards:
        decorate_market_card(card)
        if card.get("deck_name"):
            card["market_note"] = f"In deck: {card['deck_name']}"

    return cards

def get_planeswalker_market_alerts(manager, limit=24, market_sort="owned_impact"):
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager, trailing_comma=True)

    query = f"""
        {cte_sql}

        PlaneswalkerRows AS (
            SELECT
                MIN(i.instance_id) AS instance_id,
                cp.scryfall_id,
                COALESCE(i.finish, 'nonfoil') AS finish,
                MAX(i.location_id) AS location_id,
                COUNT(i.instance_id) AS qty,
                MAX(COALESCE(i.is_tradeable, 0)) AS is_tradeable,
                MAX(COALESCE(i.is_surplus, 0)) AS is_surplus,

                cp.image_url,
                cp.set_code,
                cp.collector_number,
                p.name,

                CASE
                    WHEN COALESCE(i.finish, 'nonfoil') = 'foil' THEN pp.old_price_foil
                    ELSE pp.old_price_usd
                END AS old_price,

                CASE
                    WHEN COALESCE(i.finish, 'nonfoil') = 'foil' THEN pp.new_price_foil
                    ELSE pp.new_price_usd
                END AS new_price,

                ROUND(
                    CASE
                        WHEN COALESCE(i.finish, 'nonfoil') = 'foil' THEN pp.new_price_foil - pp.old_price_foil
                        ELSE pp.new_price_usd - pp.old_price_usd
                    END,
                    2
                ) AS price_delta,

                ROUND(
                    CASE
                        WHEN COALESCE(i.finish, 'nonfoil') = 'foil'
                            AND pp.old_price_foil IS NOT NULL
                            AND pp.old_price_foil > 0
                        THEN ((pp.new_price_foil - pp.old_price_foil) / pp.old_price_foil) * 100

                        WHEN COALESCE(i.finish, 'nonfoil') != 'foil'
                            AND pp.old_price_usd IS NOT NULL
                            AND pp.old_price_usd > 0
                        THEN ((pp.new_price_usd - pp.old_price_usd) / pp.old_price_usd) * 100

                        ELSE 0
                    END,
                    1
                ) AS percent_delta,

                ROUND(
                    (
                        CASE
                            WHEN COALESCE(i.finish, 'nonfoil') = 'foil' THEN pp.new_price_foil - pp.old_price_foil
                            ELSE pp.new_price_usd - pp.old_price_usd
                        END
                    ) * COUNT(i.instance_id),
                    2
                ) AS owned_impact,

                pp.latest_scraped_at,
                pp.previous_scraped_at,
                p.release_date
            FROM planeswalker_tracker p
            JOIN card_printings cp
                ON p.oracle_id = cp.oracle_id
            JOIN PricePairs pp
                ON cp.scryfall_id = pp.scryfall_id
            LEFT JOIN inventory i
                ON cp.scryfall_id = i.scryfall_id
            GROUP BY cp.scryfall_id, COALESCE(i.finish, 'nonfoil')
        )

        SELECT *
        FROM PlaneswalkerRows
        WHERE
            old_price IS NOT NULL
            AND new_price IS NOT NULL
            AND old_price > 0
            AND (
                new_price >= ?
                OR old_price >= ?
                OR ABS((new_price - old_price) * qty) >= ?
            )
            AND (
                ABS(new_price - old_price) >= ?
                OR ABS(((new_price - old_price) / old_price) * 100) >= ?
            )
        ORDER BY {sort_sql}
        LIMIT ?
    """

    cards = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in cards:
        decorate_market_card(card)

        if card.get("qty", 0) == 0:
            card["market_note"] = "Tracked planeswalker not currently owned"
        else:
            card["market_note"] = "Owned planeswalker"

    return cards

def get_surplus_market_alerts(manager, limit=24, market_sort="owned_impact"):
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            instance_id,
            scryfall_id,
            finish,
            location_id,
            qty,
            is_tradeable,
            is_surplus,

            image_url,
            set_code,
            collector_number,
            name,

            old_price,
            new_price,

            ROUND(new_price - old_price, 2) AS price_delta,

            ROUND(
                CASE
                    WHEN old_price IS NOT NULL AND old_price > 0
                    THEN ((new_price - old_price) / old_price) * 100
                    ELSE 0
                END,
                1
            ) AS percent_delta,

            ROUND((new_price - old_price) * qty, 2) AS owned_impact,

            latest_scraped_at,
            previous_scraped_at
        FROM MarketRows
        WHERE
            old_price IS NOT NULL
            AND new_price IS NOT NULL
            AND old_price > 0
            AND is_surplus = 1
            AND new_price > old_price
            AND (
                new_price >= ?
                OR old_price >= ?
                OR ABS((new_price - old_price) * qty) >= ?
            )
            AND (
                ABS(new_price - old_price) >= ?
                OR ABS(((new_price - old_price) / old_price) * 100) >= ?
            )
        ORDER BY {sort_sql}
        LIMIT ?
    """

    cards = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in cards:
        decorate_market_card(card)
        card["market_note"] = "Surplus copy gained value"

    return cards

def get_purchase_gain_loss_alerts(manager, limit=24):
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            instance_id,
            scryfall_id,
            finish,
            location_id,
            qty,
            is_tradeable,
            is_surplus,

            image_url,
            set_code,
            collector_number,
            name,

            old_price,
            new_price,

            ROUND(new_price - old_price, 2) AS price_delta,

            ROUND(
                CASE
                    WHEN old_price IS NOT NULL AND old_price > 0
                    THEN ((new_price - old_price) / old_price) * 100
                    ELSE 0
                END,
                1
            ) AS percent_delta,

            ROUND((new_price * qty) - total_purchase_price, 2) AS owned_impact,
            ROUND(total_purchase_price, 2) AS total_purchase_price,
            ROUND(new_price * qty, 2) AS current_owned_value,

            latest_scraped_at,
            previous_scraped_at
        FROM MarketRows
        WHERE
            new_price IS NOT NULL
            AND total_purchase_price IS NOT NULL
            AND total_purchase_price > 0
            AND ABS((new_price * qty) - total_purchase_price) >= ?
        ORDER BY ABS((new_price * qty) - total_purchase_price) DESC
        LIMIT ?
    """

    cards = fetch_all_dicts(manager, query, (MIN_DOLLAR_MOVE, limit))

    for card in cards:
        decorate_market_card(card)

        if card.get("owned_impact", 0) > 0:
            card["market_note"] = "Above recorded purchase price"
        elif card.get("owned_impact", 0) < 0:
            card["market_note"] = "Below recorded purchase price"
        else:
            card["market_note"] = "At recorded purchase price"

    return cards

def get_price_quality_flags(manager, limit=24):
    foil_like_sql = FOIL_LIKE_FINISH_SQL.format(finish_column="i.finish")

    query = f"""
        WITH PriceHistoryCounts AS (
            SELECT
                scryfall_id,
                COUNT(*) AS price_history_count
            FROM price_history
            GROUP BY scryfall_id
        )

        SELECT
            MIN(i.instance_id) AS instance_id,
            i.scryfall_id,
            i.finish,
            MAX(i.location_id) AS location_id,
            COUNT(DISTINCT i.instance_id) AS qty,

            cp.image_url,
            cp.set_code,
            cp.collector_number,
            cd.name,

            cp.current_price,
            cp.current_price_foil,

            COALESCE(phc.price_history_count, 0) AS price_history_count,

            CASE
                WHEN {foil_like_sql}
                    AND (cp.current_price_foil IS NULL OR cp.current_price_foil = '')
                    AND (
                        cp.tcgplayer_etched_id IS NOT NULL
                        OR o.tcgplayer_id IS NOT NULL
                    )
                THEN 'Mapped foil-like copy missing foil price'

                WHEN NOT ({foil_like_sql})
                    AND (cp.current_price IS NULL OR cp.current_price = '')
                THEN 'Nonfoil copy missing price'

                WHEN COALESCE(phc.price_history_count, 0) < 2
                THEN 'Needs at least two price snapshots'

                ELSE 'Review price data'
            END AS market_note
        FROM inventory i
        JOIN card_printings cp
            ON i.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        LEFT JOIN PriceHistoryCounts phc
            ON i.scryfall_id = phc.scryfall_id
        LEFT JOIN tcgplayer_price_overrides o
            ON i.scryfall_id = o.scryfall_id
            AND LOWER(REPLACE(i.finish, '_', ' ')) = LOWER(REPLACE(o.finish, '_', ' '))
        GROUP BY
            i.scryfall_id,
            i.finish,
            cp.image_url,
            cp.set_code,
            cp.collector_number,
            cd.name,
            cp.current_price,
            cp.current_price_foil,
            phc.price_history_count
        HAVING
            (
                {foil_like_sql}
                AND (cp.current_price_foil IS NULL OR cp.current_price_foil = '')
            )
            OR
            (
                NOT ({foil_like_sql})
                AND (cp.current_price IS NULL OR cp.current_price = '')
            )
            OR COALESCE(phc.price_history_count, 0) < 2
        ORDER BY cd.name ASC
        LIMIT ?
    """

    return fetch_all_dicts(manager, query, (limit,))


# =========================================================
# Shared SQL Fragments
# =========================================================

PRICE_PAIR_CTE = """
WITH RankedPrices AS (
    SELECT
        scryfall_id,
        source,
        CAST(NULLIF(price_usd, '') AS REAL) AS price_usd,
        CAST(NULLIF(price_foil, '') AS REAL) AS price_foil,
        scraped_at,
        ROW_NUMBER() OVER (
            PARTITION BY scryfall_id, source
            ORDER BY scraped_at DESC
        ) AS rn
    FROM price_history
    WHERE source = 'tcgcsv'
),

PricePairs AS (
    SELECT
        curr.scryfall_id,

        prev.price_usd AS old_price_usd,
        curr.price_usd AS new_price_usd,

        prev.price_foil AS old_price_foil,
        curr.price_foil AS new_price_foil,

        curr.scraped_at AS latest_scraped_at,
        prev.scraped_at AS previous_scraped_at
    FROM RankedPrices curr
    LEFT JOIN RankedPrices prev
        ON curr.scryfall_id = prev.scryfall_id
        AND curr.source = prev.source
        AND prev.rn = 2
    WHERE curr.rn = 1
),

InventoryGrouped AS (
    SELECT
        i.scryfall_id,
        i.finish,
        MIN(i.instance_id) AS instance_id,
        MAX(i.location_id) AS location_id,
        COUNT(*) AS qty,
        MAX(COALESCE(i.is_tradeable, 0)) AS is_tradeable,
        MAX(COALESCE(i.is_surplus, 0)) AS is_surplus,
        SUM(COALESCE(i.purchase_price, 0)) AS total_purchase_price
    FROM inventory i
    GROUP BY i.scryfall_id, i.finish
),

MarketRows AS (
    SELECT
        ig.instance_id,
        ig.scryfall_id,
        ig.finish,
        ig.location_id,
        ig.qty,
        ig.is_tradeable,
        ig.is_surplus,
        ig.total_purchase_price,

        cp.image_url,
        cp.set_code,
        cp.collector_number,
        cd.name,

        CASE
            WHEN LOWER(REPLACE(COALESCE(ig.finish, ''), '_', ' ')) IN (
                'foil',
                'etched',
                'rainbow foil'
            )
            THEN pp.old_price_foil
            ELSE pp.old_price_usd
        END AS old_price,

        CASE
            WHEN LOWER(REPLACE(COALESCE(ig.finish, ''), '_', ' ')) IN (
            'foil',
            'etched',
            'rainbow foil'
        )
        THEN pp.new_price_foil
            ELSE pp.new_price_usd
        END AS new_price,

        pp.latest_scraped_at,
        pp.previous_scraped_at
    FROM InventoryGrouped ig
    JOIN PricePairs pp
        ON ig.scryfall_id = pp.scryfall_id
    LEFT JOIN card_printings cp
        ON ig.scryfall_id = cp.scryfall_id
    LEFT JOIN card_definitions cd
        ON cp.oracle_id = cd.oracle_id
)
"""


def market_base_cte(manager, trailing_comma=False):
    """
    Return a CTE that resolves PricePairs/MarketRows.

    When request-scoped temp cache tables are available, this avoids rerunning
    the expensive price_history window query for every dashboard section.
    """
    if getattr(manager, "_market_cache_ready", False):
        cte = """
WITH PricePairs AS (
    SELECT * FROM temp_market_price_pairs
),
MarketRows AS (
    SELECT * FROM temp_market_rows
)
"""
    else:
        cte = PRICE_PAIR_CTE

    if trailing_comma:
        return f"{cte.rstrip()}\n,"

    return cte


def prepare_market_query_cache(manager):
    """Build request-local temp tables used by market dashboard queries."""
    manager.cursor.executescript("""
        DROP TABLE IF EXISTS temp_market_price_pairs;
        DROP TABLE IF EXISTS temp_market_rows;

        CREATE TEMP TABLE temp_market_price_pairs AS
        WITH RankedPrices AS (
            SELECT
                scryfall_id,
                source,
                CAST(NULLIF(price_usd, '') AS REAL) AS price_usd,
                CAST(NULLIF(price_foil, '') AS REAL) AS price_foil,
                scraped_at,
                ROW_NUMBER() OVER (
                    PARTITION BY scryfall_id, source
                    ORDER BY scraped_at DESC
                ) AS rn
            FROM price_history
            WHERE source = 'tcgcsv'
        )
        SELECT
            curr.scryfall_id,
            prev.price_usd AS old_price_usd,
            curr.price_usd AS new_price_usd,
            prev.price_foil AS old_price_foil,
            curr.price_foil AS new_price_foil,
            curr.scraped_at AS latest_scraped_at,
            prev.scraped_at AS previous_scraped_at
        FROM RankedPrices curr
        LEFT JOIN RankedPrices prev
            ON curr.scryfall_id = prev.scryfall_id
            AND curr.source = prev.source
            AND prev.rn = 2
        WHERE curr.rn = 1;

        CREATE INDEX IF NOT EXISTS idx_tmp_market_price_pairs_scryfall
            ON temp_market_price_pairs(scryfall_id);

        CREATE TEMP TABLE temp_market_rows AS
        WITH InventoryGrouped AS (
            SELECT
                i.scryfall_id,
                i.finish,
                MIN(i.instance_id) AS instance_id,
                MAX(i.location_id) AS location_id,
                COUNT(*) AS qty,
                MAX(COALESCE(i.is_tradeable, 0)) AS is_tradeable,
                MAX(COALESCE(i.is_surplus, 0)) AS is_surplus,
                SUM(COALESCE(i.purchase_price, 0)) AS total_purchase_price
            FROM inventory i
            GROUP BY i.scryfall_id, i.finish
        )
        SELECT
            ig.instance_id,
            ig.scryfall_id,
            ig.finish,
            ig.location_id,
            ig.qty,
            ig.is_tradeable,
            ig.is_surplus,
            ig.total_purchase_price,
            cp.image_url,
            cp.set_code,
            cp.collector_number,
            cd.name,
            CASE
                WHEN LOWER(REPLACE(COALESCE(ig.finish, ''), '_', ' ')) IN (
                    'foil',
                    'etched',
                    'rainbow foil'
                )
                THEN pp.old_price_foil
                ELSE pp.old_price_usd
            END AS old_price,
            CASE
                WHEN LOWER(REPLACE(COALESCE(ig.finish, ''), '_', ' ')) IN (
                    'foil',
                    'etched',
                    'rainbow foil'
                )
                THEN pp.new_price_foil
                ELSE pp.new_price_usd
            END AS new_price,
            pp.latest_scraped_at,
            pp.previous_scraped_at
        FROM InventoryGrouped ig
        JOIN temp_market_price_pairs pp
            ON ig.scryfall_id = pp.scryfall_id
        LEFT JOIN card_printings cp
            ON ig.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id;

        CREATE INDEX IF NOT EXISTS idx_tmp_market_rows_tradeable
            ON temp_market_rows(is_tradeable);
        CREATE INDEX IF NOT EXISTS idx_tmp_market_rows_surplus
            ON temp_market_rows(is_surplus);
        CREATE INDEX IF NOT EXISTS idx_tmp_market_rows_scryfall_finish
            ON temp_market_rows(scryfall_id, finish);
    """)

    manager._market_cache_ready = True


# =========================================================
# Market Data Helpers
# =========================================================

def get_market_summary(manager):
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            COALESCE(SUM(new_price * qty), 0) AS collection_value,

            COALESCE(SUM(
                CASE
                    WHEN old_price IS NOT NULL
                        AND old_price > 0
                        AND new_price > old_price
                    THEN (new_price - old_price) * qty
                    ELSE 0
                END
            ), 0) AS total_gains,

            COALESCE(SUM(
                CASE
                    WHEN old_price IS NOT NULL
                        AND old_price > 0
                        AND new_price < old_price
                    THEN (old_price - new_price) * qty
                    ELSE 0
                END
            ), 0) AS total_losses,

            COALESCE(SUM(
                CASE
                    WHEN is_tradeable = 1
                    THEN new_price * qty
                    ELSE 0
                END
            ), 0) AS trade_value,

            COUNT(
                CASE
                    WHEN old_price IS NOT NULL
                    AND new_price IS NOT NULL
                    AND old_price > 0
                    AND (
                        new_price >= ?
                        OR old_price >= ?
                        OR ABS((new_price - old_price) * qty) >= ?
                    )
                    AND (
                        ABS(new_price - old_price) >= ?
                        OR ABS(((new_price - old_price) / old_price) * 100) >= ?
                    )
                    THEN 1
                END
            ) AS alert_count,

            COUNT(DISTINCT scryfall_id) AS tracked_count,

            MAX(latest_scraped_at) AS last_sync
        FROM MarketRows
        WHERE new_price IS NOT NULL
    """

    row = fetch_one_dict(manager, query, movement_threshold_params())

    return {
        "last_sync": row.get("last_sync") or "Not yet synced",
        "collection_value": row.get("collection_value") or 0,
        "total_gains": row.get("total_gains") or 0,
        "total_losses": row.get("total_losses") or 0,
        "trade_value": row.get("trade_value") or 0,
        "alert_count": row.get("alert_count", 0),
        "tracked_count": row.get("tracked_count", 0),
        "missing_price_count": get_missing_price_count(manager),
        "wishlist_drop_count": 0,
    }


def get_market_movers(
    manager,
    limit=MARKET_MOVER_LIMIT,
    market_filter="all",
    market_sort="owned_impact"
):
    filter_sql = get_market_filter_sql(market_filter)
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            instance_id,
            scryfall_id,
            finish,
            location_id,
            qty,
            is_tradeable,
            is_surplus,

            image_url,
            set_code,
            collector_number,
            name,

            old_price,
            new_price,

            ROUND(new_price - old_price, 2) AS price_delta,

            ROUND(
                CASE
                    WHEN old_price IS NOT NULL AND old_price > 0
                    THEN ((new_price - old_price) / old_price) * 100
                    ELSE 0
                END,
                1
            ) AS percent_delta,

            ROUND((new_price - old_price) * qty, 2) AS owned_impact,

            latest_scraped_at,
            previous_scraped_at
        FROM MarketRows
        WHERE
            old_price IS NOT NULL
            AND new_price IS NOT NULL
            AND old_price > 0
            AND (
                new_price >= ?
                OR old_price >= ?
                OR ABS((new_price - old_price) * qty) >= ?
            )
            AND (
                ABS(new_price - old_price) >= ?
                OR ABS(((new_price - old_price) / old_price) * 100) >= ?
            )
            {filter_sql}
        ORDER BY {sort_sql}
        LIMIT ?
    """

    movers = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in movers:
        decorate_market_card(card)

    spikes = [card for card in movers if card.get("price_delta", 0) > 0]
    drops = [card for card in movers if card.get("price_delta", 0) < 0]

    return spikes, drops


def get_trade_alerts(
    manager,
    limit=MARKET_TRADE_ALERT_LIMIT,
    market_sort="owned_impact"
):
    sort_sql = get_market_sort_sql(market_sort)
    cte_sql = market_base_cte(manager)

    query = f"""
        {cte_sql}

        SELECT
            instance_id,
            scryfall_id,
            finish,
            location_id,
            qty,
            is_tradeable,
            is_surplus,

            image_url,
            set_code,
            collector_number,
            name,

            old_price,
            new_price,

            ROUND(new_price - old_price, 2) AS price_delta,

            ROUND(
                CASE
                    WHEN old_price IS NOT NULL AND old_price > 0
                    THEN ((new_price - old_price) / old_price) * 100
                    ELSE 0
                END,
                1
            ) AS percent_delta,

            ROUND((new_price - old_price) * qty, 2) AS owned_impact,

            latest_scraped_at,
            previous_scraped_at
        FROM MarketRows
        WHERE
            old_price IS NOT NULL
            AND new_price IS NOT NULL
            AND old_price > 0
            AND is_tradeable = 1
            AND (
                new_price >= ?
                OR old_price >= ?
                OR ABS((new_price - old_price) * qty) >= ?
            )
            AND (
                ABS(new_price - old_price) >= ?
                OR ABS(((new_price - old_price) / old_price) * 100) >= ?
            )
        ORDER BY {sort_sql}
        LIMIT ?
    """

    cards = fetch_all_dicts(
        manager,
        query,
        movement_threshold_params_with_limit(limit)
    )

    for card in cards:
        decorate_market_card(card)

    return cards

def get_missing_price_count(manager):
    foil_like_sql = FOIL_LIKE_FINISH_SQL.format(finish_column="i.finish")

    query = f"""
        SELECT COUNT(*) AS missing_count
        FROM inventory i
        JOIN card_printings cp
            ON i.scryfall_id = cp.scryfall_id
        WHERE
            CASE
                WHEN {foil_like_sql}
                THEN cp.current_price_foil IS NULL OR cp.current_price_foil = ''
                ELSE cp.current_price IS NULL OR cp.current_price = ''
            END
    """

    row = fetch_one_dict(manager, query)
    return row.get("missing_count", 0)


def get_missing_price_cards(manager, limit=MARKET_MISSING_PRICE_LIMIT):
    foil_like_sql = FOIL_LIKE_FINISH_SQL.format(finish_column="i.finish")

    query = f"""
        SELECT
            MIN(i.instance_id) AS instance_id,
            i.scryfall_id,
            i.finish,
            MAX(i.location_id) AS location_id,
            COUNT(*) AS qty,

            cp.image_url,
            cp.set_code,
            cp.collector_number,
            cd.name,

            MAX(COALESCE(i.is_tradeable, 0)) AS is_tradeable,
            MAX(COALESCE(i.is_surplus, 0)) AS is_surplus
        FROM inventory i
        JOIN card_printings cp
            ON i.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE
            CASE
                WHEN {foil_like_sql}
                THEN cp.current_price_foil IS NULL OR cp.current_price_foil = ''
                ELSE cp.current_price IS NULL OR cp.current_price = ''
            END
        GROUP BY i.scryfall_id, i.finish
        ORDER BY cd.name ASC
        LIMIT ?
    """

    return fetch_all_dicts(manager, query, (limit,))


def get_market_opportunities(
    manager,
    limit=MARKET_OPPORTUNITY_LIMIT,
    market_filter="all",
    market_sort="owned_impact",
    spikes=None,
    drops=None,
    trade_alerts=None,
    missing_count=None,
):
    """
    Builds lightweight opportunity cards from already-trackable market movement.

    Wishlist/deck/set-completion intelligence should be layered in later
    after those data sources are wired into the market system.
    """
    if spikes is None or drops is None:
        spikes, drops = get_market_movers(
            manager,
            limit=24,
            market_filter=market_filter,
            market_sort=market_sort,
        )

    if trade_alerts is None:
        trade_alerts = get_trade_alerts(
            manager,
            limit=12,
            market_sort=market_sort,
        )

    if missing_count is None:
        missing_count = get_missing_price_count(manager)

    opportunities = []

    strongest_trade_spike = next(
        (
            card for card in trade_alerts
            if card.get("price_delta", 0) > 0
        ),
        None
    )

    if strongest_trade_spike:
        opportunities.append({
            "type": "Trade Alert",
            "title": f"{strongest_trade_spike.get('name', 'A tradeable card')} moved up",
            "description": (
                f"{strongest_trade_spike.get('name', 'This card')} is marked tradeable "
                f"and moved {strongest_trade_spike.get('formatted_delta')} "
                f"({strongest_trade_spike.get('formatted_percent_delta')})."
            ),
            "action": "Consider reviewing surplus or trade binder copies."
        })

    strongest_drop = drops[0] if drops else None

    if strongest_drop:
        opportunities.append({
            "type": "Price Drop",
            "title": f"{strongest_drop.get('name', 'A card')} moved down",
            "description": (
                f"{strongest_drop.get('name', 'This card')} dropped "
                f"{strongest_drop.get('formatted_delta')} "
                f"({strongest_drop.get('formatted_percent_delta')})."
            ),
            "action": "Check whether this is a buy opportunity or a normal correction."
        })

    strongest_spike = spikes[0] if spikes else None

    if strongest_spike:
        opportunities.append({
            "type": "Collection Gain",
            "title": f"{strongest_spike.get('name', 'A card')} gained value",
            "description": (
                f"Your tracked copies gained an estimated "
                f"{strongest_spike.get('formatted_owned_impact')} in collection impact."
            ),
            "action": "Review high-value copies for trade, deck, or storage decisions."
        })

    if missing_count:
        opportunities.append({
            "type": "Data Quality",
            "title": f"{missing_count} cards are missing price data",
            "description": (
                "Some cards could not be valued from the current stored price fields."
            ),
            "action": "Review missing prices before relying on total collection value."
        })

    return opportunities[:limit]


# =========================================================
# Data Decoration
# =========================================================

def decorate_market_card(card):
    """
    Adds formatted fields for templates without disturbing the original card data.
    """
    old_price = card.get("old_price")
    new_price = card.get("new_price")
    price_delta = card.get("price_delta")
    percent_delta = card.get("percent_delta")
    owned_impact = card.get("owned_impact")

    card["formatted_old_price"] = money(old_price)
    card["formatted_new_price"] = money(new_price)
    card["formatted_delta"] = format_signed_money(price_delta)
    card["formatted_percent_delta"] = format_signed_percent(percent_delta)
    card["formatted_owned_impact"] = format_signed_money(owned_impact)

    if price_delta is None:
        card["market_direction"] = "neutral"
    elif price_delta > 0:
        card["market_direction"] = "gain"
    elif price_delta < 0:
        card["market_direction"] = "loss"
    else:
        card["market_direction"] = "neutral"

    return card


def format_signed_money(value):
    if value is None:
        return "$0.00"

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "$0.00"

    sign = "+" if numeric > 0 else ""
    return f"{sign}${numeric:,.2f}"


def format_signed_percent(value):
    if value is None:
        return "0.0%"

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "0.0%"

    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:,.1f}%"


def get_deferred_market_sections(
    manager,
    section_visibility,
    market_sort,
    trade_alerts=None,
):
    wishlist_drops = []
    deck_alerts = []
    planeswalker_alerts = []
    surplus_alerts = []
    purchase_alerts = []
    price_quality_flags = []

    if section_visibility["show_wishlist_sections"]:
        wishlist_drops = get_wishlist_drops(
            manager,
            market_sort=market_sort,
        )

    if section_visibility["show_deck_sections"]:
        deck_alerts = get_deck_market_alerts(
            manager,
            market_sort=market_sort,
        )

    if section_visibility["show_planeswalker_sections"]:
        planeswalker_alerts = get_planeswalker_market_alerts(
            manager,
            market_sort=market_sort,
        )

    if section_visibility["show_trade_sections"]:
        if trade_alerts is None:
            trade_alerts = get_trade_alerts(
                manager,
                market_sort=market_sort,
            )

        surplus_alerts = get_surplus_market_alerts(
            manager,
            market_sort=market_sort,
        )

    if section_visibility["show_owned_sections"]:
        purchase_alerts = get_purchase_gain_loss_alerts(manager)

    if section_visibility["show_data_quality_sections"]:
        price_quality_flags = get_price_quality_flags(manager)

    return {
        "trade_alerts": trade_alerts or [],
        "wishlist_drops": wishlist_drops,
        "deck_alerts": deck_alerts,
        "planeswalker_alerts": planeswalker_alerts,
        "surplus_alerts": surplus_alerts,
        "purchase_alerts": purchase_alerts,
        "price_quality_flags": price_quality_flags,
    }


# =========================================================
# Routes
# =========================================================

@markets_bp.route("/market/dashboard", methods=["GET"])
@login_required
def market_dashboard():
    manager = get_db()

    market_filter = get_market_filter()
    market_sort = get_market_sort()

    section_visibility = {
        "show_owned_sections": market_filter in ("all", "owned"),
        "show_trade_sections": market_filter in ("all", "tradeable"),
        "show_wishlist_sections": market_filter in ("all", "wishlist"),
        "show_deck_sections": market_filter in ("all", "decks"),
        "show_planeswalker_sections": market_filter in ("all", "planeswalkers"),
        "show_data_quality_sections": market_filter in ("all", "owned"),
    }

    defer_sections = request.args.get("defer_sections", "1") != "0"

    try:
        try:
            prepare_market_query_cache(manager)
        except Exception:
            manager._market_cache_ready = False

        market_summary = get_market_summary(manager)

        spikes, drops = get_market_movers(
            manager,
            market_filter=market_filter,
            market_sort=market_sort,
        )

        trade_alerts = get_trade_alerts(
            manager,
            market_sort=market_sort,
        )

        opportunities = get_market_opportunities(
            manager,
            market_filter=market_filter,
            market_sort=market_sort,
            spikes=spikes,
            drops=drops,
            trade_alerts=trade_alerts,
            missing_count=market_summary.get("missing_price_count", 0),
        )

        deferred_sections = {
            "trade_alerts": [],
            "wishlist_drops": [],
            "deck_alerts": [],
            "planeswalker_alerts": [],
            "surplus_alerts": [],
            "purchase_alerts": [],
            "price_quality_flags": [],
        }

        if not defer_sections:
            deferred_sections = get_deferred_market_sections(
                manager,
                section_visibility=section_visibility,
                market_sort=market_sort,
                trade_alerts=trade_alerts,
            )

        return render_template(
            "market.html",
            market_summary=market_summary,
            opportunities=opportunities,
            spikes=spikes,
            drops=drops,
            trade_alerts=deferred_sections["trade_alerts"],
            wishlist_drops=deferred_sections["wishlist_drops"],
            deck_alerts=deferred_sections["deck_alerts"],
            planeswalker_alerts=deferred_sections["planeswalker_alerts"],
            surplus_alerts=deferred_sections["surplus_alerts"],
            purchase_alerts=deferred_sections["purchase_alerts"],
            price_quality_flags=deferred_sections["price_quality_flags"],
            section_visibility=section_visibility,
            market_filter=market_filter,
            market_sort=market_sort,
            defer_sections=defer_sections,
            view_mode="tracking",
        )

    finally:
        manager.close()


@markets_bp.route("/market/dashboard/deferred-sections", methods=["GET"])
@login_required
def market_dashboard_deferred_sections():
    manager = get_db()

    market_filter = get_market_filter()
    market_sort = get_market_sort()

    section_visibility = {
        "show_owned_sections": market_filter in ("all", "owned"),
        "show_trade_sections": market_filter in ("all", "tradeable"),
        "show_wishlist_sections": market_filter in ("all", "wishlist"),
        "show_deck_sections": market_filter in ("all", "decks"),
        "show_planeswalker_sections": market_filter in ("all", "planeswalkers"),
        "show_data_quality_sections": market_filter in ("all", "owned"),
    }

    try:
        try:
            prepare_market_query_cache(manager)
        except Exception:
            manager._market_cache_ready = False

        deferred_sections = get_deferred_market_sections(
            manager,
            section_visibility=section_visibility,
            market_sort=market_sort,
        )

        return render_template(
            "_market_deferred_sections.html",
            section_visibility=section_visibility,
            trade_alerts=deferred_sections["trade_alerts"],
            wishlist_drops=deferred_sections["wishlist_drops"],
            deck_alerts=deferred_sections["deck_alerts"],
            planeswalker_alerts=deferred_sections["planeswalker_alerts"],
            surplus_alerts=deferred_sections["surplus_alerts"],
            purchase_alerts=deferred_sections["purchase_alerts"],
            price_quality_flags=deferred_sections["price_quality_flags"],
        )
    finally:
        manager.close()


@markets_bp.route("/run-price-update")
@login_required
@admin_required
def run_price_update():
    return Response(update_prices(), mimetype="text/event-stream")


# =========================================================
# Price Sync
# =========================================================

def update_prices():
    manager = CardDB()
    session = requests.Session()
    session.headers.update(TCGCSV_HEADERS)

    try:
        yield sse_message(5, "Checking for newest local TCGCSV price CSV...")

        refresh_result = refresh_current_day_history_csv_if_due()

        if refresh_result.get("attempted"):
            yield sse_message(7, refresh_result.get("message", "Checked for current-day CSV."))
        else:
            yield sse_message(7, refresh_result.get("message", "Using newest existing local CSV."))

        current_snapshot_path = resolve_local_price_snapshot_path()
        tcgcsv_price_date = get_price_date_for_snapshot(current_snapshot_path)
        tcgcsv_timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

        yield sse_message(
            10,
            f"Using local TCGCSV file {current_snapshot_path} for price date {tcgcsv_price_date}."
        )

        local_rows = get_local_cards_needing_prices(manager)

        if not local_rows:
            manager.log_update(
                task_name="TCGCSV Price Sync",
                cards_updated=0,
                status="Warning",
                message="No cards with TCGplayer IDs found. Run the TCGCSV/Scryfall ID migration first."
            )
            yield sse_message(
                100,
                "No cards with TCGplayer IDs found. Run the TCGCSV/Scryfall ID migration first."
            )
            return

        total_cards = len(local_rows)
        updated_count = 0
        missing_count = 0
        updated_scryfall_ids = set()

        yield sse_message(
            15,
            f"Preparing local TCGCSV group lookup for {total_cards} local card targets..."
        )

        grouped_targets = get_grouped_local_price_targets(
            manager,
            session,
            local_rows,
            allow_remote_group_lookup=False,
            snapshot_path=current_snapshot_path,
        )

        if not grouped_targets:
            manager.log_update(
                task_name="TCGCSV Price Sync",
                cards_updated=0,
                status="Warning",
                message="No TCGCSV group matches found in local CSV for your local TCGplayer product IDs."
            )
            yield sse_message(
                100,
                "No TCGCSV group matches found in local CSV for your local TCGplayer product IDs."
            )
            return

        group_items = list(grouped_targets.items())
        total_groups = len(group_items)

        yield sse_message(
            20,
            f"Updating prices across {total_groups} local TCGCSV groups..."
        )

        for group_index, (group_id, targets_by_scryfall_id) in enumerate(group_items, start=1):
            prices = get_tcgcsv_prices_for_group_with_fallback(
                session,
                group_id,
                data_source=TCGCSV_SOURCE_LOCAL_ONLY,
                snapshot_path=current_snapshot_path,
            )

            if not prices:
                yield sse_message(
                    int(20 + (group_index / total_groups) * 78),
                    f"Skipped group {group_id}; no local prices found."
                )
                time.sleep(TCGCSV_RATE_LIMIT_DELAY)
                continue

            prices_by_product_id = build_prices_by_product_id(prices)

            for target in targets_by_scryfall_id.values():
                scryfall_id = target["scryfall_id"]
                nonfoil_price, foil_price = prices_for_target(target, prices_by_product_id)

                if nonfoil_price is None and foil_price is None:
                    missing_count += 1
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
                    tcgcsv_price_date,
                    "tcgcsv",
                ))

                updated_count += 1
                updated_scryfall_ids.add(scryfall_id)

            manager.commit()

            yield sse_message(
                int(20 + (group_index / total_groups) * 78),
                f"Processed {group_index}/{total_groups} local TCGCSV groups."
            )

            time.sleep(TCGCSV_RATE_LIMIT_DELAY)

        backfilled_count = 0

        if updated_scryfall_ids:
            backfilled_count = backfill_prior_history_if_needed(
                manager=manager,
                session=session,
                grouped_targets=grouped_targets,
                updated_scryfall_ids=updated_scryfall_ids,
                current_snapshot_path=current_snapshot_path,
                min_history_points=2,
            )
            manager.commit()

        manager.log_update(
            task_name="TCGCSV Price Sync",
            cards_updated=updated_count,
            status="Success",
            message=(
                f"Updated {updated_count} cards from local CSV date {tcgcsv_price_date}. "
                f"Backfilled {backfilled_count} prior history rows. "
                f"Missing prices for {missing_count} cards."
            )
        )

        yield sse_message(
            100,
            (
                f"TCGCSV sync complete. Updated {updated_count} cards from local CSV date "
                f"{tcgcsv_price_date}. Backfilled {backfilled_count} prior rows. "
                f"Missing prices for {missing_count} cards."
            )
        )

    except Exception as error:
        manager.log_update(
            task_name="TCGCSV Price Sync",
            cards_updated=0,
            status="Error",
            message=str(error)
        )
        yield sse_message(100, f"TCGCSV sync failed: {error}")

    finally:
        manager.close()



def progress_for_index(index, total_cards):
    if total_cards <= 0:
        return 100

    return int(15 + (index / total_cards) * 80)


def sse_message(progress, status):
    return f"data: {json.dumps({'progress': progress, 'status': status})}\n\n"