from flask import Blueprint, render_template
from db.db_manager import CardDB
from routes.markets import get_market_summary

main_bp = Blueprint('main', __name__)



def fetch_one(cursor, query, params=()):
    return cursor.execute(query, params).fetchone()


def fetch_all(cursor, query, params=()):
    return cursor.execute(query, params).fetchall()

def get_collection_value(manager):
    value_query = """
        SELECT 
            SUM(
                CASE
                    WHEN LOWER(COALESCE(i.finish, '')) IN ('foil', 'etched', 'foil_etched')
                        THEN COALESCE(cp.current_price_foil, cp.current_price, 0)
                    ELSE COALESCE(cp.current_price, cp.current_price_foil, 0)
                END
            ) AS total_value
        FROM inventory i
        JOIN card_printings cp 
            ON i.scryfall_id = cp.scryfall_id
    """

    return manager.cursor.execute(value_query).fetchone()["total_value"] or 0.0

@main_bp.route('/')
def index():
    manager = CardDB()

    try:
        # Keep route paths centralized so public landing-page links are easy to adjust
        # if your app uses slightly different route URLs.
        links = {
            "trade_binder": "/binder/trades",
            "create_trade": "/trade",
            "wishlist": "/wishlist",
            "sets": "/sets",
            "commander": "/edh/gallery",
            "planeswalkers": "/collection/planeswalkers",
            "inventory": "/inventory",
            "search": "/inventory",
            "market_dashboard": "/market/dashboard",
            "admin_dashboard": "/admin/dashboard",
        }

        market_summary = get_market_summary(manager)
        
        
        stats = {
            'total_cards': fetch_one(manager.cursor, 'SELECT COUNT(*) FROM inventory')[0] or 0,
            'unique_cards': fetch_one(manager.cursor, 'SELECT COUNT(DISTINCT scryfall_id) FROM inventory')[0] or 0,
            'tradeable_cards': fetch_one(manager.cursor, '''
                SELECT COUNT(*)
                FROM inventory
                WHERE COALESCE(is_tradeable, 0) = 1
            ''')[0] or 0,
            'total_value' : market_summary["collection_value"],
            'wishlist_cards': fetch_one(manager.cursor, 'SELECT COUNT(*) FROM wishlist')[0] or 0,
        }
        

        featured_trade_cards = fetch_all(manager.cursor, '''
            SELECT
                i.scryfall_id,
                i.finish,
                cp.image_url,
                cp.set_code,
                cp.collector_number,
                cd.name,
                COALESCE(cp.current_price, cp.current_price_foil, 0) AS sort_price
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            WHERE COALESCE(i.is_tradeable, 0) = 1
              AND cp.image_url IS NOT NULL
            GROUP BY i.scryfall_id, i.finish
            ORDER BY sort_price DESC, MAX(i.instance_id) DESC
            LIMIT 8
        ''')

        wishlist_highlights = fetch_all(manager.cursor, '''
            SELECT
                w.scryfall_id,
                w.finish,
                w.priority,
                w.notes,
                w.non_specific,
                cp.image_url,
                cp.set_code,
                cp.collector_number,
                cd.name
            FROM wishlist w
            JOIN card_printings cp ON w.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            WHERE cp.image_url IS NOT NULL
            ORDER BY COALESCE(w.priority, 1) DESC, w.added DESC, cd.name ASC
            LIMIT 6
        ''')

        recent_cards = fetch_all(manager.cursor, '''
            SELECT
                cp.image_url,
                cp.set_code,
                cp.collector_number,
                cd.name
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            WHERE cp.image_url IS NOT NULL
            GROUP BY cp.scryfall_id
            ORDER BY MAX(i.instance_id) DESC
            LIMIT 10
        ''')

        current_set_projects = fetch_all(manager.cursor, '''
            SELECT
                s.set_code,
                s.set_name,
                s.released_at,
                COUNT(DISTINCT cp.scryfall_id) AS owned_printings
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN sets s ON cp.set_code = s.set_code
            GROUP BY s.set_code, s.set_name, s.released_at
            ORDER BY s.released_at DESC
            LIMIT 4
        ''')

        spotlight = fetch_one(manager.cursor, '''
            SELECT cp.image_url, cd.name, cp.set_code, s.set_name
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            JOIN sets s ON cp.set_code = s.set_code
            WHERE cp.image_url IS NOT NULL
            ORDER BY RANDOM()
            LIMIT 1
        ''')

    finally:
        manager.close()

    return render_template(
        'index.html',
        links=links,
        stats=stats,
        spotlight=spotlight,
        featured_trade_cards=featured_trade_cards,
        wishlist_highlights=wishlist_highlights,
        recent_cards=recent_cards,
        current_set_projects=current_set_projects,
    )
