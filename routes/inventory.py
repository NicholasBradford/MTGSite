from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import login_required
from db.db_manager import CardDB
from search import search
import sqlite3, ScryfallFetcher, db.db_manager, re
from services.tcgcsv_prices import search_tcgcsv_products_for_finish, update_single_card_price_from_tcgcsv, normalize_finish

sort_options = {
        'name': """
            LOWER(
                REPLACE(
                    CASE 
                        WHEN cd.name LIKE 'The %' THEN SUBSTR(cd.name, 5)
                        WHEN cd.name LIKE 'An %' THEN SUBSTR(cd.name, 4)
                        WHEN cd.name LIKE 'A %' THEN SUBSTR(cd.name, 3)
                        ELSE cd.name 
                    END,
                    ' ', ''
                )
            ) ASC
        """,
        'set': 'cp.set_code ASC, cd.name ASC',
        'price': """
            CASE 
                WHEN LOWER(REPLACE(COALESCE(i.finish, ''), '_', ' ')) IN (
                    'foil',
                    'etched',
                    'rainbow foil'
                )
                THEN cp.current_price_foil
                ELSE cp.current_price 
            END DESC
        """,
        'added': 'i.added DESC'
    }

def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route('/inventory', methods=['GET', 'POST'])
def inventory():
    search_query = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50 
    offset = (page - 1) * per_page
    
    manager = CardDB()

    params, filter_sql, having_sql, having_params, sort_sql = search(search_query)
    sort_by = request.args.get('sort', 'name')
    
    if sort_by not in sort_options:
        sort_by = 'name'

# 3. Extract the SPECIFIC SQL clause to use
    active_sort = sort_options[sort_by]

    # 2. THE QUERIES
    count_query = f'''
        SELECT COUNT(*) FROM (
            SELECT i.scryfall_id 
            FROM inventory i 
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            {filter_sql}
            GROUP BY i.scryfall_id, i.finish
            {having_sql}
        )
    '''
    total_items = manager.cursor.execute(count_query, params + having_params).fetchone()[0]
    total_pages = (total_items + per_page - 1) // per_page
    
    

    main_query = f'''
        SELECT 
            i.scryfall_id, 
            i.instance_id, 
            i.location_id, 
            i.is_tradeable,
            cd.name, 
            cp.image_url, 
            cp.set_code, 
            cp.collector_number,
            i.finish, 
            i.instance_id,
            cd.type_line, 
            cd.cmc, 
            cd.mana_cost,
            COUNT(i.instance_id) as qty,
            CASE 
                WHEN LOWER(REPLACE(COALESCE(i.finish, ''), '_', ' ')) IN (
                    'foil',
                    'etched',
                    'rainbow foil'
                )
                THEN cp.current_price_foil
                ELSE cp.current_price 
            END as price,
            i.finish, COUNT(*) as qty 
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        {filter_sql}
        GROUP BY i.scryfall_id, i.finish
        {having_sql}
        ORDER BY {sort_sql}, CAST(cp.collector_number AS INTEGER) ASC, i.finish DESC
        LIMIT ? OFFSET ?
    '''    

    cards = manager.cursor.execute(main_query, params + having_params + [per_page, offset]).fetchall()

    query_locs = 'SELECT location_id as id, name FROM locations ORDER BY name'
    locs = manager.cursor.execute(query_locs).fetchall()

    manager.close()

    card_list = [dict(row) for row in cards]
    loc_list = [dict(row) for row in locs]

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        view_mode = request.headers.get(
            "X-View-Mode",
            request.args.get("view_mode", "grid")
        )

        if view_mode == "table":
            return render_template(
                "_table_rows.html",
                cards=card_list,
                view_mode="inventory",
                locations=loc_list
            )

        return render_template(
            "_card_items.html",
            cards=card_list,
            view_mode="inventory",
            locations=loc_list
        )

    return render_template('inventory.html', 
                        cards=card_list, 
                        locations=loc_list, 
                        view_mode='inventory',
                        page=page,
                        total_pages=total_pages,
                        search_query=search_query)

@inventory_bp.route('/edit_instance/<int:instance_id>', methods=['POST'])
@login_required
def edit_instance(instance_id):
    manager = CardDB()

    new_loc = request.form.get('location_id')
    new_trade = request.form.get('is_tradeable')
    new_finish = request.form.get("finish")

    if new_loc is not None:
        manager.cursor.execute(
            "UPDATE inventory SET location_id = ? WHERE instance_id = ?",
            (new_loc, instance_id)
        )

    if new_trade is not None:
        trade_val = 1 if new_trade in ['1', 'true'] else 0
        manager.cursor.execute(
            "UPDATE inventory SET is_tradeable = ? WHERE instance_id = ?",
            (trade_val, instance_id)
        )

    if new_finish is not None:
        manager.cursor.execute(
            "UPDATE inventory SET finish = ? WHERE instance_id = ?",
            (normalize_finish(new_finish), instance_id)
        )

    manager.commit()
    manager.close()

    return {"status": "success"}, 200

@inventory_bp.route('/get_instances/<scryfall_id>/<finish>')
@login_required
def get_instances(scryfall_id, finish):
    manager = CardDB()
    
    query = '''
        SELECT i.scryfall_id, i.instance_id, i.location_id, i.is_tradeable, i.finish,
        cp.current_price, cp.current_price_foil, l.name as location_name
        FROM inventory i
        JOIN locations l ON i.location_id = l.location_id
        JOIN card_printings cp on i.scryfall_id = cp.scryfall_id
        WHERE i.scryfall_id = ? AND i.finish = ?
    '''
    
    # Ensure your cursor is configured to return row objects that can be converted to dicts
    # e.g., if using sqlite3: manager.connection.row_factory = sqlite3.Row
    rows = manager.cursor.execute(query, (scryfall_id, finish)).fetchall()
    manager.close()
    
    # Convert rows to a list of dictionaries
    instances_list = [dict(row) for row in rows]
    
    # Return using jsonify to ensure correct headers for fetch()
    return jsonify({"instances": instances_list})

# @inventory_bp.route('/inventory/table', methods=['GET'])
# @login_required
# def inventory_table():
#     search_query = request.args.get('q', '').strip()
#     page = request.args.get('page', 1, type=int)
#     per_page = 50 
#     offset = (page - 1) * per_page
    
#     manager = CardDB()

#     # Reusing your existing search logic
#     params, filter_sql, having_sql, having_params, sort_sql = search(search_query)
#     sort_by = request.args.get('sort', 'name')
    
#     if sort_by not in sort_options:
#         sort_by = 'name'

#         # Calculate Total Pages for pagination
#     count_query = f'''
#         SELECT COUNT(*) FROM (
#             SELECT i.scryfall_id 
#             FROM inventory i 
#             JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
#             JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
#             {filter_sql}
#             GROUP BY i.scryfall_id, i.finish
#             {having_sql}
#         )
#     '''
#     total_items = manager.cursor.execute(count_query, params + having_params).fetchone()[0]
#     total_pages = (total_items + per_page - 1) // per_page
    
#     main_query = f'''
#         SELECT 
#             i.scryfall_id, 
#             i.finish, 
#             i.location_id,
#             i.is_tradeable,
#             i.instance_id,
#             cd.name, 
#             cd.type_line, 
#             cd.cmc, 
#             cp.collector_number,
#             cd.mana_cost,
#             cp.set_code, 
#             COUNT(i.instance_id) as qty,
#             CASE 
#                 WHEN i.finish = 'foil' THEN cp.current_price_foil
#                 WHEN i.finish = 'etched' THEN cp.current_price_foil 
#                 ELSE cp.current_price 
#             END as price
#         FROM inventory i 
#         JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
#         JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
#         {filter_sql}
#         GROUP BY i.scryfall_id, i.finish
#         {having_sql}
#         ORDER BY {sort_sql}, CAST(cp.collector_number AS INTEGER) ASC, i.finish DESC
#         LIMIT ? OFFSET ?
#     '''    

#     cards = manager.cursor.execute(main_query, params + having_params + [per_page, offset]).fetchall()

#     query_locs = 'SELECT location_id as id, name FROM locations ORDER BY name'
#     locs = manager.cursor.execute(query_locs).fetchall()

#     manager.close()

#     card_list = [dict(row) for row in cards]
#     loc_list = [dict(row) for row in locs]

#     # Designed to be modular: Return just the partial if requested via fetch/AJAX
#     if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
#         view_mode = request.headers.get("X-View-Mode", request.args.get("view_mode", "grid"))

#         if view_mode == "table":
#             return render_template(
#                 "_table_rows.html",
#                 cards=card_list,
#                 view_mode="inventory",
#                 locations=loc_list
#             )

#         return render_template(
#             "_card_items.html",
#             cards=card_list,
#             view_mode="inventory",
#             locations=loc_list
#         )

#     # Fallback to full page render if accessed directly via URL
#     return render_template('_inventory_table.html', 
#                         cards=card_list, 
#                         locations=loc_list, 
#                         view_mode='inventory',
#                         page=page,
#                         total_pages=total_pages,
#                         search_query=search_query)

@inventory_bp.route("/api/tcgcsv/special-finish-candidates")
@login_required
def special_finish_candidates():
    scryfall_id = request.args.get("scryfall_id", "").strip()
    finish = request.args.get("finish", "").strip()

    if not scryfall_id or not finish:
        return jsonify({"success": False, "error": "Missing scryfall_id or finish."}), 400

    manager = CardDB()

    try:
        candidates = search_tcgcsv_products_for_finish(manager, scryfall_id, finish)
        return jsonify({"success": True, "candidates": candidates})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500
    finally:
        manager.close()
        
@inventory_bp.route("/api/tcgcsv/price-override", methods=["POST"])
@login_required
def save_price_override():
    data = request.get_json() or {}

    scryfall_id = (data.get("scryfall_id") or "").strip()
    finish = (data.get("finish") or "").strip()
    tcgplayer_id = data.get("tcgplayer_id")
    tcgcsv_group_id = data.get("tcgcsv_group_id")
    note = data.get("note") or "Manual special-finish price mapping"

    if not scryfall_id or not finish or not tcgplayer_id:
        return jsonify({"success": False, "error": "Missing required override data."}), 400

    manager = CardDB()

    try:
        manager.cursor.execute("""
            INSERT INTO tcgplayer_price_overrides (
                scryfall_id,
                finish,
                tcgplayer_id,
                tcgcsv_group_id,
                note
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scryfall_id, finish) DO UPDATE SET
                tcgplayer_id = excluded.tcgplayer_id,
                tcgcsv_group_id = excluded.tcgcsv_group_id,
                note = excluded.note
        """, (
            scryfall_id,
            normalize_finish(finish),
            int(tcgplayer_id),
            tcgcsv_group_id,
            note
        ))

        manager.commit()

        price_refreshed = False
        price_warning = None

        try:
            price_refreshed = update_single_card_price_from_tcgcsv(manager, scryfall_id)
            manager.commit()
        except Exception as error:
            price_warning = (
                "Override saved, but TCGCSV did not respond for immediate price refresh. "
                "Run the market sync later."
            )
            print(f"{price_warning} Details: {error}")

        return jsonify({
            "success": True,
            "price_refreshed": price_refreshed,
            "warning": price_warning
        })

    except Exception as error:
        manager.conn.rollback()
        return jsonify({"success": False, "error": str(error)}), 500

    finally:
        manager.close()