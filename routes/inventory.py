from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import login_required
from db.db_manager import CardDB
import sqlite3, ScryfallFetcher, db.db_manager

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

    # 1. DYNAMIC SEARCH BUILDER
    conditions = []
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
    filter_sql = ""
    if conditions:
        filter_sql = "WHERE " + " AND ".join(conditions)

    # 2. THE QUERIES (These remain mostly the same, just using the new filter_sql)
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
    # Notice we pass our dynamically built `params` list here!
    total_items = manager.cursor.execute(count_query, params).fetchone()[0]
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
        ORDER BY cp.set_code, cd.name, cp.collector_number
        LIMIT ? OFFSET ?
    '''    
    # Add limit and offset to params list
    cards = manager.cursor.execute(main_query, params + [per_page, offset]).fetchall()
    
    query_locs = 'SELECT location_id as id, name FROM locations ORDER BY name'
    locs = manager.cursor.execute(query_locs).fetchall()
    
    manager.close()
    
    # Safely convert tuples to dictionaries so Jinja doesn't crash
    card_list = [dict(row) for row in cards]
    loc_list = [dict(row) for row in locs]
    
    # THE MISSING AJAX CHECK: 
    # If the request comes from our infinite scroll script, ONLY return the card snippets
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_card_items.html', cards=card_list, view_mode='inventory', locations = loc_list)
    
    # Otherwise, return the full page layout
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