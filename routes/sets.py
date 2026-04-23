from flask import Blueprint, request, redirect, url_for, render_template
from db.db_manager import CardDB
from datetime import datetime, timedelta

sets_bp = Blueprint('sets', __name__)

@sets_bp.route('/sets', methods=['GET', 'POST'])
def set_gallery():
    manager = CardDB()
    # Pull Standard sets first, then Legacy, both sorted by date
    query = """
        SELECT 
            s.*,
            (
                SELECT SUM(
                    CASE 
                        WHEN finish = "foil" THEN COALESCE(cp_val.current_price_foil, 0)
                        ELSE COALESCE(cp_val.current_price, 0)
                    END
                )
                FROM inventory i
                JOIN card_printings cp_val ON i.scryfall_id = cp_val.scryfall_id
                WHERE cp_val.set_code = s.set_code 
                   OR cp_val.set_code = 'p' || s.set_code
            ) as set_value
        FROM sets s
        WHERE EXISTS (
            SELECT 1 
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            WHERE cp.set_code = s.set_code
        )
        AND s.set_type NOT IN ('promo', 'token', 'memorabilia')
        ORDER BY standard_legal DESC, released_at DESC
    """
    
    sets = manager.cursor.execute(query).fetchall()
    manager.close()
    return render_template('set_gallery.html', sets=sets)

@sets_bp.route('/set/<set_code>')
def set_detail(set_code):
    manager = CardDB()
    # 1. Get every card that exists in that set
    # 2. Join with instances to count how many you have
    # Use this query in your route
    # The updated query for your set_detail route
    query = """
        SELECT 
            cd.name, 
            cp.collector_number, 
            cp.image_url, 
            cp.scryfall_id,
            -- Counts all versions of this card name within the specific set and its promo counterpart
            (SELECT COUNT(*) 
            FROM inventory i 
            JOIN card_printings cp2 ON i.scryfall_id = cp2.scryfall_id 
            WHERE cp2.oracle_id = cp.oracle_id 
            AND (cp2.set_code = cp.set_code OR cp2.set_code = 'p' || cp.set_code)
            ) as owned_count
        FROM card_printings cp
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE cp.set_code = ?
        -- Logic to show only the primary version of the card in the list
        AND cp.collector_number = (
            SELECT MIN(CAST(inner_cp.collector_number AS INTEGER))
            FROM card_printings inner_cp
            WHERE inner_cp.oracle_id = cp.oracle_id 
            AND inner_cp.set_code = cp.set_code
        )
        GROUP BY cd.name
        ORDER BY CAST(cp.collector_number AS INTEGER) ASC
    """

    # Fetch the set info using .fetchone() to avoid the "set is undefined" list error
    query_set_info = "SELECT set_code, set_name, standard_legal FROM sets WHERE set_code = ?"
    cards = manager.cursor.execute(query, (set_code,)).fetchall()
    set_info = manager.cursor.execute(query_set_info, (set_code,)).fetchone()
    manager.close()

    return render_template('set_detail.html', cards=cards, set=set_info, set_code=set_code)