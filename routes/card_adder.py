import sqlite3, ScryfallFetcher, db.db_manager, datetime, csv, os, math
from flask import Blueprint, request, redirect, url_for, render_template, send_from_directory, session, current_app
from db.db_manager import get_db
from flask_login import login_required, current_user
from io import TextIOWrapper
from functools import wraps
from services.card_importer import CardImporterService

def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

adder_bp = Blueprint('adder', __name__)


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return "Access Denied", 403
        return f(*args, **kwargs)

    return decorated_function

@adder_bp.route('/add/inventory', methods=['GET', 'POST'])
@login_required
@admin_required
def adder():
    manager = get_db()
    importer = CardImporterService(manager, fetcher=ScryfallFetcher.ScryfallFetcher(manager))
    
    page = int(request.args.get('page', 1))
    per_page = 25  # Number of cards to load per batch
    offset = (page - 1) * per_page
    
    count_query = '''
        SELECT COUNT(*) 
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        JOIN locations l ON i.location_id = l.location_id
        JOIN price_history ph ON ph.scryfall_id = cp.scryfall_id
    '''
    count_result = manager.cursor.execute(count_query).fetchone()
    total_cards = count_result[0] if count_result else 0
    total_pages = max(1, math.ceil(total_cards / per_page))

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
        LIMIT ? OFFSET ?
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
        
        if sc != "RESET":
            session['last_set_code'] = sc
        
        try:              
            importer.import_single_card(
                set_code=sc,
                collector_number=cn,
                qty=qty,
                location_id=loc_id,
                condition=condition,
                finish="foil" if foil else "nonfoil",
                purchase_price=price if price else 0,
                is_tradeable=trade,
            )
        except Exception as e:
            print(f"Invalid Card Entry: {e}")
        finally:
            # Always ensure the connection is closed before leaving the POST block
            manager.close()
            return redirect(url_for('.adder'))
        
    # Use manager.conn.execute to fetch the rows
    cards = manager.cursor.execute(query, (per_page, offset)).fetchall()   
    locations = manager.cursor.execute(query_2).fetchall()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('card_adder_rows.html', cards=cards)
    
    # Close the connection after we have our data
    manager.close()
    
    last_set_code = session.get('last_set_code', '')
    
    return render_template('card_adder.html', 
                           cards=cards, 
                           locations=locations,
                           page=page, 
                           total_pages=total_pages,
                           last_set_code=last_set_code)

@adder_bp.route('/delete_card/<int:inventory_id>', methods=['POST'])
@login_required
@admin_required
def delete_card(inventory_id):
    manager = get_db()
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
    
    return redirect(request.referrer or url_for('.adder'))

# Route 1: To VIEW the page
@adder_bp.route('/add/inventory/bulk', methods=['GET'])
@login_required
@admin_required
def bulk_import_page():
    manager = get_db()
    # Fetch locations so the user can select a default for the CSV rows
    locations = manager.cursor.execute("SELECT * FROM locations").fetchall()
    manager.close()
    return render_template('bulk_adder.html', locations=locations)

# Route 2: To PROCESS the file
@adder_bp.route('/add/inventory/bulk', methods=['POST'])
@login_required
@admin_required
def bulk_import_action():
    if 'file' not in request.files:
        return redirect(url_for('.bulk_import_page'))
    
    file = request.files['file']
    # Capture default location from the form if specific rows don't have one
    default_loc_id = request.form.get('location_id') if request.form.get('location_id') else 1
    # print(request.form.get('location_id'))

    if file and file.filename.endswith('.csv'):
        csv_file = TextIOWrapper(file.stream, encoding='utf-8')
        reader = csv.DictReader(csv_file)
        
        manager = get_db()
        importer = CardImporterService(
            manager,
            fetcher=ScryfallFetcher.ScryfallFetcher(manager, setting=1),
            commit_batch_size=50,
        )

        try:
            importer.import_bulk_rows(reader, default_location_id=default_loc_id)
                        
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
    
@adder_bp.before_app_request
def clear_stale_set_code():
    """
    Clears the remembered set_code if the user navigates away from the single-card adder.
    """
    # 1. Ensure the request actually has an endpoint (ignores malformed requests)
    if request.endpoint:
        # 2. Define the endpoints where we WANT to keep the set code alive
        # - 'adder.adder' is the main GET/POST page.
        # - 'adder.delete_card' ensures deleting a card doesn't wipe your input.
        # - 'static' ensures loading CSS/JS doesn't accidentally trigger the wipe.
        keep_endpoints = ['adder.adder', 'adder.delete_card', 'static']
        
        if request.endpoint not in keep_endpoints:
            # User navigated away (e.g., to dashboard or bulk import). Nuke the variable.
            session.pop('last_set_code', None)