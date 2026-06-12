import sqlite3, os, re, time, requests
from flask import Blueprint, render_template, request, abort, redirect, url_for, flash
from flask_login import login_required, current_user
from functools import wraps
# Import your database connection utility here, e.g., get_db
from db.db_manager import get_db          
from ScryfallFetcher import ScryfallFetcher

# Initialize Blueprint
edh_bp = Blueprint('edh', __name__, template_folder='templates')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403) # Forbidden
        return f(*args, **kwargs)
    return decorated_function

def resolve_card_by_name(card_name, db, fetcher):
    """
    Checks if a card exists in the DB by name. 
    If not, fetches its set/collector info from Scryfall and uses ScryfallFetcher to add it.
    """
    # 1. Check local DB first
    db.cursor.execute('''
        SELECT cp.scryfall_id 
        FROM card_printings cp
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE cd.name = ? 
        LIMIT 1
    ''', (card_name,))
    res = db.cursor.fetchone()
    
    if res:
        return res['scryfall_id']
        
    # 2. Not in DB - Ping Scryfall by exact name
    print(f"Card not found locally. Resolving '{card_name}' via Scryfall API...")
    
    # Scryfall API requires a slight delay to respect rate limits (10 requests / second)
    time.sleep(0.1) 
    
    # Use exact match. (Scryfall handles "Face A // Face B" syntax cleanly)
    api_url = f"https://api.scryfall.com/cards/named?exact={card_name}"
    response = requests.get(api_url, headers={'User-Agent': 'MTG-Collection-Tracker/1.0'})
    
    if response.status_code == 200:
        data = response.json()
        set_code = data.get('set')
        collector_number = str(data.get('collector_number'))
        
        # 3. Trigger your existing ScryfallFetcher tool
        print(f"Found {card_name} in set {set_code.upper()} #{collector_number}. Downloading...")
        scryfall_id = fetcher.fetch_and_add(set_code, collector_number)
        
        return scryfall_id
    else:
        print(f"WARNING: Could not find '{card_name}' on Scryfall. Skipping.")
        return None

def parse_decklist_file(file_path):
    """
    Parses a text decklist. 
    Returns a list of tuples: (quantity, card_name, category)
    """
    decklist_data = []
    current_category = "Mainboard" # Default category
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: 
                continue
            
            # Match lines that start with a number (e.g., "1 Sol Ring")
            match = re.match(r'^(\d+)\s+(.+)$', line)
            
            if match:
                qty = int(match.group(1))
                name = match.group(2).strip()
                decklist_data.append((qty, name, current_category))
            else:
                # If it doesn't start with a number, it's a category header
                # Clean up formatting like "Commander" -> "Commander"
                clean_cat = re.sub(r'\[.*?\]\s*', '', line).strip()
                if clean_cat:
                    current_category = clean_cat
                    
    return decklist_data


# --- Main Import Logic ---

def process_and_import_deck(file_path, deck_name, color_identity, commander_name):
    """Orchestrates parsing the file, fetching missing data, and saving to the DB."""
    
    # Use the CardDB class directly so it plays nicely with ScryfallFetcher
    db = get_db() 
    fetcher = ScryfallFetcher(db,setting=1)
    
    try:
        # 1. Ensure Commander is in DB (Required for foreign key)
        commander_scryfall_id = resolve_card_by_name(commander_name, db, fetcher)
        if not commander_scryfall_id:
            raise ValueError(f"Could not resolve Commander '{commander_name}'. Import aborted.")
        
        # 2. Create Deck Record
        db.cursor.execute('''
            INSERT INTO edh_decks (deck_name, commander_scryfall_id, color_identity) 
            VALUES (?, ?, ?)
        ''', (deck_name, commander_scryfall_id, color_identity))
        
        deck_id = db.cursor.lastrowid
        
        # 3. Parse and Insert Cards
        parsed_cards = parse_decklist_file(file_path)
        
        for qty, card_name, category in parsed_cards:
            scryfall_id = resolve_card_by_name(card_name, db, fetcher)
            
            if scryfall_id:
                db.cursor.execute('''
                    INSERT INTO edh_deck_cards (deck_id, scryfall_id, quantity, category)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(deck_id, scryfall_id) DO UPDATE SET quantity = quantity + ?
                ''', (deck_id, scryfall_id, qty, category, qty))
                
        db.commit()
        print(f"Deck '{deck_name}' imported successfully.")
        
    except Exception as e:
        print(f"Error during import: {e}")
        db.conn.rollback() # Ensure partial data isn't saved on failure
        raise e
    finally:
        db.close()

def get_available_card_location(self, scryfall_id):
    query = '''
        SELECT 
            i.instance_id,
            i.scryfall_id,
            i.finish,
            i.purchase_price,
            i.added,
            l.location_id,
            cp.set_code,
            s.set_name,
            l.name AS location_name
        FROM inventory i
        LEFT JOIN locations l ON i.location_id = l.location_id
        LEFT JOIN card_printings cp on i.scryfall_id = cp.scryfall_id
        LEFT JOIN sets s on s.set_code = cp.set_code
        WHERE i.scryfall_id = ? 
          AND (i.in_deck = 0 OR i.in_deck IS NULL)
        ORDER BY 
            i.purchase_price ASC,  -- Prioritize the cheapest version first
            i.added DESC           -- Tie-breaker: most recently added
        LIMIT 1
    '''
    
    result = self.cursor.execute(query, (scryfall_id,)).fetchone()
    return result

# --- Routes ---

@edh_bp.route('/edh/import', methods=['GET', 'POST'])
@login_required
@admin_required
def import_deck():
    if request.method == 'POST':
        if 'file' not in request.files:
            return "No file part", 400
            
        file = request.files['file']
        if file.filename == '':
            return "No selected file", 400
        
        # Required Form Fields
        deck_name = request.form.get('deck_name')
        color_identity = request.form.get('color_identity')
        commander_name = request.form.get('commander_name')
        
        if not all([deck_name, color_identity, commander_name]):
            return "Missing required deck details.", 400
        
        # Save file to uploads directory
        upload_folder = os.path.join(os.getcwd(), 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        file_path = os.path.join(upload_folder, file.filename)
        file.save(file_path)
        
        # Process the Import
        try:
            process_and_import_deck(file_path, deck_name, color_identity, commander_name)
            
            # Clean up the text file after successful import
            os.remove(file_path) 
            return "Deck Imported Successfully!"
        except Exception as e:
            return f"Failed to import deck: {str(e)}", 500
        
    return render_template('edh_import.html')

@edh_bp.route('/edh/gallery')
def edh_gallery():
    """Renders the EDH Gallery View."""
    db = get_db() 
    
    decks = db.cursor.execute('''
        SELECT 
            d.deck_id, 
            d.deck_name, 
            d.color_identity, 
            cd.name AS commander_name, 
            cp.image_url
        FROM edh_decks d
        JOIN card_printings cp ON d.commander_scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        ORDER BY d.deck_name DESC
    ''').fetchall()
    
    db.close()
    return render_template('edh_gallery.html', decks=decks)

@edh_bp.route('/edh/view/<deck_name>')
def edh_view(deck_name):
    """Renders a specific EDH Deck matching the URL path name, categorized with collection status."""
    if not deck_name:
        abort(404, description="Deck name is required.")
        
    db = get_db()
    
    # 1. Get Deck & Commander Info
    deck = db.cursor.execute('''
        SELECT 
            d.deck_id, 
            d.deck_name, 
            d.color_identity, 
            cd.name AS commander_name, 
            cp.image_url
        FROM edh_decks d
        JOIN card_printings cp ON d.commander_scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE d.deck_name = ?
    ''', (deck_name,)).fetchone()

    if not deck:
        db.close()
        abort(404, description=f"Deck '{deck_name}' not found.")

    # 2. Get Deck Cards & Collection Status (Added cd.oracle_id to SELECT)
    cards_data = db.cursor.execute('''
        SELECT 
            dc.scryfall_id, 
            cd.name, 
            cd.mana_cost,
            cd.cmc,
            cd.type_line,
            cd.oracle_id,
            dc.quantity AS quantity_needed,
            
            (
                SELECT COUNT(i.instance_id) 
                FROM inventory i
                JOIN card_printings inv_cp ON i.scryfall_id = inv_cp.scryfall_id
                WHERE inv_cp.oracle_id = cd.oracle_id
            ) AS quantity_owned

        FROM edh_deck_cards dc
        LEFT JOIN card_printings cp ON dc.scryfall_id = cp.scryfall_id
        LEFT JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE dc.deck_id = ? AND cd.name != ?
        ORDER BY cd.cmc ASC, cd.name ASC
    ''', (deck['deck_id'], deck['commander_name'])).fetchall()
    
    # 3. Process Data for Jinja Rendering
    categorized_cards = {
        'Creatures': [], 'Artifacts': [], 'Enchantments': [], 
        'Planeswalkers': [], 'Battles': [], 'Instants': [], 
        'Sorceries': [], 'Lands': []
    }
    mana_curve = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, '6+': 0}
    
    for row in cards_data:
        card = dict(row)
        
        # --- NEW INVENTORY LOCATION LOOKUP ---
        # Look up the location for the cheapest/newest available copy.
        # We match on `oracle_id` so that ANY physical printing of the card you own works.
        # Look up the location for the cheapest/newest available copy.
        best_copy = db.cursor.execute('''
            SELECT 
                l.name AS location_name,
                i.purchase_price,
                s.set_name              -- FETCH THE SET NAME
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            LEFT JOIN locations l ON i.location_id = l.location_id
            LEFT JOIN sets s ON cp.set_code = s.set_code    -- JOIN THE SETS TABLE
            WHERE cp.oracle_id = ? 
              AND (i.in_deck = 0 OR i.in_deck IS NULL)
            ORDER BY 
                i.purchase_price ASC,  
                i.added DESC           
            LIMIT 1
        ''', (card['oracle_id'],)).fetchone()

        if best_copy:
            card['available_location'] = best_copy['location_name'] or "Unassigned"
            card['available_price'] = best_copy['purchase_price']
            card['set_name'] = best_copy['set_name']        
        else:
            card['available_location'] = "Unavailable"
            card['available_price'] = None
            card['set_name'] = None                         
        # -------------------------------------

        type_line = card.get('type_line', '') or ''
        
        if 'Land' in type_line:
            cat = 'Lands'
        elif 'Creature' in type_line:
            cat = 'Creatures'
        elif 'Planeswalker' in type_line:
            cat = 'Planeswalkers'
        elif 'Battle' in type_line:
            cat = 'Battles'
        elif 'Instant' in type_line:
            cat = 'Instants'
        elif 'Sorcery' in type_line:
            cat = 'Sorceries'
        elif 'Enchantment' in type_line:
            cat = 'Enchantments'
        elif 'Artifact' in type_line:
            cat = 'Artifacts'
        else:
            cat = 'Other'

        card['is_owned'] = card['quantity_owned'] >= card['quantity_needed']
        categorized_cards[cat].append(card)
        
        if cat != 'Lands':
            cmc = int(card['cmc']) if card['cmc'] is not None else 0
            if cmc >= 6:
                mana_curve['6+'] += card['quantity_needed']
            else:
                mana_curve[cmc] += card['quantity_needed']

    # CRITICAL: Moved db.close() to AFTER the loop completes!
    db.close()

    total_non_lands = sum(mana_curve.values())
    mana_curve_percentages = {
        k: (v / total_non_lands * 100) if total_non_lands > 0 else 0 
        for k, v in mana_curve.items()
    }

    active_categories = {k: v for k, v in categorized_cards.items() if len(v) > 0}

    return render_template(
        'edh_detail.html', 
        deck=deck, 
        cards=active_categories, 
        mana_curve=mana_curve_percentages
    )
    
@edh_bp.route('/deck/<int:deck_id>/add/<string:scryfall_id>', methods=['POST'])
@login_required
@admin_required
def add_card_to_deck(deck_id, scryfall_id):
    """Increments the quantity of a card in the deck."""
    db = get_db()
    
    # 1. Fetch deck_name for the redirect 
    deck_row = db.cursor.execute("SELECT deck_name FROM edh_decks WHERE deck_id = ?", (deck_id,)).fetchone()
    if not deck_row:
        db.close()
        abort(404, description="Deck not found.")
    deck_name = deck_row['deck_name']
    
    # 2. Increment existing quantity
    db.cursor.execute("""
        UPDATE edh_deck_cards 
        SET quantity = quantity + 1 
        WHERE deck_id = ? AND scryfall_id = ?
    """, (deck_id, scryfall_id))
    
    # 3. If rowcount is 0, the card wasn't in the deck. We INSERT it.
    if db.cursor.rowcount == 0:
        db.cursor.execute("""
            INSERT INTO edh_deck_cards (deck_id, scryfall_id, quantity, category) 
            VALUES (?, ?, 1, 'Mainboard')
        """, (deck_id, scryfall_id))
        flash('Card added to deck.', 'success')
    else:
        flash('Card quantity increased.', 'success')

    db.commit()
    db.close()
    
    # Redirect back to the view using the Blueprint format
    return redirect(url_for('edh.edh_view', deck_name=deck_name))


@edh_bp.route('/deck/<int:deck_id>/remove/<string:scryfall_id>', methods=['POST'])
@login_required
@admin_required
def remove_card_from_deck(deck_id, scryfall_id):
    """Decrements the quantity of a card in the deck or removes it if 0."""
    db = get_db()

    # 1. Fetch deck_name for the redirect
    deck_row = db.cursor.execute("SELECT deck_name FROM edh_decks WHERE deck_id = ?", (deck_id,)).fetchone()
    if not deck_row:
        db.close()
        abort(404, description="Deck not found.")
    deck_name = deck_row['deck_name']

    # 2. Retrieve current quantity
    row = db.cursor.execute("""
        SELECT quantity 
        FROM edh_deck_cards 
        WHERE deck_id = ? AND scryfall_id = ?
    """, (deck_id, scryfall_id)).fetchone()

    if row:
        current_qty = row['quantity'] if isinstance(row, dict) else row[0]
        
        if current_qty > 1:
            db.cursor.execute("""
                UPDATE edh_deck_cards 
                SET quantity = quantity - 1 
                WHERE deck_id = ? AND scryfall_id = ?
            """, (deck_id, scryfall_id))
            flash('Card quantity decreased.', 'success')
        else:
            db.cursor.execute("""
                DELETE FROM edh_deck_cards 
                WHERE deck_id = ? AND scryfall_id = ?
            """, (deck_id, scryfall_id))
            flash('Card removed from deck.', 'success')
            
        db.commit()
    else:
        flash('Card not found in deck.', 'error')

    db.close()
    return redirect(url_for('edh.edh_view', deck_name=deck_name))

@edh_bp.route('/deck/<int:deck_id>/add_by_name', methods=['POST'])
@login_required
@admin_required
def add_card_to_deck_by_name(deck_id):
    """Adds a card to a deck via a typed text string, resolving missing data via Scryfall."""
    card_name = request.form.get('card_name')
    if not card_name:
        flash('Please enter a card name.', 'error')
        return redirect(url_for('edh.edh_gallery'))

    db = get_db()
    
    # 1. Fetch deck_name for the redirect
    deck_row = db.cursor.execute("SELECT deck_name FROM edh_decks WHERE deck_id = ?", (deck_id,)).fetchone()
    if not deck_row:
        db.close()
        abort(404, description="Deck not found.")
    deck_name = deck_row['deck_name']

    # 2. Resolve the card by name using your existing helper
    fetcher = ScryfallFetcher(db, setting=1)
    scryfall_id = resolve_card_by_name(card_name.strip(), db, fetcher)

    if not scryfall_id:
        flash(f"Could not find the card '{card_name}' on Scryfall. Check spelling.", 'error')
        db.close()
        return redirect(url_for('edh.edh_view', deck_name=deck_name))

    # 3. Add to deck (Increment quantity if it exists)
    db.cursor.execute("""
        UPDATE edh_deck_cards 
        SET quantity = quantity + 1 
        WHERE deck_id = ? AND scryfall_id = ?
    """, (deck_id, scryfall_id))
    
    # 4. If rowcount is 0, the card wasn't in the deck. We INSERT it.
    if db.cursor.rowcount == 0:
        db.cursor.execute("""
            INSERT INTO edh_deck_cards (deck_id, scryfall_id, quantity, category) 
            VALUES (?, ?, 1, 'Mainboard')
        """, (deck_id, scryfall_id))
        flash(f"'{card_name}' added to deck.", 'success')
    else:
        flash(f"Quantity of '{card_name}' increased.", 'success')

    db.commit()
    db.close()
    
    # Redirect back to the view
    return redirect(url_for('edh.edh_view', deck_name=deck_name))

@edh_bp.route('/edh/assign_card/<int:deck_id>/<oracle_id>', methods=['POST'])
@login_required
def assign_card_to_deck(deck_id, oracle_id):
    db = get_db()

    deck_row = db.cursor.execute("SELECT deck_name FROM edh_decks WHERE deck_id = ?", (deck_id,)).fetchone()
    if not deck_row:
        db.close()
        abort(404, description="Deck not found.")
    deck_name = deck_row['deck_name']
    
    # 1. Find the best available physical copy of this card
    best_copy = db.cursor.execute('''
        SELECT i.instance_id
        FROM inventory i
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        WHERE cp.oracle_id = ? 
          AND (i.in_deck = 0 OR i.in_deck IS NULL)
          AND i.deck_id IS NULL
        ORDER BY i.purchase_price ASC, i.added DESC
        LIMIT 1
    ''', (oracle_id,)).fetchone()

    if not best_copy:
        flash("No available copies found in your inventory.", "error")
        db.close()
        return redirect(url_for('edh.edh_view', deck_name=deck_name))

    # 2. Lock this specific instance to the deck
    instance_id = best_copy['instance_id']
    
    db.cursor.execute('''
        UPDATE inventory 
        SET in_deck = 1, 
            deck_id = ?, 
            location_id = NULL -- Optional: Remove it from its storage box
        WHERE instance_id = ?
    ''', (deck_id, instance_id))
    
    db.conn.commit()
    db.close()
    
    flash("Card successfully assigned to deck!", "success")
    return redirect(url_for('edh.edh_view', deck_name=deck_name))