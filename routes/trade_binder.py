import sqlite3, db.db_manager, uuid, requests,datetime, ScryfallFetcher
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

@trade_bp.route('/binder/trades', methods=['GET', 'POST'])
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
                WHEN i.finish = 'foil' THEN cp.current_price_foil
                WHEN i.finish = 'etched' THEN cp.current_price_foil 
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
    
    # Execute main query passing the search params PLUS the limit and offset params
    cards = manager.cursor.execute(main_query, params + [per_page, offset]).fetchall()
    
    query_locs = 'SELECT location_id as id, name FROM locations ORDER BY name'
    locs = manager.cursor.execute(query_locs).fetchall()

    manager.close()

    card_list = [dict(row) for row in cards]
    loc_list = [dict(row) for row in locs]

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_card_items.html', cards=card_list, view_mode='trades', locations=loc_list)
    
    # Otherwise, return the full page layout on initial load
    return render_template('trade_binder.html', 
                           cards=card_list, 
                           locations=loc_list,
                           view_mode="trades",
                           page=page,
                           total_pages=total_pages,
                           search_query=search_query)
    
@trade_bp.route('/api/submit_trade', methods=['POST'])
@login_required
def submit_trade():
    # Grab the JSON payload sent by the JavaScript cart
    data = request.get_json()
    outbound_items = data.get('outbound', []) 
    inbound_items = data.get('inbound', [])
    
    if not outbound_items and not inbound_items:
        return jsonify({'success': False, 'error': 'No cards selected for trade.'}), 400
    
    # Generate a unique alphanumeric trade ID (e.g., "TRD-8A3B9C")
    trade_id = f"TRD-{uuid.uuid4().hex[:6].upper()}"
    
    user_id = current_user.id

    manager = CardDB()
    
    fetcher = ScryfallFetcher.ScryfallFetcher(manager)
    
    try:
        # 1. Create the main trade record
        manager.cursor.execute('''
            INSERT INTO trades (trade_id, user_id, status)
            VALUES (?, ?, 'Pending')
        ''', (trade_id, user_id))
        
        # 2. Insert all the individual requested cards into the outbound table
        for item in outbound_items:
            manager.cursor.execute('''
                INSERT INTO trade_outbound_items (trade_id, scryfall_id, finish, quantity)
                VALUES (?, ?, ?, ?)
            ''', (
                trade_id, 
                item['scryfall_id'], 
                item['finish'], 
                item['qty']
            ))
        for item in inbound_items:
            fetcher.fetch_and_add(item['set_code'], item['cn'])
            manager.cursor.execute('''
                INSERT INTO trade_inbound_items (trade_id, scryfall_id, finish, quantity)
                VALUES (?, ?, ?, ?)
            ''', (trade_id, item['scryfall_id'], item['finish'], item['qty']))
            
        manager.conn.commit()
        return jsonify({'success': True, 'trade_id': trade_id})
        
    except Exception as e:
        manager.conn.rollback()
        print(f"Database Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        manager.close()

@trade_bp.route('/wishlist', methods=['GET'])
def wishlist():
    search_query = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50 
    offset = (page - 1) * per_page
    
    manager = CardDB()

    params, filter_sql, having_sql, having_params, sort_sql = search(search_query)

    count_query = f'''
        SELECT COUNT(*) FROM (
            SELECT w.scryfall_id 
            FROM wishlist w 
            JOIN card_printings cp ON w.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            {filter_sql}
            GROUP BY w.scryfall_id, w.finish
            {having_sql}
        )
    '''
    total_items = manager.cursor.execute(count_query, params + having_params).fetchone()[0]
    total_pages = (total_items + per_page - 1) // per_page  

    main_query = f'''
        SELECT 
            w.scryfall_id, 
            w.wish_id, 
            w.finish, 
            cd.name, 
            cd.cmc, 
            cd.color_identity, 
            cp.image_url, 
            cp.set_code, 
            cp.collector_number,
            COUNT(*) as qty 
        FROM wishlist w 
        JOIN card_printings cp ON w.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        {filter_sql}
        GROUP BY w.scryfall_id, w.finish
        {having_sql}
        ORDER BY {sort_sql}, w.finish DESC
        LIMIT ? OFFSET ?
    '''    
   
    cards = manager.cursor.execute(main_query, params + [per_page, offset]).fetchall()

    manager.close()

    card_list = [dict(row) for row in cards]
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_card_items.html', cards=card_list, view_mode='trades')
    
    return render_template('wishlist.html',cards=card_list, 
                           view_mode="wishlist",
                           page=page,
                           total_pages=total_pages)

@trade_bp.route('/trade', methods=['GET'])
def trade_page():
    return render_template('trade_page.html')

@trade_bp.route('/api/trade_search', methods=['GET'])
def search_tradeable_cards():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    manager = CardDB()
    # Search for cards in inventory where is_tradeable is true (1)
    # Note: If your column is still technically 'is_surplus' in the DB schema, change it here.
    sql = """
        SELECT 
            i.instance_id, 
            i.scryfall_id,
            cd.name, 
            cp.set_code, 
            cp.collector_number,
            i.finish, 
            cp.image_url,
            COUNT(*) as qty
        FROM inventory i
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE i.is_tradeable = 1 AND cd.name LIKE ?
        GROUP BY i.scryfall_id, i.finish
        ORDER BY cd.name ASC
        LIMIT 20
    """
    results = manager.cursor.execute(sql, (f'%{query}%',)).fetchall()
    manager.close()
    
    cards = [dict(row) for row in results]
    return jsonify(cards)

@trade_bp.route('/api/fetch_incoming', methods=['POST'])
def fetch_incoming_card():
    data = request.json
    set_code = data.get('set_code', '').lower()
    cn = data.get('cn', '').lower()
    finish = data.get('finish', 'nonfoil')

    # Fetch directly from Scryfall to just get the image/data without adding to DB yet
    url = f"https://api.scryfall.com/cards/{set_code}/{cn}"
    response = requests.get(url)
    
    if response.status_code == 200:
        card_data = response.json()
        
        # Handle double-faced cards for images
        if 'image_uris' in card_data:
            image_url = card_data['image_uris'].get('normal', '')
        elif 'card_faces' in card_data and 'image_uris' in card_data['card_faces'][0]:
            image_url = card_data['card_faces'][0]['image_uris'].get('normal', '')
        else:
            image_url = ""

        return jsonify({
            'success': True,
            'name': card_data['name'],
            'scryfall_id': card_data['id'],
            'set_code': set_code,
            'cn': cn,
            'finish': finish,
            'image_url': image_url
        })
    else:
        return jsonify({'success': False, 'error': 'Card not found on Scryfall.'}), 404