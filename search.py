from flask import request
import re

def search(search_query, conditions=None):
    if conditions is None:
        conditions = []
    
    s_sort = 'name' # Default sort key
    search_params = {
        'names': [], 'sets': [], 'types': [], 'colors': [], 
        'identities': [], 'text': [], 'locs': [], 'cns': [], 'oracle':[],
        'usd': [], 'qty': [], 'sort': 'name'
    }
    
    sort_options = {
        'name': """LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
        'rarity': """CASE cp.rarity WHEN 'mythic' THEN 1 WHEN 'rare' THEN 2 WHEN 'uncommon' THEN 3 WHEN 'common' THEN 4 ELSE 5 END ASC, cd.name ASC""",
        'usd': """(CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0) ELSE COALESCE(cp.current_price, 0) END) DESC""",
        'set': "cp.set_code ASC, CAST(cp.collector_number AS INTEGER) ASC",
        'location': "l.name ASC, cd.name ASC",
        'added': "i.added DESC",
    }
            
    if search_query:
        tokens = re.findall(r'(?:-?\w+:(?:[^\s"]+|"[^"]*")|-?[^\s"]+|"[^"]*")', search_query)
        
        for token in tokens:
            is_negated = token.startswith('-')
            clean_token = token[1:] if is_negated else token
            
            match = re.match(r'^([a-zA-Z_]+)([:<>=!]+)(.*)$', clean_token)
            
            if match:
                key = match.group(1).lower()
                operator = match.group(2)
                val = match.group(3).strip('"') 

                if operator != ':':
                    val = operator + val
                
                prefix_val = f"-{val}" if is_negated else val
                
                if key in ['set', 's']:
                    search_params['sets'].append(prefix_val)
                elif key in ['id', 'identity']:
                    search_params['identities'].append(prefix_val)
                elif key in ['loc', 'location', 'l']:
                    search_params['locs'].append(prefix_val)
                elif key in ['type', 't']:
                    search_params['types'].append(prefix_val)
                elif key in ["color", "c"]:
                    search_params['colors'].append(prefix_val)
                elif key in ["text","oracle","o"]:
                    search_params['text'].append(prefix_val)
                elif key in ["qty", "q", "quantity"]:
                    search_params['qty'].append(prefix_val)
                elif key in ["usd"]:
                    search_params['usd'].append(prefix_val)
                elif key == "sort":
                    s_sort = val.lower()
            else:
                search_params['names'].append(token)

    params = []

    # Process Lists into SQL
    for term in search_params['names']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        conditions.append(f"cd.name {'NOT ' if is_negated else ''}LIKE ?")
        params.append(f'%{val}%')

    for term in search_params['sets']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        conditions.append(f"cp.set_code {'!=' if is_negated else '='} ?")
        params.append(val.lower())

    for term in search_params['types']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        conditions.append(f"cd.type_line {'NOT ' if is_negated else ''}LIKE ?")
        params.append(f'%{val}%')

    for term in search_params['text']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        conditions.append(f"cd.oracle_text {'NOT ' if is_negated else ''}LIKE ?")
        params.append(f'%{val}%')

    # Color Identity (Commander Logic)
    for term in search_params['identities']:
        is_negated = term.startswith('-')
        val = term[1:].replace('id:', '').upper() if is_negated else term.replace('id:', '').upper()
        
        if val in ['C', 'COLORLESS']:
            operator = "!=" if is_negated else "="
            conditions.append(f"cd.color_identity {operator} ''")
        else:
            for c in 'WUBRG':
                if c not in val:
                    if not is_negated:
                        conditions.append("cd.color_identity NOT LIKE ?")
                        params.append(f'%{c}%')
            
            if is_negated:
                for c in val:
                    conditions.append("cd.color_identity NOT LIKE ?")
                    params.append(f'%{c}%')

    # Location
    for term in search_params['locs']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        # print(f"{val} : {term}")
        if val.isdigit():
            conditions.append(f"i.location_id {'!=' if is_negated else '='} ?")
            # print(f"DEBUG: {conditions}")
            params.append(int(val))
        else:
            conditions.append(f"i.location_id {'NOT IN' if is_negated else 'IN'} (SELECT location_id FROM locations WHERE name LIKE ?)")
            params.append(f'%{val}%')

    # USD Logic
    for usd_term in search_params['usd']:
        match = re.match(r'([<>=!]+)?([\d\.]+)', usd_term)
        if match:
            op, val = match.group(1) or '=', float(match.group(2))
            if op in ['>', '<', '>=', '<=', '=', '!=']:
                conditions.append(f"""
                    (CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0)
                     ELSE COALESCE(cp.current_price, 0) END) {op} ?""")
                params.append(val)

    # Final SQL construction
    filter_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    # Quantity (HAVING)
    having_sql, having_params = "", []
    if search_params['qty']:
        match = re.match(r'([<>=]*)\s*(\d+)', search_params['qty'][0])
        if match:
            op, val = match.groups()
            having_sql = f"HAVING COUNT(*) {op if op else '='} ?"
            having_params.append(int(val))

    sort_sql = sort_options.get(s_sort, sort_options['name'])
            
    return params, filter_sql, having_sql, having_params, sort_sql
