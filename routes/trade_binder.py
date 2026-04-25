import sqlite3, db.db_manager, uuid
from ScryfallFetcher import ScryfallFetcher
from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import current_user, login_required
from db.db_manager import CardDB


def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

trade_bp = Blueprint('trade_binder', __name__)

@trade_bp.route('/trade_binder', methods=['GET', 'POST'])
def trade():
    # 1. Grab all the URL parameters
    page = request.args.get('page', 1, type=int)
    per_page = 50 
    offset = (page - 1) * per_page
    
    manager = CardDB()

    # 2. DYNAMIC SEARCH BUILDER
    # START the list with the mandatory tradeable requirement!
    conditions = ["i.is_tradeable = 1"]
    params = []

    # Grab all possible search fields from the URL
    s_name = request.args.get('name', '').strip()
    s_set = request.args.get('set', '').strip()
    s_type = request.args.get('type', '').strip()
    s_color = request.args.get('color', '').strip()
    s_text = request.args.get('text', '').strip()

    # Append conditions only if the user typed something in that box
    if s_name:
        for term in s_name.split():
            if term.startswith('-'):
                conditions.append("cd.name NOT LIKE ?")
                params.append(f'%{term[1:]}%') # The [1:] strips the '-' away
            else:
                conditions.append("cd.name LIKE ?")
                params.append(f'%{term}%')

    if s_type:
        for term in s_type.split():
            if term.startswith('-'):
                conditions.append("cd.type_line NOT LIKE ?")
                params.append(f'%{term[1:]}%')
            else:
                conditions.append("cd.type_line LIKE ?")
                params.append(f'%{term}%')

    if s_text:
        for term in s_text.split():
            if term.startswith('-'):
                conditions.append("cd.oracle_text NOT LIKE ?")
                params.append(f'%{term[1:]}%')
            else:
                conditions.append("cd.oracle_text LIKE ?")
                params.append(f'%{term}%')

    # --- SET ---
    if s_set:
        if s_set.startswith('-'):
            conditions.append("cp.set_code != ?")
            params.append(s_set[1:].lower())
        else:
            conditions.append("cp.set_code = ?")
            params.append(s_set.lower())

    # --- COLOR IDENTITY ---
    if s_color:
        for term in s_color.upper().split():
            
            if term in ['C', 'COLORLESS']:
                # Exact Colorless
                conditions.append("(cd.color_identity IS NULL OR cd.color_identity = '' OR cd.color_identity = '[]')")
                
            elif term.startswith('ID:'):
                # COMMANDER IDENTITY MODE (e.g., id:WUB)
                # This excludes any color NOT in your commander's identity
                allowed_colors = term[3:] # Grabs the 'WUB' part
                for c in 'WUBRG':
                    if c not in allowed_colors:
                        conditions.append("cd.color_identity NOT LIKE ?")
                        params.append(f'%{c}%')
                        
            elif term.startswith('-'):
                for char in term[1:]:
                    if char in 'WUBRG':
                        conditions.append("cd.color_identity NOT LIKE ?")
                        params.append(f'%{char}%')
                        
            else:
                for char in term:
                    if char in 'WUBRG':
                        conditions.append("cd.color_identity LIKE ?")
                        params.append(f'%{char}%')

    # Join them all together with " AND "
    # Since we started with "i.is_tradeable = 1", this list is NEVER empty.
    filter_sql = "WHERE " + " AND ".join(conditions)

    # 3. THE COUNT QUERY (For Pagination Math)
    count_query = f'''
        SELECT COUNT(*) FROM (
            SELECT i.scryfall_id 
            FROM inventory i 
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            {filter_sql}
            GROUP BY i.scryfall_id, i.finish
        )
    '''
    # Execute the count query to get total pages
    total_items = manager.cursor.execute(count_query, params).fetchone()[0]
    total_pages = (total_items + per_page - 1) // per_page

    # 4. THE MAIN QUERY (For Displaying Cards)
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
        ORDER BY cd.name, cp.set_code, cp.collector_number ASC
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


@trade_bp.route('/admin/dashboard', methods=['GET'])
@login_required
def admin_dashboard():
    manager = CardDB()
    
    # Grab all pending trades
    trades = manager.cursor.execute('''
        SELECT t.*, u.username AS submitter_name
        FROM trades t
        JOIN users u ON t.user_id = u.user_id
        WHERE t.status = 'Pending' 
        ORDER BY t.created_at ASC
    ''').fetchall()
    
    # Convert to a list of dicts so we can append items to them
    pending_trades = [dict(t) for t in trades]
    
    # Fetch the requested cards for each trade
    for trade in pending_trades:
        items = manager.cursor.execute('''
            SELECT ti.quantity, ti.finish, cd.name, cp.set_code, cp.collector_number
            FROM trade_outbound_items ti
            JOIN card_printings cp ON ti.scryfall_id = cp.scryfall_id
            JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
            WHERE ti.trade_id = ?
        ''', (trade['trade_id'],)).fetchall()
        
        trade['requested_cards'] = [dict(i) for i in items]
        
    manager.close()
    
    return render_template('admin_dashboard.html', pending_trades=pending_trades, )

def process_incoming_cards(incoming, manager):
    if not incoming:
        return
    
    # Initialize the fetcher with your current db manager
    fetcher = ScryfallFetcher(manager)
    
    incoming_items = [item.strip() for item in incoming.split(',')]
    
    for item in incoming_items:
        if not item: 
            continue
        
        parts = item.split('-')
        if len(parts) != 3:
            print(f"Skipping badly formatted item: {item}")
            continue
            
        set_code, cn, finish = parts
        
        # 1. Try to find the card in the local DB first
        card = manager.cursor.execute('''
            SELECT scryfall_id FROM card_printings 
            WHERE set_code = ? AND collector_number = ?
        ''', (set_code.lower(), cn.lower())).fetchone()
        
        scryfall_id = None
        
        # print(f"DEBUG: {dict(card)}")

        if card:
            scryfall_id = card['scryfall_id']
        else:
            # 2. If NOT found, use ScryfallFetcher to get it from the API
            print(f"Card {set_code}-{cn} not in DB. Fetching from Scryfall...")
            card = fetcher.fetch_and_add(set_code, cn)


        # 3. If we have a scryfall_id (either from DB or Fetcher), add to inventory
        if card:
            manager.cursor.execute('''
                INSERT INTO inventory (scryfall_id, finish, is_surplus, is_tradeable, location_id)
                VALUES (?, ?, 0, 0, 1)
            ''', (card['scryfall_id'], finish.lower()))
            print(f"Successfully added {set_code}-{cn} ({finish}) to inventory!")
            return True
        else:
            print(f"Error: Card {set_code}-{cn} could not be found or fetched.")
            return False

@trade_bp.route('/admin/process_trade', methods=['POST'])
@login_required 
def process_trade():
    # 1. Grab the data from the submitted form
    trade_id = request.form.get('trade_id')
    trade_notes = request.form.get('trade_notes')
    incoming_cards = request.form.get('incoming_cards')
    action = request.form.get('action') # Will be exactly 'accept' or 'deny'

    manager = CardDB()

    try:
        if action == 'accept':
            new_status = 'Accepted'
            
            check = process_incoming_cards(incoming_cards, manager)
            if not check:
                raise Exception("Invalid Trade: One or more incoming cards could not be resolved.")
            
            # --- INVENTORY REMOVAL LOGIC ---
            outbound_items = manager.cursor.execute('''
                SELECT scryfall_id, finish, quantity 
                FROM trade_outbound_items 
                WHERE trade_id = ?
            ''', (trade_id,)).fetchall()
            
            # For each group of cards they requested...
            for item in outbound_items:
                # Find exactly [quantity] instance_ids from your inventory that match
                instances = manager.cursor.execute('''
                    SELECT instance_id 
                    FROM inventory 
                    WHERE scryfall_id = ? AND finish = ? AND is_tradeable = 1
                    LIMIT ?
                ''', (item['scryfall_id'], item['finish'], item['quantity'])).fetchall()
                
            # Delete those specific physical copies from your database
                for instance in instances:
                        manager.cursor.execute('''
                            DELETE FROM inventory WHERE instance_id = ?
                        ''', (instance['instance_id'],))
                
                
                    
        elif action == 'deny':
            new_status = 'Rejected'
        else:
            return "Invalid action", 400 # Just in case something weird happens

        # 3. Update the existing trade record (DO NOT DELETE IT)
        manager.cursor.execute('''
            UPDATE trades 
            SET status = ?, notes = ?, incoming = ?
            WHERE trade_id = ?
        ''', (new_status, trade_notes, incoming_cards, trade_id))
        
        manager.commit()
        
    except Exception as e:
        print(f"Error processing trade {trade_id}: {e}")
        
    finally:
        manager.close()

    # Send the user right back to the dashboard to process the next one
    return redirect(url_for('trade_binder.admin_dashboard'))