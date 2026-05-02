from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import login_required
from db.db_manager import CardDB
from search import search
import sqlite3, ScryfallFetcher, db.db_manager, re

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
        'price': 'cp.current_price DESC',
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
            i.scryfall_id, i.instance_id, i.location_id, i.is_tradeable,
            cd.name, cp.image_url, cp.set_code, cp.collector_number,
            i.finish, COUNT(*) as qty 
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        {filter_sql}
        GROUP BY i.scryfall_id, i.finish
        {having_sql}
        ORDER BY {sort_sql}
        LIMIT ? OFFSET ?
    '''    

    cards = manager.cursor.execute(main_query, params + having_params + [per_page, offset]).fetchall()

    query_locs = 'SELECT location_id as id, name FROM locations ORDER BY name'
    locs = manager.cursor.execute(query_locs).fetchall()

    manager.close()

    card_list = [dict(row) for row in cards]
    loc_list = [dict(row) for row in locs]

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_card_items.html', cards=card_list, view_mode='inventory', locations=loc_list)

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
    # Get values from the fetch request
    new_loc = request.form.get('location_id')
    new_trade = request.form.get('is_tradeable')

    # Update Location if provided
    if new_loc is not None:
        manager.cursor.execute(
            "UPDATE inventory SET location_id = ? WHERE instance_id = ?",
            (new_loc, instance_id)
        )

    # Update Trade Status if provided
    if new_trade is not None:
        # Convert JS 'true'/'false' or '1'/'0' to integer 1 or 0
        trade_val = 1 if new_trade in ['1', 'true'] else 0
        manager.cursor.execute(
            "UPDATE inventory SET is_tradeable = ? WHERE instance_id = ?",
            (trade_val, instance_id)
        )

    manager.commit()
    manager.close()
    return {"status": "success"}, 200

@inventory_bp.route('/get_instances/<scryfall_id>/<finish>')
@login_required
def get_instances(scryfall_id, finish):
    manager = CardDB()
    
    query = '''
        SELECT i.scryfall_id, i.instance_id, i.location_id, i.is_tradeable, l.name as location_name
        FROM inventory i
        JOIN locations l ON i.location_id = l.location_id
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