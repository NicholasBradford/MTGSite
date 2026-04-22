from flask import Blueprint, request, redirect, url_for, render_template, jsonify
from flask_login import login_required
from db.db_manager import CardDB
import sqlite3, ScryfallFetcher, db.db_manager

def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route('/inventory', methods=['GET', 'POST'])
def inventory():
    # 1. Open the connection INSIDE the route so it belongs to this specific thread
    manager = CardDB()
    # manager.create_tables()
    # 2. Pass the manager to the fetcher
    # GET logic: Fetch all cards to display
    query = '''
        SELECT 
            i.scryfall_id,
            i.instance_id,
            i.location_id,     -- Add this so the JS can read it
            i.is_tradeable,      -- Add this so the JS can read it
            cd.name, 
            cp.image_url, 
            cp.set_code, 
            cp.collector_number,
            i.finish,
            COUNT(*) as qty 
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        GROUP BY i.scryfall_id, i.finish
        ORDER BY cp.set_code, cd.name, cp.collector_number
    '''
    
    query2 = '''SELECT
        location_id as id, name
        FROM locations
        ORDER BY name'''
    
    cards = manager.cursor.execute(query).fetchall()
    locs = manager.cursor.execute(query2).fetchall()
    # Close the connection after we have our data
    manager.close()
    
    loc_list = [dict(row) for row in locs]
    
    return render_template('inventory.html', cards=cards, locations=loc_list, view_mode='inventory')

@inventory_bp.route('/edit_instance/<int:instance_id>', methods=['POST'])
@login_required
def edit_instance(instance_id):
    manager = CardDB()
    # Get values from the fetch request
    new_loc = request.form.get('location_id')
    new_trade = request.form.get('is_tradeable')

    # Update Location if provided
    if new_loc is not None:
        manager.cursor.execute(
            "UPDATE inventory SET location_id = ? WHERE instance_id = ?",
            (new_loc, instance_id)
        )

    # Update Trade Status if provided
    if new_trade is not None:
        # Convert JS 'true'/'false' or '1'/'0' to integer 1 or 0
        trade_val = 1 if new_trade in ['1', 'true'] else 0
        manager.cursor.execute(
            "UPDATE inventory SET is_tradeable = ? WHERE instance_id = ?",
            (trade_val, instance_id)
        )

    manager.commit()
    manager.close()
    return {"status": "success"}, 200

@inventory_bp.route('/get_instances/<scryfall_id>/<finish>')
@login_required
def get_instances(scryfall_id, finish):
    manager = CardDB()
    
    query = '''
        SELECT i.scryfall_id, i.instance_id, i.location_id, i.is_tradeable, l.name as location_name
        FROM inventory i
        JOIN locations l ON i.location_id = l.location_id
        WHERE i.scryfall_id = ? AND i.finish = ?
    '''
    
    # Ensure your cursor is configured to return row objects that can be converted to dicts
    # e.g., if using sqlite3: manager.connection.row_factory = sqlite3.Row
    rows = manager.cursor.execute(query, (scryfall_id, finish)).fetchall()
    manager.close()
    
    # Convert rows to a list of dictionaries
    instances_list = [dict(row) for row in rows]
    
    # Return using jsonify to ensure correct headers for fetch()
    return jsonify({"instances": instances_list})