import sqlite3, ScryfallFetcher, db.db_manager, datetime, csv, os
from flask import Blueprint, request, redirect, url_for, render_template, send_from_directory, current_app
from db.db_manager import CardDB
from flask_login import login_required, current_user
from io import TextIOWrapper

def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

adder_bp = Blueprint('adder', __name__)

@adder_bp.route('/add/inventory', methods=['GET', 'POST'])
@login_required
def adder():
    
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    manager = CardDB()
    fetcher = ScryfallFetcher.ScryfallFetcher(manager)

    query = '''
        SELECT i.instance_id, cd.name, cp.set_code, cp.collector_number, i.added, i.finish, l.name AS location_name, ph.price_usd as nonfoil, ph.price_foil as foil
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        JOIN locations l ON i.location_id = l.location_id
        JOIN price_history ph ON ph.scryfall_id = cp.scryfall_id
        WHERE ph.scraped_At = (
            SELECT MAX(scraped_at) 
            FROM price_history 
            WHERE scryfall_id = cp.scryfall_id
        )
        ORDER BY i.added DESC
    '''
    
    query_2 = '''
        SELECT l.name, l.location_id
        FROM locations l
    '''
    
    if request.method == 'POST':            
        sc = request.form["set_code"]
        cn = request.form['collector_number']
        foil = True if request.form.get('is_foil') == "yes" else False
        trade = 1 if request.form.get('is_tradeable') == "yes" else 0
        condition = request.form['condition']
        price = request.form['price']
        loc_id = request.form.get('location')
        qty = request.form['qty']
        if sc == "RESET" and cn == "":
            # manager.close()
            manager.nuke()
            # Nuke closes the connection internally, so we just redirect
            return redirect(url_for('.adder'))
        surplus_val = 0
        try:              
            card_info = manager.cursor.execute(
                "SELECT scryfall_id FROM card_printings WHERE set_code = ? AND collector_number = ?",
                (sc, cn)
            ).fetchone()
            # print(f"DEBUG: {sc}-{cn}:{card_info}")
            if card_info:
                sid = card_info['scryfall_id']
                
                # 2. Check how many you already have
                current_count = manager.cursor.execute(
                    "SELECT COUNT(*) FROM inventory WHERE scryfall_id = ?", (sid,)
                ).fetchone()[0]

                # 3. If you have 4+, this new one is surplus
                surplus_val = 1 if current_count >= 4 else 0
            
            card_id = fetcher.fetch_and_add(sc, cn)
            
            if card_id:
                for _ in range(int(qty)):
                    manager.cursor.execute("INSERT INTO inventory (scryfall_id, location_id, condition, finish, purchase_price, is_tradeable, added, is_surplus) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                                        (card_id, 
                                            loc_id, 
                                            condition, 
                                            "foil" if foil else "nonfoil", 
                                            price if price else 0, 
                                            trade,
                                            datetime.datetime.now(), 
                                            surplus_val
                                            ))
                    manager.commit()
        except Exception as e:
            print(f"Invalid Card Entry: {e}")
        finally:
            # Always ensure the connection is closed before leaving the POST block
            manager.close()
            return redirect(url_for('.adder'))
        
    # Use manager.conn.execute to fetch the rows
    cards = manager.cursor.execute(query).fetchall()
    locations = manager.cursor.execute(query_2).fetchall()
    
    # Close the connection after we have our data
    manager.close()
    
    return render_template('card_adder.html', cards=cards, locations=locations)

@adder_bp.route('/delete_card/<int:inventory_id>', methods=['POST'])
def delete_card(inventory_id):
    manager = CardDB()
    try:
        # 1. Get the set_code of the card BEFORE deleting it
        # This allows us to check the set's status after the card is gone
        set_info = manager.cursor.execute('''
            SELECT cp.set_code 
            FROM inventory i
            JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            WHERE i.instance_id = ?
        ''', (inventory_id,)).fetchone()

        if set_info:
            set_code = set_info['set_code']

            # 2. Delete the specific card instance
            manager.cursor.execute("DELETE FROM inventory WHERE instance_id = ?", (inventory_id,))
            
            # 3. Check if any cards from that set remain in the inventory
            remaining_count = manager.cursor.execute('''
                SELECT COUNT(*) 
                FROM inventory i
                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                WHERE cp.set_code = ?
            ''', (set_code,)).fetchone()[0]

            # 4. If the set is now empty in your inventory, delete the set record
            if remaining_count == 0:
                # Replace 'sets' with your actual set tracker table name if different
                manager.cursor.execute("DELETE FROM sets WHERE set_code = ?", (set_code,))
                print(f"Set {set_code} was empty and has been removed.")

            manager.commit()
            
    except Exception as e:
        print(f"Error deleting card or cleaning up set: {e}")
    finally:
        manager.close()
    
    return redirect(url_for('adder.adder'))

# Route 1: To VIEW the page
@adder_bp.route('/add/inventory/bulk', methods=['GET'])
@login_required
def bulk_import_page():
    manager = CardDB()
    # Fetch locations so the user can select a default for the CSV rows
    locations = manager.cursor.execute("SELECT * FROM locations").fetchall()
    manager.close()
    return render_template('bulk_adder.html', locations=locations)

# Route 2: To PROCESS the file
@adder_bp.route('/add/inventory/bulk', methods=['POST'])
@login_required
def bulk_import_action():
    # 1. Admin check (from your adder logic)
    if current_user.role != 'admin':
        return "Access Denied", 403

    if 'file' not in request.files:
        return redirect(url_for('.bulk_import_page'))
    
    file = request.files['file']
    # Capture default location from the form if specific rows don't have one
    default_loc_id = request.form.get('location_id') if request.form.get('location_id') else 1
    # print(request.form.get('location_id'))

    if file and file.filename.endswith('.csv'):
        csv_file = TextIOWrapper(file.stream, encoding='utf-8')
        reader = csv.DictReader(csv_file)
        
        manager = CardDB()
        fetcher = ScryfallFetcher.ScryfallFetcher(manager)

        # Helper function to dynamically get or create locations
        def get_loc(loc_name):
            rec = manager.cursor.execute("SELECT location_id FROM locations WHERE name = ?", (loc_name,)).fetchone()              
            if rec:
                return rec['location_id']
            
            # 3. If it DOES NOT exist, fetch the 'Unsorted Box' instead
            unsorted_rec = manager.cursor.execute("SELECT location_id FROM locations WHERE name = 'Unsorted Box'").fetchone()
            
            if unsorted_rec:
                return unsorted_rec['location_id']
                
            # 4. Ultimate fallback: If even "Unsorted Box" doesn't exist in your DB yet, create it.
            manager.cursor.execute("INSERT INTO locations (name) VALUES ('Unsorted Box')")
            return manager.cursor.lastrowid

        try:
            for row in reader:
                # Map CSV headers to your variables
                sc = row.get('set_code', '').strip()
                cn = row.get('collector_number', '').strip()
                qty = int(row.get('qty', 1))
                finish = row.get('finish', 'nonfoil').lower()
                raw_dest = row.get('location', 'Unsorted Box').strip().title()
                tradeable = 1 if row.get('tradeable') == "yes" else 0
                
                # Check surplus status (Logic from your adder)
                surplus_val = 0
                card_info = manager.cursor.execute(
                    "SELECT scryfall_id FROM card_printings WHERE set_code = ? AND collector_number = ?",
                    (sc, cn)
                ).fetchone()

                if card_info:
                    sid = card_info['scryfall_id']
                    current_count = manager.cursor.execute(
                        "SELECT COUNT(*) FROM inventory WHERE scryfall_id = ?", (sid,)
                    ).fetchone()[0]
                    surplus_val = 1 if current_count >= 4 else 0
                
                # Use your existing fetcher logic to ensure card data exists
                card_id = fetcher.fetch_and_add(sc, cn)
                
                if card_id:
                    # Fetch the unified card name to ensure all arts/versions are counted toward the playset
                    name_rec = manager.cursor.execute("""
                        SELECT cd.name FROM card_definitions cd
                        JOIN card_printings cp ON cd.oracle_id = cp.oracle_id
                        WHERE cp.scryfall_id = ?
                    """, (card_id,)).fetchone()
                    card_name = name_rec['name'] if name_rec else ""

                    for _ in range(qty):
                        # Start with the form's default location
                        assigned_loc_id = default_loc_id

                        # Apply Auto-Sorting & Playset Logic for THIS specific copy
                        if raw_dest == "Master":
                            target_binder_name = f"{sc.upper()} Master Set Binder"
                            bulk_box_name = f"{sc.upper()} Bulk Box"
                            
                            binder_loc_id = get_loc(target_binder_name)
                            bulk_loc_id = get_loc(bulk_box_name)
                                
                            # Query the database to count foils and nonfoils in the binder
                            count_query = """
                                SELECT 
                                    SUM(CASE WHEN i.finish = 'foil' THEN 1 ELSE 0 END) as foil_count,
                                    SUM(CASE WHEN i.finish = 'nonfoil' THEN 1 ELSE 0 END) as nonfoil_count
                                FROM inventory i
                                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                                JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                                WHERE cd.name = ? AND i.location_id = ?
                            """
                            counts = manager.cursor.execute(count_query, (card_name, binder_loc_id)).fetchone()
                            
                            foil_count = counts['foil_count'] if counts and counts['foil_count'] else 0
                            nonfoil_count = counts['nonfoil_count'] if counts and counts['nonfoil_count'] else 0
                            total_count = foil_count + nonfoil_count

                            # THE FOIL UPGRADER LOGIC
                            if finish == "nonfoil":
                                if total_count >= 4:
                                    tradeable = 1
                                    assigned_loc_id = bulk_loc_id
                                else:
                                    assigned_loc_id = binder_loc_id
                                    
                            elif finish == "foil":
                                if foil_count >= 4:
                                    tradeable = 1
                                    assigned_loc_id = bulk_loc_id
                                elif total_count >= 4 and nonfoil_count > 0:
                                    # Binder full, kick the most recently added nonfoil to bulk
                                    kick_query = """
                                        SELECT i.instance_id FROM inventory i
                                        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                                        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                                        WHERE cd.name = ? AND i.location_id = ? AND i.finish = 'nonfoil'
                                        ORDER BY i.added DESC LIMIT 1
                                    """
                                    kick_target = manager.cursor.execute(kick_query, (card_name, binder_loc_id)).fetchone()
                                    
                                    if kick_target:
                                        manager.cursor.execute(
                                            "UPDATE inventory SET location_id = ? WHERE instance_id = ?",
                                            (bulk_loc_id, kick_target['instance_id'])
                                        )
                                        # Save the kick immediately so the counts update correctly
                                        manager.commit() 
                                    
                                    assigned_loc_id = binder_loc_id
                                else:
                                    assigned_loc_id = binder_loc_id
                                    
                        elif raw_dest == "Bulk":
                            tradeable = 1
                            assigned_loc_id = get_loc(f"{sc.upper()} Bulk Box")
                            
                        elif raw_dest == "Trade":
                            tradeable = 1
                            assigned_loc_id = get_loc("Trades")

                        # Proceed with original insert logic using our dynamically calculated `assigned_loc_id`
                        manager.cursor.execute("""
                            INSERT INTO inventory (
                                scryfall_id, location_id, condition, finish, 
                                purchase_price, is_tradeable, added, is_surplus
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            card_id, 
                            assigned_loc_id, 
                            "NM", # Defaulting to Near Mint for bulk
                            finish, 
                            0,    # Defaulting price to 0 for bulk
                            tradeable,    # Defaulting tradeable to 1
                            datetime.datetime.now(), 
                            surplus_val
                        ))
                        # We commit inside the loop so if qty=4, the queries correctly see the count go 1, 2, 3, 4!
                        manager.commit()
                        
        except Exception as e:
            print(f"Bulk Import Error: {e}")
        finally:
            manager.close()
            
    return redirect(url_for('inventory.inventory'))

@adder_bp.route('/download_template')
@login_required # Optional: recommended if you only want admins downloading it
def download_template():
    # current_app.root_path ensures the 'static' folder is found relative to the app root
    static_dir = os.path.join(current_app.root_path, 'static')
    
    # This sends 'template.csv' from your /static folder
    return send_from_directory(
        static_dir, 
        'template.csv', 
        as_attachment=True,
        download_name='mtg_bulk_import_template.csv'
    )