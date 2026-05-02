import sqlite3, db.db_manager, uuid
from ScryfallFetcher import ScryfallFetcher
from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import current_user, login_required
from search import search
from db.db_manager import CardDB
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

trade_bp = Blueprint('trade_binder', __name__)

@trade_bp.route('/trade_binder', methods=['GET', 'POST'])
def trade():
    search_query = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50 
    offset = (page - 1) * per_page
    
    manager = CardDB()
    # conditions = 
    params, filter_sql, having_sql, having_params, sort_sql = search(search_query,["i.is_tradeable = 1"])
    # params.append("i.is_tradeable = 1")
    # sort_by = request.args.get('sort', 'name')
    
    # if sort_by not in sort_options:
    #     sort_by = 'name'

# 3. Extract the SPECIFIC SQL clause to use
    # active_sort = sort_options[sort_by]

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
            cd.cmc, 
            cd.color_identity, 
            cp.image_url, 
            cp.set_code, 
            cp.collector_number,
            i.finish, 
            COUNT(*) as qty 
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        {filter_sql}
        GROUP BY i.scryfall_id, i.finish
        {having_sql}
        ORDER BY {sort_sql}
        LIMIT ? OFFSET ?
    '''    
    
    # Execute main query passing the search params PLUS the limit and offset params
    cards = manager.cursor.execute(main_query, params + [per_page, offset]).fetchall()
    
    # Close the connection after we have our data
    manager.close()
    
    # Safely convert tuples to dictionaries so Jinja doesn't crash
    card_list = [dict(row) for row in cards]
    
    # 5. AJAX CHECK FOR INFINITE SCROLL
    # If the request comes from our infinite scroll script, ONLY return the card snippets
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_card_items.html', cards=card_list, view_mode='trades')
    
    # Otherwise, return the full page layout on initial load
    return render_template('trade_binder.html', 
                           cards=card_list, 
                           view_mode="trades",
                           page=page,
                           total_pages=total_pages)
    
@trade_bp.route('/api/submit_trade', methods=['POST'])
def submit_trade():
    # Grab the JSON payload sent by the JavaScript cart
    data = request.get_json()
    items = data.get('items', [])
    
    if not items:
        return jsonify({'success': False, 'error': 'Cart is empty'}), 400

    # Generate a unique alphanumeric trade ID (e.g., "TRD-8A3B9C")
    trade_id = f"TRD-{uuid.uuid4().hex[:6].upper()}"
    
    # TODO: If you are requiring users to be logged in to trade, grab their ID here.
    # For example: user_id = current_user.id (Assuming Flask-Login is set up)
    # For now, we will hardcode user_id = 1 for testing.
    user_id = current_user.id

    manager = CardDB()
    
    try:
        # 1. Create the main trade record
        manager.cursor.execute('''
            INSERT INTO trades (trade_id, user_id, status)
            VALUES (?, ?, 'Pending')
        ''', (trade_id, user_id))
        
        # 2. Insert all the individual requested cards into the outbound table
        for item in items:
            manager.cursor.execute('''
                INSERT INTO trade_outbound_items (trade_id, scryfall_id, finish, quantity)
                VALUES (?, ?, ?, ?)
            ''', (
                trade_id, 
                item['scryfall_id'], 
                item['finish'], 
                item['qty']
            ))
            
        manager.commit()
        success = True
        
    except Exception as e:
        print(f"Error saving trade to database: {e}")
        success = False
        
    finally:
        manager.close()

    return jsonify({'success': success, 'trade_id': trade_id})



