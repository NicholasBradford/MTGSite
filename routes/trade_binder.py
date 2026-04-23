from flask import Blueprint, request, redirect, url_for, render_template
from db.db_manager import CardDB
import sqlite3, ScryfallFetcher, db.db_manager

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