import sqlite3, os 
from dotenv import load_dotenv

load_dotenv()

class CardDB:
    def __init__(self, db_path=os.environ.get('DB_PATH')):
        if db_path is None:
            db_path = os.environ.get('DB_PATH')
            print(f"db_path is:{db_path}")
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row  # Allows accessing columns by name
        self.cursor = self.conn.cursor()
    
    def wipe_db(self):
        """Safely closes connection, deletes the file, and restarts."""
        if self.conn:
            self.conn.close()
            # CRITICAL: Delete the references to ensure the file lock is released
            del self.cursor
            del self.conn
            self.conn = None
            self.cursor = None
        
        if os.path.exists(self.db_path):
            try:
                # On Windows, sometimes the OS needs a split second to 
                # acknowledge the handle closure from 'del'
                os.remove(self.db_path)
                print(f"Database {self.db_path} deleted.")
            except PermissionError:
                import time
                time.sleep(0.2) # Increased slightly for Windows stability
                os.remove(self.db_path)
            
    # Do NOT call self.__init__ here. 
    # Let the nuke() method handle the restart.
    
    def create_tables(self):
        # Enable foreign keys in SQLite
        self.cursor.execute("PRAGMA foreign_keys = ON;")

        # 1. card_definitions (The "Library")
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS card_definitions (
                oracle_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mana_cost TEXT,
                cmc REAL,
                type_line TEXT,
                oracle_text TEXT,
                color_identity TEXT
            )
        ''')

        # 2. card_printings (The "Catalog")
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS card_printings (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT,
                set_code TEXT,
                collector_number TEXT,
                rarity TEXT,
                image_url TEXT,
                flavor_text TEXT,
                current_price REAL,
                current_price_foil REAL,
                last_updated DATE DEFAULT (CURRENT_DATE),
                FOREIGN KEY (oracle_id) REFERENCES card_definitions (oracle_id)
            )
        ''')

        # 3. inventory (The "Collection")
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                finish TEXT,
                condition TEXT,
                is_tradeable INTEGER DEFAULT 0,
                purchase_price REAL,
                location_id INTEGER ,
                is_surplus BOOL,
                added DATETIME,
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id),
                FOREIGN KEY (location_id) REFERENCES locations (location_id)
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,          -- e.g., 'Trade Binder', 'Modern Deck', 'Storage Box A'
                description TEXT             -- e.g., 'Top shelf of the closet'
            )
        ''')

        # 4. price_history (The "Ticker")
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                price_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                price_usd REAL,
                price_foil REAL,
                scraped_at DATE DEFAULT (CURRENT_DATE),
                UNIQUE(scryfall_id, scraped_at) ON CONFLICT REPLACE
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS wishlist (
                wish_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                priority INTEGER DEFAULT 1, -- 1-5 scale
                notes TEXT,
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS update_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                task_name TEXT,              -- e.g., 'Midnight Price Sync'
                cards_updated INTEGER,
                status TEXT,                 -- 'Success' or 'Error'
                message TEXT                 -- e.g., 'Updated 450 prices in 12s'
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS update_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                task_name TEXT,              -- e.g., 'Midnight Price Sync'
                cards_updated INTEGER,
                status TEXT,                 -- 'Success' or 'Error'
                message TEXT                 -- e.g., 'Updated 450 prices in 12s'
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT DEFAULT CURRENT_TIMESTAMP,
                password_hash TEXT,     
                role TEXT
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS sets (
                set_code TEXT PRIMARY KEY,
                set_name TEXT NOT NULL,
                set_type TEXT,
                standard_legal BOOL,
                released_at DATE,
                icon_svg_uri TEXT
            );
        ''')
        
        self.cursor.execute('''CREATE INDEX IF NOT EXISTS idx_type_line ON card_definitions(type_line);''')   
        self.initialize_locations()              
        self.commit()
        
    def initialize_locations(self):
    # Add a default location so cards have a place to live
        self.cursor.execute('''
            INSERT OR IGNORE INTO locations (location_id, name, description)
            VALUES (1, 'Unsorted Box', 'Cards waiting to be filed')
        ''')
        self.conn.commit()
        
    def commit(self):
        self.conn.commit()
    
    def close(self):
        """Close the connection so the file can be managed."""
        if self.conn:
            self.conn.close()
            
    def nuke(self):
        self.wipe_db()
        # Now that the file is gone, re-establish the connection and tables
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        
        self.create_tables()
        self.initialize_locations()
        self.commit()