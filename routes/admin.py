import os, datetime
from flask import Blueprint,request,flash,redirect,url_for, render_template, abort
from functools import wraps
from ScryfallFetcher import ScryfallFetcher
from flask_login import login_required, current_user
from db.db_manager import CardDB

admin_bp = Blueprint('admin', __name__)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403) # Forbidden
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/admin')
@login_required
@admin_required
def admin():
    return render_template('admin.html')


@admin_bp.route('/manage_locations', methods=['GET', 'POST'])
@login_required
def manage_locations():
    manager = CardDB()
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            name = request.form.get('location_name')
            manager.cursor.execute("INSERT INTO locations (name) VALUES (?)", (name,))
            manager.commit()
            flash(f"Added location: {name}")

        elif action == 'delete':
            loc_id = request.form.get('location_id')
            # Check if cards are still in this location first
            count = manager.cursor.execute("SELECT COUNT(*) FROM inventory WHERE location_id=?", (loc_id,)).fetchone()[0]
            if count == 0:
                manager.cursor.execute("DELETE FROM locations WHERE location_id=?", (loc_id,))
                manager.commit()
                flash("Location deleted.")
            else:
                flash(f"Cannot delete: {count} cards are still assigned here!", "error")

        elif action == 'update_id':
            old_id = request.form.get('old_id')
            new_id = request.form.get('new_id')
            new_name = request.form.get('new_name')
            
            try:
                # 1. Update the location record first
                manager.cursor.execute("UPDATE locations SET location_id=?, name=? WHERE location_id=?", (new_id, new_name, old_id))

                # 2. Immediately update the inventory to match
                manager.cursor.execute("UPDATE inventory SET location_id=? WHERE location_id=?", (new_id, old_id))

                manager.commit()
                flash(f"Location updated to ID {new_id} and Name '{new_name}'.")
            except Exception as e:
                manager.conn.rollback() # Roll back if the new ID already exists
                flash(f"Error: Could not change ID. {e}", "error")

        manager.close()
        return redirect(url_for('admin.manage_locations'))

    # GET: Fetch all locations and their card counts for insight
    query = """
        SELECT l.location_id, l.name, COUNT(i.instance_id) as card_count 
        FROM locations l 
        LEFT JOIN inventory i ON l.location_id = i.location_id 
        GROUP BY l.location_id
    """
    locations = manager.cursor.execute(query).fetchall()
    manager.close()
    return render_template("manage_locations.html", locations=locations)

@admin_bp.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    manager = CardDB()
    try:
        # --- 1. EXISTING STATS (Do not change) ---
        total_cards = manager.cursor.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        unique_cards = manager.cursor.execute("SELECT COUNT(DISTINCT scryfall_id) FROM inventory").fetchone()[0]
        
        # --- 2. EXISTING DELICATE TRADE LOGIC (Do not change) ---
        active_trades = manager.cursor.execute("SELECT * FROM trades WHERE status = 'Pending'").fetchall()
        trades = manager.cursor.execute('''
            SELECT t.*, u.username AS submitter_name
            FROM trades t
            JOIN users u ON t.user_id = u.user_id
            WHERE t.status = 'Pending' 
            ORDER BY t.created_at ASC
        ''').fetchall()
        
        # Convert to a list of dicts so we can append items to them
        pending_trades = [dict(t) for t in trades]
        
        # Fetch the requested cards for each trade
        for trade in pending_trades:
            items = manager.cursor.execute('''
                SELECT ti.quantity, ti.finish, cd.name, cp.set_code, cp.collector_number
                FROM trade_outbound_items ti
                JOIN card_printings cp ON ti.scryfall_id = cp.scryfall_id
                JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE ti.trade_id = ?
            ''', (trade['trade_id'],)).fetchall()
            
            trade['requested_cards'] = [dict(i) for i in items]
        
        # --- 3. NEW DASHBOARD METRICS (Add these) ---
        
        # A. Data Integrity: Cards with $0.00 Value (Missing Price or Unrecognized Finish like Etched)
        zero_value_count = manager.cursor.execute('''
            SELECT COUNT(i.instance_id) 
            FROM inventory i 
            LEFT JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            WHERE (i.finish = 'foil' AND COALESCE(cp.current_price_foil, 0) = 0)
               OR (i.finish = 'nonfoil' AND COALESCE(cp.current_price, 0) = 0)
               OR (i.finish NOT IN ('foil', 'nonfoil'))
        ''').fetchone()[0]

        # B. Logistics: Cards sitting in the void (Unassigned Location)
        unassigned_cards = manager.cursor.execute('''
            SELECT COUNT(*) FROM inventory WHERE location_id IS NULL OR location_id = ''
        ''').fetchone()[0]

        # C. Infrastructure: DB File Size (Monitors Render Persistent Disk limits)
        try:
            # Assuming manager.db_path points to 'db/mtg_inventory.db'
            db_size_mb = round(os.path.getsize(os.environ.get('DB_PATH')) / (1024 * 1024), 2)
        except (AttributeError, FileNotFoundError):
            db_size_mb = "Unknown"

    except Exception as e:
        flash(f"Error loading dashboard data: {e}", "error")
        total_cards, unique_cards, active_trades = 0, 0, []
        zero_value_count, unassigned_cards, db_size_mb = 0, 0, 0
    finally:
        manager.close()

    # Make sure to add the new variables to your return statement!
    return render_template('admin_dashboard.html', 
                           total_cards=total_cards, 
                           unique_cards=unique_cards, 
                           active_trades=active_trades,
                           zero_value_count=zero_value_count,
                           unassigned_cards=unassigned_cards,
                           pending_trades=pending_trades,
                           db_size_mb=db_size_mb)

def process_incoming_cards(incoming, manager):
    if not incoming:
        return
    
    # Initialize the fetcher with your current db manager
    fetcher = ScryfallFetcher(manager)
    
    incoming_items = [item.strip() for item in incoming.split(',')]
    
    for item in incoming_items:
        if not item: 
            continue
        
        parts = item.split('-')
        if len(parts) != 3:
            print(f"Skipping badly formatted item: {item}")
            continue
            
        set_code, cn, finish = parts
        
        # 1. Try to find the card in the local DB first
        card = manager.cursor.execute('''
            SELECT scryfall_id FROM card_printings 
            WHERE set_code = ? AND collector_number = ?
        ''', (set_code.lower(), cn.lower())).fetchone()
        
        scryfall_id = None
        
        # print(f"DEBUG: {dict(card)}")

        if card:
            scryfall_id = card['scryfall_id']
        else:
            # 2. If NOT found, use ScryfallFetcher to get it from the API
            print(f"Card {set_code}-{cn} not in DB. Fetching from Scryfall...")
            card = fetcher.fetch_and_add(set_code, cn)


        # 3. If we have a scryfall_id (either from DB or Fetcher), add to inventory
        if card:
            manager.cursor.execute('''
                INSERT INTO inventory (scryfall_id, finish, is_surplus, is_tradeable, location_id, added)
                VALUES (?, ?, 0, 0, 1, ?)
            ''', (card['scryfall_id'], finish.lower(),datetime.datetime.now(),))
            print(f"Successfully added {set_code}-{cn} ({finish}) to inventory!")
            return True
        else:
            print(f"Error: Card {set_code}-{cn} could not be found or fetched.")
            return False

@admin_bp.route('/process_trade', methods=['POST'])
@login_required
@admin_required
def process_trade():
    trade_id = request.form.get('trade_id')
    action = request.form.get('action')
    incoming_cards = request.form.get('incoming_cards')
    trade_notes = request.form.get('trade_notes')

    manager = CardDB()
    try:
        if action == 'accept':
            new_status = 'Accepted'
            
            check = process_incoming_cards(incoming_cards, manager)
            if not check:
                raise Exception("Invalid Trade: One or more incoming cards could not be resolved.")
            
            # --- INVENTORY REMOVAL LOGIC ---
            outbound_items = manager.cursor.execute('''
                SELECT scryfall_id, finish, quantity 
                FROM trade_outbound_items 
                WHERE trade_id = ?
            ''', (trade_id,)).fetchall()
            
            # For each group of cards they requested...
            for item in outbound_items:
                # Find exactly [quantity] instance_ids from your inventory that match
                instances = manager.cursor.execute('''
                    SELECT instance_id 
                    FROM inventory 
                    WHERE scryfall_id = ? AND finish = ? AND is_tradeable = 1
                    LIMIT ?
                ''', (item['scryfall_id'], item['finish'], item['quantity'])).fetchall()
                
            # Delete those specific physical copies from your database
                for instance in instances:
                        manager.cursor.execute('''
                            DELETE FROM inventory WHERE instance_id = ?
                        ''', (instance['instance_id'],))
                
                
                    
        elif action == 'deny':
            new_status = 'Rejected'
        else:
            return "Invalid action", 400 # Just in case something weird happens
        
        manager.cursor.execute('''
            UPDATE trades SET status = ?, notes = ?, incoming = ? WHERE trade_id = ?
        ''', (new_status, trade_notes, incoming_cards, trade_id))
        manager.commit()
        flash(f"Trade {trade_id} {new_status.lower()} successfully.")
    except Exception as e:
        flash(f"Error processing trade: {e}", "error")
    finally:
        manager.close()

    return redirect(url_for('admin.admin_dashboard'))