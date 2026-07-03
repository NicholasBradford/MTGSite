import os, datetime, time, requests, json
from flask import Blueprint, Response, request, flash, redirect, url_for, render_template, abort, jsonify
from functools import wraps
import ScryfallFetcher
from flask_login import login_required, current_user
from db.db_manager import get_db
try:
    from services.tcgcsv_prices import get_local_snapshot_metadata, local_snapshot_exists
except ImportError:
    # Keep admin dashboard functional on branches where snapshot helpers are not present.
    def get_local_snapshot_metadata():
        return {}

    def local_snapshot_exists():
        return False

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
@admin_required
def manage_locations():
    manager = get_db()
    
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

def _fetch_scalar(manager, query, params=(), default=0):
    # Return a single scalar value without letting optional dashboard queries break the page.
    try:
        row = manager.cursor.execute(query, params).fetchone()
        if row is None:
            return default
        value = row[0]
        return default if value is None else value
    except Exception:
        return default


def _fetch_dict(manager, query, params=()):
    # Return one sqlite row as a dict, or an empty dict if the query cannot run.
    try:
        row = manager.cursor.execute(query, params).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def _fetch_dicts(manager, query, params=()):
    # Return sqlite rows as dicts, or an empty list if the query cannot run.
    try:
        return [dict(row) for row in manager.cursor.execute(query, params).fetchall()]
    except Exception:
        return []


def _file_size_mb(path):
    if not path:
        return "Unknown"
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 2)
    except OSError:
        return "Unknown"


def _add_attention(attention_items, severity, label, count=None, detail=None, href=None):
    if count is None or count:
        attention_items.append({
            "severity": severity,
            "label": label,
            "count": count,
            "detail": detail,
            "href": href,
        })


@admin_bp.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    manager = get_db()
    growth = 0
    fall = 0
    total_cards = 0
    unique_cards = 0
    active_trades = []
    trades = []
    pending_trades = []
    zero_value_count = 0
    unassigned_cards = 0
    db_size_mb = "Unknown"

    try:
        # --- 1. EXISTING STATS (preserved) ---
        total_cards = manager.cursor.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        unique_cards = manager.cursor.execute("SELECT COUNT(DISTINCT scryfall_id) FROM inventory").fetchone()[0]

        # --- 2. EXISTING DELICATE TRADE LOGIC (preserved) ---
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

        # --- 3. SITE HEALTH METRICS ---
        normalized_finish_sql = "LOWER(REPLACE(COALESCE(i.finish, ''), '_', ' '))"
        foil_like_sql = f"{normalized_finish_sql} IN ('foil', 'etched', 'rainbow foil')"
        nonfoil_like_sql = f"{normalized_finish_sql} IN ('nonfoil', 'normal', '')"

        zero_value_count = _fetch_scalar(manager, f'''
            SELECT COUNT(i.instance_id)
            FROM inventory i
            LEFT JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
            WHERE ({foil_like_sql} AND COALESCE(cp.current_price_foil, 0) = 0)
               OR ({nonfoil_like_sql} AND COALESCE(cp.current_price, 0) = 0)
               OR NOT ({foil_like_sql} OR {nonfoil_like_sql})
        ''')

        unassigned_cards = _fetch_scalar(manager, '''
            SELECT COUNT(*)
            FROM inventory
            WHERE location_id IS NULL OR location_id = ''
        ''')

        collection_health = {
            "total_cards": total_cards,
            "unique_cards": unique_cards,
            "tradeable_cards": _fetch_scalar(manager, "SELECT COUNT(*) FROM inventory WHERE is_tradeable = 1"),
            "surplus_cards": _fetch_scalar(manager, "SELECT COUNT(*) FROM inventory WHERE is_surplus = 1"),
            "cards_in_decks": _fetch_scalar(manager, "SELECT COUNT(*) FROM inventory WHERE in_deck = 1"),
            "wishlist_cards": _fetch_scalar(manager, "SELECT COUNT(*) FROM wishlist"),
            "decklists": _fetch_scalar(manager, "SELECT COUNT(*) FROM edh_decks"),
        }

        finish_breakdown = _fetch_dicts(manager, '''
            SELECT
                COALESCE(NULLIF(TRIM(finish), ''), 'blank') AS finish,
                COUNT(*) AS count
            FROM inventory
            GROUP BY COALESCE(NULLIF(TRIM(finish), ''), 'blank')
            ORDER BY count DESC, finish ASC
        ''')

        data_integrity = {
            "missing_value_count": zero_value_count,
            "unassigned_cards": unassigned_cards,
            "orphan_inventory": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM inventory i
                LEFT JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                WHERE cp.scryfall_id IS NULL
            '''),
            "orphan_printings": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings cp
                LEFT JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE cd.oracle_id IS NULL
            '''),
            "orphan_wishlist": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM wishlist w
                LEFT JOIN card_printings cp ON w.scryfall_id = cp.scryfall_id
                WHERE cp.scryfall_id IS NULL
            '''),
            "orphan_deck_cards": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM edh_deck_cards edc
                LEFT JOIN card_printings cp ON edc.scryfall_id = cp.scryfall_id
                WHERE cp.scryfall_id IS NULL
            '''),
            "unexpected_finish": _fetch_scalar(manager, f'''
                SELECT COUNT(*)
                FROM inventory i
                WHERE NOT ({foil_like_sql} OR {nonfoil_like_sql})
            '''),
            "cards_without_images": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings
                WHERE image_url IS NULL OR TRIM(image_url) = ''
            '''),
        }

        price_health = _fetch_dict(manager, '''
            SELECT
                COUNT(*) AS price_history_rows,
                COUNT(DISTINCT scryfall_id) AS cards_with_history,
                COUNT(DISTINCT scraped_at) AS snapshot_dates,
                MIN(scraped_at) AS oldest_snapshot,
                MAX(scraped_at) AS newest_snapshot
            FROM price_history
        ''')
        price_health.update({
            "all_printings_missing_nonfoil_price": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings
                WHERE current_price IS NULL OR current_price = 0
            '''),
            "all_printings_missing_foil_price": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings
                WHERE current_price_foil IS NULL OR current_price_foil = 0
            '''),

            "owned_nonfoil_missing_current_price": _fetch_scalar(manager, f'''
                SELECT COUNT(DISTINCT i.scryfall_id)
                FROM inventory i
                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                WHERE ({nonfoil_like_sql})
                AND COALESCE(cp.current_price, 0) = 0
            '''),

            "owned_foil_like_missing_current_price": _fetch_scalar(manager, f'''
                SELECT COUNT(DISTINCT i.scryfall_id)
                FROM inventory i
                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                WHERE ({foil_like_sql})
                AND COALESCE(cp.current_price_foil, 0) = 0
            '''),

            "owned_printings_with_any_current_price": _fetch_scalar(manager, f'''
                SELECT COUNT(DISTINCT i.scryfall_id)
                FROM inventory i
                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                WHERE
                    (({nonfoil_like_sql}) AND COALESCE(cp.current_price, 0) > 0)
                    OR
                    (({foil_like_sql}) AND COALESCE(cp.current_price_foil, 0) > 0)
            '''),
            "owned_cards_with_two_snapshots": _fetch_scalar(manager, '''
                WITH SnapshotCounts AS (
                    SELECT scryfall_id, COUNT(DISTINCT scraped_at) AS snapshots
                    FROM price_history
                    GROUP BY scryfall_id
                )
                SELECT COUNT(DISTINCT i.scryfall_id)
                FROM inventory i
                JOIN SnapshotCounts sc ON i.scryfall_id = sc.scryfall_id
                WHERE sc.snapshots >= 2
            '''),
            "owned_cards_missing_two_snapshots": _fetch_scalar(manager, '''
                WITH SnapshotCounts AS (
                    SELECT scryfall_id, COUNT(DISTINCT scraped_at) AS snapshots
                    FROM price_history
                    GROUP BY scryfall_id
                )
                SELECT COUNT(DISTINCT i.scryfall_id)
                FROM inventory i
                LEFT JOIN SnapshotCounts sc ON i.scryfall_id = sc.scryfall_id
                WHERE COALESCE(sc.snapshots, 0) < 2
            '''),
            "tcgplayer_id_missing": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings
                WHERE tcgplayer_id IS NULL AND COALESCE(tcgplayer_id_missing, 0) = 0
            '''),
            "tcgplayer_marked_missing": _fetch_scalar(manager, '''
                SELECT COUNT(*)
                FROM card_printings
                WHERE COALESCE(tcgplayer_id_missing, 0) = 1
            '''),
            "manual_price_overrides": _fetch_scalar(manager, "SELECT COUNT(*) FROM tcgplayer_price_overrides"),
        })

        source_breakdown = _fetch_dicts(manager, '''
            SELECT COALESCE(source, 'unknown') AS source, COUNT(*) AS count
            FROM price_history
            GROUP BY COALESCE(source, 'unknown')
            ORDER BY count DESC, source ASC
        ''')

        sync_health = {
            "latest": _fetch_dict(manager, '''
                SELECT timestamp, task_name, cards_updated, status, message
                FROM update_log
                ORDER BY timestamp DESC
                LIMIT 1
            '''),
            "last_success": _fetch_dict(manager, '''
                SELECT timestamp, task_name, cards_updated, status, message
                FROM update_log
                WHERE LOWER(COALESCE(status, '')) IN ('success', 'successful', 'ok', 'completed')
                ORDER BY timestamp DESC
                LIMIT 1
            '''),
            "last_error": _fetch_dict(manager, '''
                SELECT timestamp, task_name, cards_updated, status, message
                FROM update_log
                WHERE LOWER(COALESCE(status, '')) NOT IN ('success', 'successful', 'ok', 'completed')
                ORDER BY timestamp DESC
                LIMIT 1
            '''),
            "recent_logs": _fetch_dicts(manager, '''
                SELECT timestamp, task_name, cards_updated, status, message
                FROM update_log
                ORDER BY timestamp DESC
                LIMIT 8
            '''),
        }

        trade_health = {
            "pending": len(pending_trades),
            "completed": _fetch_scalar(manager, "SELECT COUNT(*) FROM trades WHERE status = 'Completed'"),
            "declined": _fetch_scalar(manager, "SELECT COUNT(*) FROM trades WHERE status = 'Declined'"),
            "empty_outbound": _fetch_scalar(manager, '''
                SELECT COUNT(*) FROM (
                    SELECT t.trade_id
                    FROM trades t
                    LEFT JOIN trade_outbound_items toi ON t.trade_id = toi.trade_id
                    WHERE t.status = 'Pending'
                    GROUP BY t.trade_id
                    HAVING COUNT(toi.item_id) = 0
                )
            '''),
            "empty_inbound": _fetch_scalar(manager, '''
                SELECT COUNT(*) FROM (
                    SELECT t.trade_id
                    FROM trades t
                    LEFT JOIN trade_inbound_items tii ON t.trade_id = tii.trade_id
                    WHERE t.status = 'Pending'
                    GROUP BY t.trade_id
                    HAVING COUNT(tii.id) = 0
                )
            '''),
        }

        db_path = os.environ.get('DB_PATH')
        db_size_mb = _file_size_mb(db_path)
        infrastructure_health = {
            "database_status": "Connected",
            "db_path_known": bool(db_path),
            "db_size_mb": db_size_mb,
            "wal_size_mb": _file_size_mb(f"{db_path}-wal") if db_path else "Unknown",
            "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "debug_mode": os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes', 'on'),
        }

        latest_status = (sync_health.get("latest") or {}).get("status", "")
        latest_failed = bool(sync_health.get("latest")) and latest_status.lower() not in ('success', 'successful', 'ok', 'completed')

        attention_items = []
        _add_attention(attention_items, "critical", "Latest sync failed", 1 if latest_failed else 0, (sync_health.get("latest") or {}).get("message"))
        _add_attention(attention_items, "warning", "Owned cards missing two price snapshots", price_health.get("owned_cards_missing_two_snapshots"), "Market movement needs at least two dated snapshots.", "/market/dashboard")
        _add_attention(attention_items, "warning", "Cards missing usable values", data_integrity["missing_value_count"], "Includes missing prices and unexpected finish values.", "/inventory?q=usd%3D0")
        _add_attention(attention_items, "warning", "Unassigned inventory rows", data_integrity["unassigned_cards"], "Cards with no valid location.", "/inventory?location=unassigned")
        _add_attention(attention_items, "critical", "Inventory rows missing card_printings", data_integrity["orphan_inventory"], "These can break joins and card displays.")
        _add_attention(attention_items, "critical", "Printings missing card_definitions", data_integrity["orphan_printings"], "These can break names, type lines, and collection pages.")
        _add_attention(attention_items, "warning", "Unexpected finish values", data_integrity["unexpected_finish"], "Review finish normalization before pricing logic relies on it.")
        _add_attention(attention_items, "warning", "Card printings without images", data_integrity["cards_without_images"], "May show broken or blank card art.")
        _add_attention(attention_items, "info", "Pending trade requests", len(pending_trades), "Trade queue needs review.")

        snapshot_metadata = get_local_snapshot_metadata()
        snapshot_present = local_snapshot_exists()
        snapshot_last_updated = snapshot_metadata.get("last_updated") or "Never"
        snapshot_captured_at = snapshot_metadata.get("captured_at") or "Unknown"
        snapshot_age_days = None
        if snapshot_metadata.get("captured_at"):
            try:
                captured = datetime.datetime.fromisoformat(snapshot_metadata["captured_at"])
                snapshot_age_days = (datetime.datetime.utcnow() - captured).days
            except Exception:
                pass
        tcgcsv_snapshot_health = {
            "exists": snapshot_present,
            "last_updated": snapshot_last_updated,
            "captured_at": snapshot_captured_at,
            "age_days": snapshot_age_days,
        }

        if not snapshot_present:
            _add_attention(
                attention_items, "critical",
                "No local TCGCSV price snapshot", 1,
                "Run scripts/refresh_tcgcsv_snapshot.py to enable price sync.",
            )
        elif snapshot_age_days is not None and snapshot_age_days > 1:
            _add_attention(
                attention_items, "warning",
                "Local TCGCSV snapshot is stale", snapshot_age_days,
                f"Last captured {snapshot_age_days} day(s) ago.",
            )

    except Exception as e:
        tcgcsv_snapshot_health = {"exists": False, "last_updated": "Unknown", "captured_at": "Unknown", "age_days": None}
        flash(f"Error loading dashboard data: {e}", "error")
        collection_health = {}
        finish_breakdown = []
        data_integrity = {}
        price_health = {}
        source_breakdown = []
        sync_health = {"latest": {}, "last_success": {}, "last_error": {}, "recent_logs": []}
        trade_health = {}
        infrastructure_health = {"database_status": "Error", "db_size_mb": db_size_mb}
        attention_items = [{"severity": "critical", "label": "Dashboard failed to load fully", "count": 1, "detail": str(e), "href": None}]
    finally:
        manager.close()

    return render_template('admin_dashboard.html',
                           total_cards=total_cards,
                           unique_cards=unique_cards,
                           change=[growth, fall],
                           active_trades=active_trades,
                           zero_value_count=zero_value_count,
                           unassigned_cards=unassigned_cards,
                           pending_trades=pending_trades,
                           db_size_mb=db_size_mb,
                           trades=trades,
                           collection_health=collection_health,
                           finish_breakdown=finish_breakdown,
                           data_integrity=data_integrity,
                           price_health=price_health,
                           source_breakdown=source_breakdown,
                           sync_health=sync_health,
                           trade_health=trade_health,
                           infrastructure_health=infrastructure_health,
                           tcgcsv_snapshot_health=tcgcsv_snapshot_health,
                           attention_items=attention_items)

@admin_bp.route('/api/manage_trade', methods=['POST'])
@login_required
@admin_required
def manage_trade():
    data = request.json
    trade_id = data.get('trade_id')
    action = data.get('action') # 'accept' or 'decline'

    if not trade_id or action not in ['accept', 'decline']:
        return jsonify({'success': False, 'error': 'Invalid request parameters.'}), 400

    manager = get_db()
    
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
@login_required
@admin_required
def wishlist_add():
    manager = get_db()
    fetcher = ScryfallFetcher.ScryfallFetcher(manager, setting=1)

    query = '''
        SELECT 
            w.wish_id, 
            cd.name, 
            cp.set_code, 
            cp.collector_number,
            w.added, 
            w.finish,

            ph.price_usd AS nonfoil, 
            ph.price_foil AS foil,

            cp.current_price AS current_nonfoil,
            cp.current_price_foil AS current_foil

        FROM wishlist w
        JOIN card_printings cp
            ON w.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id

        LEFT JOIN price_history ph
            ON ph.scryfall_id = cp.scryfall_id
        AND ph.scraped_at = (
                SELECT MAX(ph2.scraped_at)
                FROM price_history ph2
                WHERE ph2.scryfall_id = cp.scryfall_id
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
@login_required
@admin_required
def delete_wish(wish_id):
    manager = get_db()
    try:
       
        manager.cursor.execute("DELETE FROM wishlist WHERE wish_id = ?", (wish_id,))
        
        manager.commit()
            
    except Exception as e:
        print(f"Error deleting card or cleaning up set: {e}")
    finally:
        manager.close()
    
    return redirect(url_for('.wishlist_add'))
