from flask import request
import re

def search(search_query, conditions=None, source='inventory'):
    if conditions is None:
        conditions = []
    
    if source not in ('inventory', 'wishlist'):
        raise ValueError('Unsupported search source')
    s_sort = request.args.get('sort', 'name') if request else 'name'
    if s_sort == 'price':
        s_sort = 'usd'
    search_params = {
        'names': [], 'sets': [], 'types': [], 'colors': [], 
        'identities': [], 'text': [], 'locs': [], 'cns': [], 'oracle':[],
        'usd': [], 'qty': [], 'sort': 'name'
    }
    
    COLOR_SORT_SQL = """CASE 
                        -- 1. MONO-COLOR (Standard WUBRG)
                        WHEN color = 'W' THEN 10
                        WHEN color = 'U' THEN 11
                        WHEN color = 'B' THEN 12
                        WHEN color = 'R' THEN 13
                        WHEN color = 'G' THEN 14

                        -- 2. TWO-COLOR PAIRS: ALLIES (Grouped by Primary Color)
                        WHEN color IN ('W,U', 'U,W') THEN 20  -- WU
                        WHEN color IN ('U,B', 'B,U') THEN 21  -- UB
                        WHEN color IN ('B,R', 'R,B') THEN 22  -- BR
                        WHEN color IN ('G,R', 'R,G') THEN 23  -- RG
                        WHEN color IN ('G,W', 'W,G') THEN 24  -- GW

                        -- 2. TWO-COLOR PAIRS: ENEMIES (Grouped by Primary Color)
                        WHEN color IN ('B,W', 'W,B') THEN 25  -- WB
                        WHEN color IN ('R,U', 'U,R') THEN 26  -- UR
                        WHEN color IN ('B,G', 'G,B') THEN 27  -- BG
                        WHEN color IN ('R,W', 'W,R') THEN 28  -- RW
                        WHEN color IN ('G,U', 'U,G') THEN 29  -- GU

                        -- 3. THREE-COLOR COMBINATIONS: SHARDS (Clockwise)
                        WHEN color IN ('B,U,W', 'B,W,U', 'U,B,W', 'U,W,B', 'W,B,U', 'W,U,B') THEN 30 -- WUB (Esper)
                        WHEN color IN ('B,R,U', 'B,U,R', 'R,B,U', 'R,U,B', 'U,B,R', 'U,R,B') THEN 31 -- UBR (Grixis)
                        WHEN color IN ('B,G,R', 'B,R,G', 'G,B,R', 'G,R,B', 'R,B,G', 'R,G,B') THEN 32 -- BRG (Jund)
                        WHEN color IN ('G,R,W', 'G,W,R', 'R,G,W', 'R,W,G', 'W,G,R', 'W,R,G') THEN 33 -- RGW (Naya)
                        WHEN color IN ('G,U,W', 'G,W,U', 'U,G,W', 'U,W,G', 'W,G,U', 'W,U,G') THEN 34 -- GWU (Bant)

                        -- 3. THREE-COLOR COMBINATIONS: WEDGES (Counter-Clockwise)
                        WHEN color IN ('B,G,W', 'B,W,G', 'G,B,W', 'G,W,B', 'W,B,G', 'W,G,B') THEN 35 -- WBG (Abzan)
                        WHEN color IN ('R,U,W', 'R,W,U', 'U,R,W', 'U,W,R', 'W,R,U', 'W,U,R') THEN 36 -- URW (Jeskai)
                        WHEN color IN ('B,G,U', 'B,U,G', 'G,B,U', 'G,U,B', 'U,B,G', 'U,G,B') THEN 37 -- BGU (Sultai)
                        WHEN color IN ('B,R,W', 'B,W,R', 'R,B,W', 'R,W,B', 'W,B,R', 'W,R,B') THEN 38 -- RWB (Mardu)
                        WHEN color IN ('G,R,U', 'G,U,R', 'R,G,U', 'R,U,G', 'U,G,R', 'U,R,G') THEN 39 -- GUR (Temur)

                        -- 4. FOUR-COLOR COMBINATIONS (Clockwise, beginning after missing color)
                        WHEN LENGTH(color) - LENGTH(REPLACE(color, ',', '')) = 3 THEN
                            CASE 
                                WHEN color NOT LIKE '%W%' THEN 40 -- Missing W: UBRG
                                WHEN color NOT LIKE '%U%' THEN 41 -- Missing U: BRGW
                                WHEN color NOT LIKE '%B%' THEN 42 -- Missing B: RGWU
                                WHEN color NOT LIKE '%R%' THEN 43 -- Missing R: GWUB
                                WHEN color NOT LIKE '%G%' THEN 44 -- Missing G: WUBR
                            END

                        -- 5. FIVE-COLOR 
                        WHEN LENGTH(color) - LENGTH(REPLACE(color, ',', '')) = 4 THEN 50

                        -- Colorless fallback (Eldrazi, Artifacts, etc.)
                        ELSE 51 
                    END ASC"""
    
    
    sort_options = {
        'name': """LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
        'rarity': """CASE cp.rarity WHEN 'mythic' THEN 1 WHEN 'rare' THEN 2 WHEN 'uncommon' THEN 3 WHEN 'common' THEN 4 ELSE 5 END ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC """,
        'color': COLOR_SORT_SQL,
        'identity': re.sub(r'\bcolor\b', 'color_identity', COLOR_SORT_SQL),
        'usd': """(CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0) ELSE COALESCE(cp.current_price, 0) END) DESC""",
        'set': """cp.set_code ASC""",
        'location': """(SELECT name FROM locations WHERE location_id = i.location_id) ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
        'added': """i.added DESC"""
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
                elif key in ['cn', 'collector', 'collector_number', 'number']:
                    search_params['cns'].append(prefix_val)
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
                search_params['names'].append(('-' if is_negated else '') + clean_token.strip(chr(34)))

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
        
    for term in search_params['cns']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term

        conditions.append(f"cp.collector_number {'!=' if is_negated else '='} ?")
        params.append(val)

    for term in search_params['text']:
        is_negated = term.startswith('-')
        val = term[1:] if is_negated else term
        conditions.append(f"cd.oracle_text {'NOT ' if is_negated else ''}LIKE ?")
        params.append(f'%{val}%')

    # Color Identity (Commander Logic)
    # --- COLOR IDENTITY LOGIC (Inclusive contains semantics) ---
    for term in search_params['identities']:
        is_negated = term.startswith('-')
        val = term[1:].upper() if is_negated else term.upper()
        
        if val in ['C', 'COLORLESS']:
            operator = "!=" if is_negated else "="
            conditions.append(f"cd.color_identity {operator} ''")
        elif val in ['MULTICOLOR','MC']:
            operator = "=" if is_negated else ">"
            conditions.append(f"LENGTH(cd.color_identity) {operator} 1")
        elif val in ['MONOCOLOR', 'MONO']:
            operator = ">" if is_negated else "="
            conditions.append(f"LENGTH(cd.color_identity) {operator} 1")
        else:
            if not is_negated:
                # Match identities that contain any selected color.
                # Example: identity:U should match U and U,B identities.
                identity_terms = [c for c in 'WUBRG' if c in val]
                if identity_terms:
                    or_clauses = []
                    for c in identity_terms:
                        or_clauses.append("cd.color_identity LIKE ?")
                        params.append(f'%{c}%')
                    conditions.append(f"({' OR '.join(or_clauses)})")
            else:
                for c in val:
                    conditions.append("cd.color_identity NOT LIKE ?")
                    params.append(f'%{c}%')
                    
    # --- CARD COLOR LOGIC (Exact Match Requirements) ---
    for term in search_params['colors']:
        is_negated = term.startswith('-')
        val = term[1:].upper() if is_negated else term.upper()
        
        if val in ['C', 'COLORLESS']:
            operator = "!=" if is_negated else "="
            conditions.append(f"cd.color {operator} ''")
        elif val in ['MULTICOLOR','MC']:
            operator = "=" if is_negated else ">"
            conditions.append(f"LENGTH(cd.color) {operator} 1")
        elif val in ['MONOCOLOR', 'MONO']:
            operator = ">" if is_negated else "="
            conditions.append(f"LENGTH(cd.color) {operator} 1")
        else:
            if not is_negated:
                # 1. Must explicitly contain all requested characters
                for c in val:
                    conditions.append("cd.color LIKE ?")
                    params.append(f'%{c}%')
                # 2. Must NOT contain any characters that weren't requested
                for c in 'WUBRG':
                    if c not in val:
                        conditions.append("cd.color NOT LIKE ?")
                        params.append(f'%{c}%')
            else:
                # Negated exact search: remove instances featuring these characters completely
                for c in val:
                    conditions.append("cd.color NOT LIKE ?")
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
        elif val == "NULL" or val == "unassigned":
            conditions.append(f"i.location_id {'IS NOT' if is_negated else 'IS'} NULL")
        else:
            conditions.append(f"i.location_id {'NOT IN' if is_negated else 'IN'} (SELECT location_id FROM locations WHERE name LIKE ?)")
            params.append(f'%{val}%')

    # Only accepted numeric operators become SQL; malformed filters are ignored.
    inverse = {'>': '<=', '<': '>=', '>=': '<', '<=': '>', '=': '!=', '!=': '='}
    for term in search_params['usd']:
        negated = term.startswith('-')
        value = term[1:] if negated else term
        if value.lower() in ('null', 'unassigned'):
            conditions.append(f"(CASE WHEN i.finish = 'foil' THEN cp.current_price_foil ELSE cp.current_price END) {'IS NOT' if negated else 'IS'} NULL")
            continue
        match = re.fullmatch(r'(>=|<=|!=|>|<|=)?(\d+(?:\.\d*)?|\.\d+)', value)
        if match:
            op = match.group(1) or '='
            if negated:
                op = inverse[op]
            conditions.append(f"(CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0) ELSE COALESCE(cp.current_price, 0) END) {op} ?")
            params.append(float(match.group(2)))

    filter_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    having_parts, having_params = [], []
    for term in search_params['qty']:
        negated = term.startswith('-')
        match = re.fullmatch(r'(>=|<=|!=|>|<|=)?(\d+)', term[1:] if negated else term)
        if match:
            op = match.group(1) or '='
            if negated:
                op = inverse[op]
            having_parts.append(f"COUNT(*) {op} ?")
            having_params.append(int(match.group(2)))
    having_sql = 'HAVING ' + ' AND '.join(having_parts) if having_parts else ''

    sort_sql = sort_options.get(s_sort, sort_options['name'])
            
    foil_condition = "LOWER(REPLACE(COALESCE(i.finish, ''), '_', ' ')) IN ('foil', 'etched', 'rainbow foil')"
    filter_sql = filter_sql.replace("i.finish = 'foil'", foil_condition)
    sort_sql = sort_sql.replace("i.finish = 'foil'", foil_condition)
    if source == 'wishlist':
        # Wishlist rows have their own finish/added fields. Storage filters refer
        # to an owned copy of that printing and finish, if one exists.
        location = "(SELECT MIN(location_id) FROM inventory WHERE scryfall_id = w.scryfall_id AND finish = w.finish)"
        filter_sql = filter_sql.replace('i.location_id', location).replace('i.finish', 'w.finish').replace('i.added', 'w.added')
        sort_sql = sort_sql.replace('i.location_id', location).replace('i.finish', 'w.finish').replace('i.added', 'w.added')
    return params, filter_sql, having_sql, having_params, sort_sql
