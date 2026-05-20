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
        'name': """LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
        'rarity': """CASE cp.rarity WHEN 'mythic' THEN 1 WHEN 'rare' THEN 2 WHEN 'uncommon' THEN 3 WHEN 'common' THEN 4 ELSE 5 END ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC """,
        'color': """CASE 
                        -- 1. MONO-COLOR (Standard WUBRG)
                        WHEN color_identity = 'W' THEN 10
                        WHEN color_identity = 'U' THEN 11
                        WHEN color_identity = 'B' THEN 12
                        WHEN color_identity = 'R' THEN 13
                        WHEN color_identity = 'G' THEN 14

                        -- 2. TWO-COLOR PAIRS: ALLIES (Grouped by Primary Color)
                        WHEN color_identity IN ('W,U', 'U,W') THEN 20  -- WU
                        WHEN color_identity IN ('U,B', 'B,U') THEN 21  -- UB
                        WHEN color_identity IN ('B,R', 'R,B') THEN 22  -- BR
                        WHEN color_identity IN ('G,R', 'R,G') THEN 23  -- RG
                        WHEN color_identity IN ('G,W', 'W,G') THEN 24  -- GW

                        -- 2. TWO-COLOR PAIRS: ENEMIES (Grouped by Primary Color)
                        WHEN color_identity IN ('B,W', 'W,B') THEN 25  -- WB
                        WHEN color_identity IN ('R,U', 'U,R') THEN 26  -- UR
                        WHEN color_identity IN ('B,G', 'G,B') THEN 27  -- BG
                        WHEN color_identity IN ('R,W', 'W,R') THEN 28  -- RW
                        WHEN color_identity IN ('G,U', 'U,G') THEN 29  -- GU

                        -- 3. THREE-COLOR COMBINATIONS: SHARDS (Clockwise)
                        WHEN color_identity IN ('B,U,W', 'B,W,U', 'U,B,W', 'U,W,B', 'W,B,U', 'W,U,B') THEN 30 -- WUB (Esper)
                        WHEN color_identity IN ('B,R,U', 'B,U,R', 'R,B,U', 'R,U,B', 'U,B,R', 'U,R,B') THEN 31 -- UBR (Grixis)
                        WHEN color_identity IN ('B,G,R', 'B,R,G', 'G,B,R', 'G,R,B', 'R,B,G', 'R,G,B') THEN 32 -- BRG (Jund)
                        WHEN color_identity IN ('G,R,W', 'G,W,R', 'R,G,W', 'R,W,G', 'W,G,R', 'W,R,G') THEN 33 -- RGW (Naya)
                        WHEN color_identity IN ('G,U,W', 'G,W,U', 'U,G,W', 'U,W,G', 'W,G,U', 'W,U,G') THEN 34 -- GWU (Bant)

                        -- 3. THREE-COLOR COMBINATIONS: WEDGES (Counter-Clockwise)
                        WHEN color_identity IN ('B,G,W', 'B,W,G', 'G,B,W', 'G,W,B', 'W,B,G', 'W,G,B') THEN 35 -- WBG (Abzan)
                        WHEN color_identity IN ('R,U,W', 'R,W,U', 'U,R,W', 'U,W,R', 'W,R,U', 'W,U,R') THEN 36 -- URW (Jeskai)
                        WHEN color_identity IN ('B,G,U', 'B,U,G', 'G,B,U', 'G,U,B', 'U,B,G', 'U,G,B') THEN 37 -- BGU (Sultai)
                        WHEN color_identity IN ('B,R,W', 'B,W,R', 'R,B,W', 'R,W,B', 'W,B,R', 'W,R,B') THEN 38 -- RWB (Mardu)
                        WHEN color_identity IN ('G,R,U', 'G,U,R', 'R,G,U', 'R,U,G', 'U,G,R', 'U,R,G') THEN 39 -- GUR (Temur)

                        -- 4. FOUR-COLOR COMBINATIONS (Clockwise, beginning after missing color)
                        WHEN LENGTH(color_identity) - LENGTH(REPLACE(color_identity, ',', '')) = 3 THEN
                            CASE 
                                WHEN color_identity NOT LIKE '%W%' THEN 40 -- Missing W: UBRG
                                WHEN color_identity NOT LIKE '%U%' THEN 41 -- Missing U: BRGW
                                WHEN color_identity NOT LIKE '%B%' THEN 42 -- Missing B: RGWU
                                WHEN color_identity NOT LIKE '%R%' THEN 43 -- Missing R: GWUB
                                WHEN color_identity NOT LIKE '%G%' THEN 44 -- Missing G: WUBR
                            END

                        -- 5. FIVE-COLOR 
                        WHEN LENGTH(color_identity) - LENGTH(REPLACE(color_identity, ',', '')) = 4 THEN 50

                        -- Colorless fallback (Eldrazi, Artifacts, etc.)
                        ELSE 51 
                    END ASC"""
                    ,
        'usd': """(CASE WHEN i.finish = 'foil' THEN COALESCE(cp.current_price_foil, 0) ELSE COALESCE(cp.current_price, 0) END) DESC""",
        'set': """cp.set_code ASC""",
        'location': """l.name ASC, LOWER(REPLACE(REPLACE(REPLACE(REPLACE(cd.name, 'The ', ''), 'An ', ''), 'A ', ''), ' ', '')) ASC""",
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
                elif key in ['id', 'identity',"color", "c"]:
                    search_params['identities'].append(prefix_val)
                elif key in ['loc', 'location', 'l']:
                    search_params['locs'].append(prefix_val)
                elif key in ['type', 't']:
                    search_params['types'].append(prefix_val)
                elif key in ["color", "c"]:
                    search_params['colors'].append(prefix_val) #Add functionality for color not identity later
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
        elif val in ['MULTICOLOR','MC']:
            operator = "=" if is_negated else ">"
            conditions.append(f"LENGTH(cd.color_identity) {operator} 1")
        elif val in ['MONOCOLOR', 'MONO']:
            operator = ">" if is_negated else "="
            conditions.append(f"LENGTH(cd.color_identity) {operator} 1")
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
        elif val == "NULL" or val == "unassigned":
            conditions.append(f"i.location_id {'IS NOT' if is_negated else 'IS'} NULL")
        else:
            conditions.append(f"i.location_id {'NOT IN' if is_negated else 'IN'} (SELECT location_id FROM locations WHERE name LIKE ?)")
            params.append(f'%{val}%')

    # USD Logic
    for usd_term in search_params['usd']:
        if usd_term.upper() == "NULL" or usd_term.lower() == "unassigned":
            conditions.append(f"""
                (CASE WHEN i.finish = 'foil' THEN cp.current_price_foil
                ELSE cp.current_price END) {'IS NOT' if is_negated else 'IS'} NULL""")
            continue # Skip the rest of the loop and move to the next term
        
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
