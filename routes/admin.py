import os, datetime, time, requests, json
from flask import Blueprint, Response, request, flash, redirect, url_for, render_template, abort, jsonify
from functools import wraps
import ScryfallFetcher
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


@admin_bp.route('/admin/locations', methods=['GET', 'POST'])
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
        
        stocks = manager.cursor.execute('''
            WITH RankedPrices AS (
                SELECT 
                    scryfall_id, 
                    price_usd, 
                    ROW_NUMBER() OVER(PARTITION BY scryfall_id ORDER BY scraped_at DESC) as rn
                FROM price_history
            ),
            PriceShifts AS (
                SELECT 
                    curr.scryfall_id,
                    prev.price_usd AS old_price,
                    curr.price_usd AS new_price
                FROM RankedPrices curr
                JOIN RankedPrices prev 
                    ON curr.scryfall_id = prev.scryfall_id 
                    AND prev.rn = 2
                WHERE curr.rn = 1
            )
            SELECT 
                SUM(CASE WHEN ps.old_price < 2 AND ps.new_price >= 2 THEN 1 ELSE 0 END) as total_growth,
                SUM(CASE WHEN ps.old_price >= 2 AND ps.new_price < 2 THEN 1 ELSE 0 END) as total_fall
            FROM (
                SELECT scryfall_id, finish 
                FROM inventory 
                GROUP BY scryfall_id, finish
                ) i
            JOIN PriceShifts ps ON i.scryfall_id = ps.scryfall_id
            WHERE 
                (ps.old_price < 2 AND ps.new_price >= 2) 
                OR 
                (ps.old_price >= 2 AND ps.new_price < 2)
        ''').fetchone()
        growth = stocks['total_growth'] or 0
        fall = stocks['total_fall'] or 0
        
        # Fetch the requested cards for each trade
        for trade in pending_trades:
            reqs = manager.cursor.execute('''
                SELECT ti.quantity, ti.finish, cd.name, cp.set_code, cp.collector_number
                FROM trade_outbound_items ti
                JOIN card_printings cp ON ti.scryfall_id = cp.scryfall_id
                JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE ti.trade_id = ?
            ''', (trade['trade_id'],)).fetchall()
            
            offers = manager.cursor.execute('''
                SELECT ti.scryfall_id, ti.quantity, ti.finish, cd.name, cp.set_code, cp.collector_number
                FROM trade_inbound_items ti
                LEFT JOIN card_printings cp ON ti.scryfall_id = cp.scryfall_id
                LEFT JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE ti.trade_id = ?
            ''', (trade['trade_id'],)).fetchall()
            
            
            trade['requested_cards'] = [dict(i) for i in reqs]
            trade['offered_cards'] = [dict(i) for i in offers]
            
            
                
        
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
                           change = [growth,fall],
                           active_trades=active_trades,
                           zero_value_count=zero_value_count,
                           unassigned_cards=unassigned_cards,
                           pending_trades=pending_trades,
                           db_size_mb=db_size_mb,
                           trades=trades)

@admin_bp.route('/api/manage_trade', methods=['POST'])
@login_required
def manage_trade():
    data = request.json
    trade_id = data.get('trade_id')
    action = data.get('action') # 'accept' or 'decline'

    if not trade_id or action not in ['accept', 'decline']:
        return jsonify({'success': False, 'error': 'Invalid request parameters.'}), 400

    manager = CardDB()
    
    try:
        if action == 'accept':
            # 1. INBOUND: Add offered cards to your inventory
            inbound_items = manager.cursor.execute('''
                SELECT scryfall_id, finish, quantity 
                FROM trade_inbound_items WHERE trade_id = ?
            ''', (trade_id,)).fetchall()
            
            for item in inbound_items:
                # Insert a new row for each individual copy of the card
                for _ in range(item['quantity']):
                    # Note: You can change location_id to whatever your "Main Binder" ID is.
                    manager.cursor.execute('''
                        INSERT INTO inventory (scryfall_id, finish, condition, location_id, is_tradeable, added)
                        VALUES (?, ?, "NM", 5, 0, ?)
                    ''', (item['scryfall_id'], item['finish'], datetime.datetime.now())),

            # 2. OUTBOUND: Remove requested cards from your inventory
            outbound_items = manager.cursor.execute('''
                SELECT scryfall_id, finish, quantity 
                FROM trade_outbound_items WHERE trade_id = ?
            ''', (trade_id,)).fetchall()
            
            for item in outbound_items:
                # Safely delete exactly 'X' copies of the card that are marked as tradeable
                manager.cursor.execute('''
                    DELETE FROM inventory 
                    WHERE instance_id IN (
                        SELECT instance_id FROM inventory 
                        WHERE scryfall_id = ? AND finish = ? AND is_tradeable = 1
                        LIMIT ?
                    )
                ''', (item['scryfall_id'], item['finish'], item['quantity']))

            # 3. Update the trade status to Completed
            manager.cursor.execute("UPDATE trades SET status = 'Completed' WHERE trade_id = ?", (trade_id,))

        elif action == 'decline':
            # Just update the status, don't move any inventory
            manager.cursor.execute("UPDATE trades SET status = 'Declined' WHERE trade_id = ?", (trade_id,))

        manager.conn.commit()
        return jsonify({'success': True, 'action': action})

    except Exception as e:
        manager.conn.rollback() # If anything fails, revert the entire transaction!
        print(f"Trade Resolution Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        manager.close()

@admin_bp.route('/search_api', methods=['GET'])
def search_api():
     return render_template('search_api.html')
 
@admin_bp.route('/add/wishlist', methods=['GET', 'POST'])
def wishlist_add():
    if current_user.role != 'admin':
        return "Access Denied", 403
    
    manager = CardDB()
    fetcher = ScryfallFetcher.ScryfallFetcher(manager, setting=1)

    query = '''
        SELECT 
        w.wish_id, 
        cd.name, 
        cp.set_code, 
        cp.collector_number,
        w.added, 
        w.finish, 
        ph.price_usd as nonfoil, 
        ph.price_foil as foil
        FROM wishlist w
        JOIN card_printings cp ON w.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        JOIN price_history ph ON ph.scryfall_id = cp.scryfall_id
        WHERE ph.scraped_At = (
            SELECT MAX(scraped_at) 
            FROM price_history 
            WHERE scryfall_id = cp.scryfall_id
        )
        ORDER BY w.added DESC
    '''
    
   
    if request.method == 'POST':            
        sc = request.form["set_code"]
        cn = request.form['collector_number']
        foil = True if request.form.get('is_foil') == "yes" else False
        try:              
            # print(f"DEBUG: {sc}-{cn}:{card_info}")
                        
            card_id = fetcher.fetch_and_add(sc, cn)
            
            if card_id:
                manager.cursor.execute("INSERT INTO wishlist (scryfall_id, finish, added) VALUES (?, ?, ?)", 
                                        (card_id, 
                                        "foil" if foil else "nonfoil", 
                                        datetime.datetime.now()
                                        ))
                manager.commit()
        except Exception as e:
            print(f"Invalid Card Entry: {e}")
        finally:
            # Always ensure the connection is closed before leaving the POST block
            manager.close()
            return redirect(url_for('.wishlist_add'))
        
    # Use manager.conn.execute to fetch the rows
    cards = manager.cursor.execute(query).fetchall()
    
    # Close the connection after we have our data
    manager.close()
    
    return render_template('/wishlist_adder.html', cards=cards,)

@admin_bp.route('/delete_wish/<int:wish_id>', methods=['POST'])
def delete_wish(wish_id):
    manager = CardDB()
    try:
       
        manager.cursor.execute("DELETE FROM wishlist WHERE wish_id = ?", (wish_id,))
        
        manager.commit()
            
    except Exception as e:
        print(f"Error deleting card or cleaning up set: {e}")
    finally:
        manager.close()
    
    return redirect(url_for('.wishlist_add'))
