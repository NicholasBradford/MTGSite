from flask import request
import re
 
def search(search_query,conditions = None):
    
    if conditions is None:
        conditions = []
        
    # Grab all possible search fields from the URL
    s_name = request.args.get('name', '').strip()
    s_set = request.args.get('set', '').strip()
    s_type = request.args.get('type', '').strip()
    s_color = request.args.get('color', '').strip()
    s_text = request.args.get('text', '').strip()
    s_loc = request.args.get('location', '').strip()
    s_qty = request.args.get('qty', '').strip()
    s_usd = request.args.get('usd', '').strip()
    s_sort = request.args.get('sort', 'name').lower()

    if search_query:
        tokens = search_query.split() # Splits by spaces
        for token in tokens:
            if token.startswith('set:'):
                s_set = token.split(':', 1)[1]
            elif token.startswith('loc:'):
                s_loc = token.split(':', 1)[1]
            elif token.startswith('type:'):
                s_type = token.split(':', 1)[1]
            elif token.startswith('color:'):
                s_color = token.split(':', 1)[1]
            elif token.startswith('text:'):
                s_text = token.split(':', 1)[1]
            elif token.startswith('qty'):
                s_qty = token.replace('qty', '') 
            elif token.startswith('usd'):
                s_usd = token.replace('usd', '') 
            elif token.startswith('sort:'):
                s_sort = token.split(':', 1)[1].lower()
            else:
                # If there is no prefix, treat it as a name search.
                # Combine it with s_name so it doesn't overwrite an advanced drawer name input.
                s_name = (s_name + ' ' + token).strip()
                
    sort_options = {
        'name': """LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
        'rarity': """CASE cd.rarity WHEN 'mythic' THEN 1 WHEN 'rare' THEN 2 WHEN 'uncommon' THEN 3 WHEN 'common' THEN 4 ELSE 5 END ASC, cd.name ASC""",
        'usd': """(CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0) ELSE COALESCE(cp.current_price, 0) END) DESC""",
        'set': "cp.set_code ASC, CAST(cp.collector_number AS INTEGER) ASC",
        'location': "l.name ASC, cd.name ASC",
        'added': "i.added DESC"
    }
    
    sort_sql = sort_options.get(s_sort, sort_options['name'])
    
    # 1. DYNAMIC SEARCH BUILDER
    params = []
    
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
                        
        # --- LOCATION ---
    
    if s_loc:
        # If using ID, check for digits; if name, join on locations table
        if s_loc.isdigit():
            conditions.append("i.location_id = ?")
            params.append(int(s_loc))
        else:
            conditions.append("i.location_id IN (SELECT location_id FROM locations WHERE name LIKE ?)")
            params.append(f'%{s_loc}%')
            
    if s_usd:
        # Regex to split the operator from the number
        # Matches optional operators [<>=!] followed by numbers/decimals
        match = re.match(r'([<>=!]+)?([\d\.]+)', s_usd)
        
        if match:
            op = match.group(1) or '=' # Defaults to '=' if they just type usd5
            val = float(match.group(2))
            
            # Security check to prevent SQL injection on the operator
            if op in ['>', '<', '>=', '<=', '=', '!=']:
                
                # The Magic: Check the finish, then evaluate the correct price
                price_condition = f"""
                (CASE 
                    WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0)
                    ELSE COALESCE(cp.current_price, 0)
                END) {op} ?
                """
                conditions.append(price_condition)
                params.append(val)
                
            
    filter_sql = ""
    if conditions:
        filter_sql = "WHERE " + " AND ".join(conditions)

    # --- QUANTITY (Applied to the Group) ---
    # Note: Since qty is an aggregate, it needs to go into a HAVING clause
    
    having_sql = ""
    having_params = []
    if s_qty:
        
        match = re.match(r'([<>=]*)\s*(\d+)', s_qty)
        if match:
            operator, value = match.groups()
            operator = operator if operator else '='
            having_sql = f"HAVING COUNT(*) {operator} ?"
            having_params.append(int(value))
            
    return params, filter_sql, having_sql, having_params, sort_sql