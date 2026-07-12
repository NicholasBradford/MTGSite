import sqlite3, os, random, traceback
import csv
from datetime import datetime
from flask import g
from dotenv import load_dotenv

load_dotenv()

TCGCSV_HISTORY_DIR = os.environ.get("TCGCSV_HISTORY_DIR", "tcg_history")
TCGCSV_HISTORY_FILE_PREFIX = "prices_category_1_"
TCGCSV_HISTORY_FILE_SUFFIX = ".csv"
TCGCSV_SNAPSHOT_DIR = os.path.join("var", "data", "tcgcsv")
TCGCSV_LOCAL_PRICE_SNAPSHOT = os.path.join(TCGCSV_SNAPSHOT_DIR, "daily_prices_latest.csv")

class CardDB:
    def __init__(self, db_path=None):
        self.trace_id = random.randint(1000, 9999)

        if db_path is None:
            db_path = os.environ.get('DB_PATH')

        self.db_path = db_path

        self.conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        
    def log_update(self, task_name, cards_updated=0, status="Success", message=""):
        self.cursor.execute("""
            INSERT INTO update_log (task_name, cards_updated, status, message)
            VALUES (?, ?, ?, ?)
        """, (task_name, cards_updated, status, message))
        self.commit()

    @staticmethod
    def local_price_index_path(snapshot_path):
        return f"{snapshot_path}.idx.sqlite"

    @staticmethod
    def _parse_tcgcsv_history_file_date(path):
        filename = os.path.basename(path)

        if not filename.startswith(TCGCSV_HISTORY_FILE_PREFIX):
            return None

        if not filename.endswith(TCGCSV_HISTORY_FILE_SUFFIX):
            return None

        date_part = filename[
            len(TCGCSV_HISTORY_FILE_PREFIX):-len(TCGCSV_HISTORY_FILE_SUFFIX)
        ]

        if "_" in date_part:
            return None

        try:
            return datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            return None

    @classmethod
    def resolve_tcgcsv_snapshot_path(cls):
        if os.path.isdir(TCGCSV_HISTORY_DIR):
            candidates = []

            for filename in os.listdir(TCGCSV_HISTORY_DIR):
                path = os.path.join(TCGCSV_HISTORY_DIR, filename)
                if not os.path.isfile(path):
                    continue

                if os.path.getsize(path) <= 0:
                    continue

                file_date = cls._parse_tcgcsv_history_file_date(path)
                if file_date is None:
                    continue

                candidates.append((file_date, os.path.getmtime(path), path))

            if candidates:
                candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
                return candidates[0][2]

        if os.path.exists(TCGCSV_LOCAL_PRICE_SNAPSHOT):
            return TCGCSV_LOCAL_PRICE_SNAPSHOT

        return None

    @staticmethod
    def _first_present(row, *names):
        for name in names:
            value = row.get(name)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _coerce_price(value):
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip().replace("$", "").replace(",", "")
            if value == "" or value.lower() in {"none", "null", "nan"}:
                return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_price_snapshot_row(cls, row):
        product_id = cls._first_present(row, "product_id", "productId")
        group_id = cls._first_present(row, "group_id", "groupId")
        subtype_name = cls._first_present(row, "sub_type_name", "subTypeName") or ""

        if not product_id or not group_id:
            return None

        try:
            product_id = int(product_id)
        except (TypeError, ValueError):
            return None

        return {
            "product_id": product_id,
            "group_id": str(group_id),
            "subtype_name": str(subtype_name),
            "market_price": cls._coerce_price(cls._first_present(row, "market_price", "marketPrice")),
            "mid_price": cls._coerce_price(cls._first_present(row, "mid_price", "midPrice")),
            "low_price": cls._coerce_price(cls._first_present(row, "low_price", "lowPrice")),
            "high_price": cls._coerce_price(cls._first_present(row, "high_price", "highPrice")),
            "direct_low_price": cls._coerce_price(cls._first_present(row, "direct_low_price", "directLowPrice")),
        }

    @classmethod
    def ensure_local_price_sidecar_index(cls, snapshot_path=None, force_rebuild=False):
        snapshot_path = snapshot_path or cls.resolve_tcgcsv_snapshot_path()
        if not snapshot_path or not os.path.exists(snapshot_path):
            return None

        snapshot_mtime = float(os.path.getmtime(snapshot_path))
        snapshot_size = int(os.path.getsize(snapshot_path))
        index_path = cls.local_price_index_path(snapshot_path)

        if not force_rebuild and os.path.exists(index_path):
            existing_index = cls(db_path=index_path)
            try:
                existing_index.cursor.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS index_meta (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        snapshot_mtime REAL NOT NULL,
                        snapshot_size INTEGER NOT NULL,
                        built_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                existing_index.commit()

                meta = existing_index.cursor.execute(
                    "SELECT snapshot_mtime, snapshot_size FROM index_meta WHERE id = 1"
                ).fetchone()

                if (
                    meta
                    and float(meta["snapshot_mtime"]) == snapshot_mtime
                    and int(meta["snapshot_size"]) == snapshot_size
                ):
                    return index_path
            finally:
                existing_index.close()

        temp_index_path = f"{index_path}.tmp"
        if os.path.exists(temp_index_path):
            os.remove(temp_index_path)

        index_db = cls(db_path=temp_index_path)
        try:
            index_db.cursor.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS index_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    snapshot_mtime REAL NOT NULL,
                    snapshot_size INTEGER NOT NULL,
                    built_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS price_rows (
                    product_id INTEGER,
                    group_id TEXT,
                    subtype_name TEXT,
                    market_price REAL,
                    mid_price REAL,
                    low_price REAL,
                    high_price REAL,
                    direct_low_price REAL
                );
                CREATE INDEX IF NOT EXISTS idx_product_id ON price_rows(product_id);
                CREATE INDEX IF NOT EXISTS idx_group_id ON price_rows(group_id);
                CREATE INDEX IF NOT EXISTS idx_group_subtype ON price_rows(group_id, subtype_name);
                """
            )

            rows_to_insert = []

            with open(snapshot_path, "r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw_row in reader:
                    normalized = cls._normalize_price_snapshot_row(raw_row)
                    if normalized is None:
                        continue

                    rows_to_insert.append((
                        normalized["product_id"],
                        normalized["group_id"],
                        normalized["subtype_name"],
                        normalized["market_price"],
                        normalized["mid_price"],
                        normalized["low_price"],
                        normalized["high_price"],
                        normalized["direct_low_price"],
                    ))

                    if len(rows_to_insert) >= 5000:
                        index_db.cursor.executemany(
                            """
                            INSERT INTO price_rows (
                                product_id,
                                group_id,
                                subtype_name,
                                market_price,
                                mid_price,
                                low_price,
                                high_price,
                                direct_low_price
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            rows_to_insert,
                        )
                        rows_to_insert.clear()

            if rows_to_insert:
                index_db.cursor.executemany(
                    """
                    INSERT INTO price_rows (
                        product_id,
                        group_id,
                        subtype_name,
                        market_price,
                        mid_price,
                        low_price,
                        high_price,
                        direct_low_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )

            index_db.cursor.execute("DELETE FROM index_meta WHERE id = 1")
            index_db.cursor.execute(
                """
                INSERT INTO index_meta (id, snapshot_mtime, snapshot_size)
                VALUES (1, ?, ?)
                """,
                (snapshot_mtime, snapshot_size),
            )
            index_db.commit()
        finally:
            index_db.close()

        os.replace(temp_index_path, index_path)
        return index_path
    
    def wipe_db(self):
        """Safely closes connection, deletes the file, and restarts."""
        if self.conn:
            self.conn.close()
            del self.cursor
            del self.conn
            self.conn = None
            self.cursor = None
        
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"Database {self.db_path} deleted.")
            except PermissionError:
                import time
                time.sleep(0.2) 
                os.remove(self.db_path)
            
    
    def create_tables(self, bootstrap_local_price_index=None):
        # Enable foreign keys in SQLite
        self.cursor.execute("PRAGMA foreign_keys = ON;")
        self.cursor.execute('PRAGMA journal_mode=WAL;')

        # 1. card_definitions (The "Library")
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS card_definitions (
                oracle_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mana_cost TEXT,
                cmc REAL,
                type_line TEXT,
                oracle_text TEXT,
                color TEXT,
                color_identity TEXT
            )
        ''')
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

            tcgplayer_id INTEGER,
            tcgplayer_etched_id INTEGER,
            tcgcsv_group_id INTEGER,
            tcgcsv_last_price_sync TEXT,
            tcgplayer_id_missing INTEGER DEFAULT 0,

            FOREIGN KEY (oracle_id) REFERENCES card_definitions (oracle_id)
        );
        ''')

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
                in_deck BOOL,
                added DATETIME,
                deck_id INTEGER,
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id),
                FOREIGN KEY (location_id) REFERENCES locations (location_id),
                FOREIGN KEY (deck_id) REFERENCES edh_decks (deck_id)
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,          -- e.g., 'Trade Binder', 'Modern Deck', 'Storage Box A'
                description TEXT             -- e.g., 'Top shelf of the closet'
            )
        ''')

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                price_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                price_usd REAL,
                price_foil REAL,
                scraped_at DATE DEFAULT (CURRENT_DATE),
                source TEXT DEFAULT 'scryfall',
                UNIQUE(scryfall_id, scraped_at) ON CONFLICT REPLACE,
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS wishlist (
                wish_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                finish TEXT,
                priority INTEGER DEFAULT 1,
                added DATETIME,
                notes TEXT,
                non_specific INTEGER DEFAULT 0,

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
        
        self.cursor.execute(''' 
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    status TEXT DEFAULT 'Pending',
                    incoming TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );
                ''')
        
        self.cursor.execute(''' 
                CREATE TABLE IF NOT EXISTS trade_outbound_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT,
                    scryfall_id TEXT,
                    finish TEXT,
                    quantity INTEGER,
                    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
                );
                ''')
        self.cursor.execute(''' 
                CREATE TABLE IF NOT EXISTS trade_inbound_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                scryfall_id TEXT NOT NULL,
                finish TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
                );
                ''')
        
        self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS edh_decks (
                deck_id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_name VARCHAR(255) NOT NULL,
                commander_scryfall_id VARCHAR(36) NOT NULL,
                color_identity VARCHAR(10),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS edh_deck_cards (
                deck_id INTEGER,
                scryfall_id TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                category TEXT,
                PRIMARY KEY (deck_id, scryfall_id),
                FOREIGN KEY (deck_id) REFERENCES edh_decks (deck_id) ON DELETE CASCADE,
                FOREIGN KEY (scryfall_id) REFERENCES card_printings (scryfall_id)
            );
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS planeswalker_tracker (
                oracle_id TEXT PRIMARY KEY,
                default_scryfall_id TEXT NOT NULL,
                name TEXT NOT NULL,
                release_date DATE NOT NULL
            );
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS tcgplayer_price_overrides (
                override_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT NOT NULL,
                finish TEXT NOT NULL,
                tcgplayer_id INTEGER NOT NULL,
                tcgcsv_group_id INTEGER,
                note TEXT,
                UNIQUE (scryfall_id, finish)
            );
        ''')
        
        self.cursor.executescript("""
            CREATE INDEX IF NOT EXISTS idx_type_line 
                ON card_definitions(type_line);                      
                                  
            CREATE INDEX IF NOT EXISTS idx_inventory_scryfall_id
                ON inventory(scryfall_id);

            CREATE INDEX IF NOT EXISTS idx_inventory_tradeable
                ON inventory(is_tradeable);

            CREATE INDEX IF NOT EXISTS idx_inventory_location
                ON inventory(location_id);

            CREATE INDEX IF NOT EXISTS idx_inventory_finish
                ON inventory(finish);

            CREATE INDEX IF NOT EXISTS idx_inventory_tradeable_scryfall
                ON inventory(is_tradeable, scryfall_id);

            CREATE INDEX IF NOT EXISTS idx_inventory_location_scryfall
                ON inventory(location_id, scryfall_id);

            CREATE INDEX IF NOT EXISTS idx_card_printings_oracle
                ON card_printings(oracle_id);

            CREATE INDEX IF NOT EXISTS idx_card_printings_set_collector
                ON card_printings(set_code, collector_number);

            CREATE INDEX IF NOT EXISTS idx_price_history_scryfall_date
                ON price_history(scryfall_id, scraped_at);

            CREATE INDEX IF NOT EXISTS idx_price_history_source_date
                ON price_history(source, scraped_at);

            CREATE INDEX IF NOT EXISTS idx_price_history_source_scryfall_scraped
                ON price_history(source, scryfall_id, scraped_at DESC);
        """)  
        
        self.initialize_locations()

        if bootstrap_local_price_index is None:
            bootstrap_local_price_index = (
                os.environ.get("BOOTSTRAP_LOCAL_PRICE_INDEX", "0").strip().lower()
                in ("1", "true", "yes", "on")
            )

        if bootstrap_local_price_index:
            try:
                self.ensure_local_price_sidecar_index()
            except Exception:
                # Sidecar index bootstrap is best-effort and should not block app startup.
                pass

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
        # print(f"[-] CLOSING connection {self.trace_id}")
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None
            
    def nuke(self):
        self.wipe_db()
        # Now that the file is gone, re-establish the connection and tables
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        
        self.create_tables()
        self.initialize_locations()
        self.commit()

def get_db():
    """Return the request-scoped CardDB handle.

    SQLite connections must not live across Gunicorn requests or they can pin
    the WAL and shm files open for the lifetime of the worker.
    """
    manager = g.get("db_manager")

    if manager is None or getattr(manager, "conn", None) is None:
        manager = CardDB()
        g.db_manager = manager

    return manager


def close_db(error=None):
    manager = g.pop("db_manager", None)

    if manager is not None:
        manager.close()


def checkpoint_db(db_path, verbose=False):
    if not db_path:
        return

    with sqlite3.connect(db_path, timeout=30) as conn:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        if verbose:
            print(
                f"WAL checkpoint: busy={result[0]}, log={result[1]}, checkpointed={result[2]}"
            )
