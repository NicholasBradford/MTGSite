import os
import tempfile
import pytest
import json
from flask import Flask

# Import your database manager and blueprint 
# (Adjust these import paths to match your actual project structure)
from db.db_manager import CardDB
from routes.trade_binder import trade_bp 

@pytest.fixture
def app():
    # 1. Set up a temporary database file for the test
    db_fd, db_path = tempfile.mkstemp()
    
    # Override the environment variable so CardDB() uses the temp file instead of your real DB
    os.environ['DB_PATH'] = db_path
    
    # 2. Initialize the necessary tables in the temporary database
    manager = CardDB()
    
    # We need a dummy user table since trades reference user_id
    manager.cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            username TEXT
        )
    ''')
    manager.cursor.execute("INSERT INTO users (user_id, username) VALUES (1, 'TestUser')")
    
    # Create the trade tables
    manager.cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            trade_id TEXT PRIMARY KEY,
            user_id INTEGER,
            status TEXT DEFAULT 'Pending'
        )
    ''')
    manager.cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_outbound_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT,
            scryfall_id TEXT,
            finish TEXT,
            quantity INTEGER
        )
    ''')
    manager.commit()
    manager.close()

    # 3. Create a barebones Flask app and register your trade route
    app = Flask(__name__)
    app.register_blueprint(trade_bp)
    
    yield app

    # 4. Clean up: Close and delete the temporary database file after the test runs
    os.close(db_fd)
    os.unlink(db_path)

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()


# --- THE ACTUAL TESTS ---

def test_submit_trade_success(client):
    """Test that submitting a valid cart successfully creates a trade request."""
    
    # Simulate the JSON payload that your JavaScript cart will send
    payload = {
        "items": [
            {"scryfall_id": "uuid-for-lightning-bolt", "finish": "nonfoil", "qty": 2, "name": "Lightning Bolt"},
            {"scryfall_id": "uuid-for-black-lotus", "finish": "foil", "qty": 1, "name": "Black Lotus"}
        ]
    }
    
    # Send the mock POST request to the API
    response = client.post(
        '/api/submit_trade',
        data=json.dumps(payload),
        content_type='application/json'
    )
    
    # 1. Assert HTTP Status Code is OK
    assert response.status_code == 200
    
    # 2. Assert the JSON response contains the success flag and the generated ID
    data = response.get_json()
    assert data['success'] is True
    assert 'trade_id' in data
    assert data['trade_id'].startswith("TRD-") # Checks if our ID generator worked
    
    # 3. Assert the data actually made it into the SQLite database!
    manager = CardDB()
    trades = manager.cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (data['trade_id'],)).fetchall()
    items = manager.cursor.execute("SELECT * FROM trade_outbound_items WHERE trade_id = ?", (data['trade_id'],)).fetchall()
    manager.close()
    
    # Did it create 1 trade record?
    assert len(trades) == 1
    assert trades[0]['status'] == 'Pending'
    
    # Did it insert the 2 requested cards?
    assert len(items) == 2
    assert items[0]['scryfall_id'] == "uuid-for-lightning-bolt"
    assert items[0]['quantity'] == 2

def test_submit_empty_trade_returns_error(client):
    """Test that submitting an empty cart returns a 400 error."""
    
    response = client.post(
        '/api/submit_trade',
        data=json.dumps({"items": []}),
        content_type='application/json'
    )
    
    # Assert it kicks back a Bad Request (400)
    assert response.status_code == 400
    
    data = response.get_json()
    assert data['success'] is False
    assert data['error'] == 'Cart is empty'