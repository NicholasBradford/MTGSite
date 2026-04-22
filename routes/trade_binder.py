from flask import Blueprint, request, redirect, url_for, render_template
from db.db_manager import CardDB
import sqlite3, ScryfallFetcher, db.db_manager

def get_db_connection():
    # Points to your db folder
    conn = sqlite3.connect('db/mtg_inventory.db')
    conn.row_factory = sqlite3.Row
    return conn

trade_bp = Blueprint('trade_binder', __name__)

@trade_bp.route('/trade_binder', methods=['GET', 'POST'])
def trade():
    # 1. Open the connection INSIDE the route so it belongs to this specific thread
    manager = CardDB()
    
    # 2. Pass the manager to the fetcher
    # GET logic: Fetch all cards to display
    query = '''
        SELECT 
            cd.name, 
            cd.cmc,
            cd.color_identity,
            cp.image_url, 
            cp.set_code, 
            cp.collector_number,
            i.finish,
            i.is_tradeable,
            COUNT(*) as qty  -- This creates the quantity number
        FROM inventory i 
        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
        WHERE i.is_tradeable = 1
        GROUP BY i.scryfall_id, i.finish  -- Grouping hides the duplicates
        ORDER BY cd.name ASC;
    '''
    cards = manager.cursor.execute(query).fetchall()
    
    # Close the connection after we have our data
    manager.close()
    
    return render_template('trade_binder.html', cards=cards, view_mode = "trades")