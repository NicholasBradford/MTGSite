import sqlite3

def import_deck_from_file(file_path, deck_name, color_identity):
    conn = sqlite3.connect('mtg_inventory.db')
    cursor = conn.cursor()
    
    # 1. Create the Deck Entry
    # We need a placeholder for commander_scryfall_id until the user specifies one
    cursor.execute('''
        INSERT INTO edh_decks (deck_name, commander_scryfall_id, color_identity) 
        VALUES (?, ?, ?)
    ''', (deck_name, 'PLACEHOLDER_ID', color_identity))
    deck_id = cursor.lastrowid
    
    # 2. Parse and Insert Cards
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            
            # Simple parser: "1 Sol Ring"
            parts = line.split(' ', 1)
            quantity = int(parts[0])
            card_name = parts[1]
            
            # Match card name to a scryfall_id from card_printings 
            # (Joining with card_definitions to find by name)
            res = cursor.execute('''
                SELECT cp.scryfall_id 
                FROM card_printings cp
                JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE cd.name = ? 
                LIMIT 1
            ''', (card_name,)).fetchone()
            
            if res:
                scryfall_id = res[0]
                cursor.execute('''
                    INSERT INTO edh_deck_cards (deck_id, scryfall_id, quantity, category)
                    VALUES (?, ?, ?, ?)
                ''', (deck_id, scryfall_id, quantity, 'Mainboard'))
    
    conn.commit()
    conn.close()
    print(f"Deck '{deck_name}' imported successfully.")