import sqlite3

from services.card_importer import CardImporterService


class DummyDbManager:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.executescript(
            """
            CREATE TABLE locations (
                location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );

            CREATE TABLE card_definitions (
                oracle_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE card_printings (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT,
                set_code TEXT,
                collector_number TEXT,
                current_price REAL,
                current_price_foil REAL
            );

            CREATE TABLE inventory (
                instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                location_id INTEGER,
                condition TEXT,
                finish TEXT,
                purchase_price REAL,
                is_tradeable INTEGER,
                added DATETIME,
                is_surplus INTEGER
            );

            CREATE TABLE price_history (
                price_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scryfall_id TEXT,
                price_usd REAL,
                price_foil REAL,
                source TEXT,
                scraped_at DATE DEFAULT CURRENT_DATE,
                UNIQUE(scryfall_id, scraped_at) ON CONFLICT REPLACE
            );
            """
        )
        self.cursor.execute("INSERT INTO locations (location_id, name) VALUES (1, 'Unsorted Box')")
        self.conn.commit()

    def commit(self):
        self.conn.commit()


class FakeFetcher:
    def __init__(self, manager, existing_price=None):
        self.manager = manager
        self.existing_price = existing_price
        self.calls = []

    def fetch_and_add(self, set_code, collector_number, sync_prices=True, return_context=False):
        set_code = str(set_code).lower()
        collector_number = str(collector_number)
        cache_key = (set_code, collector_number)
        self.calls.append(cache_key)

        oracle_id = f"or-{set_code}-{collector_number}"
        scryfall_id = f"sf-{set_code}-{collector_number}"

        self.manager.cursor.execute(
            "INSERT OR IGNORE INTO card_definitions (oracle_id, name) VALUES (?, ?)",
            (oracle_id, f"Card {set_code}-{collector_number}"),
        )
        self.manager.cursor.execute(
            """
            INSERT OR IGNORE INTO card_printings (
                scryfall_id,
                oracle_id,
                set_code,
                collector_number,
                current_price,
                current_price_foil
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scryfall_id,
                oracle_id,
                set_code,
                collector_number,
                self.existing_price,
                None,
            ),
        )
        self.manager.commit()

        payload = {
            "scryfall_id": scryfall_id,
            "scryfall_price_usd": "2.34",
            "scryfall_price_foil": "4.56",
        }
        return payload if return_context else scryfall_id


def test_import_single_card_applies_fallback_price_and_surplus_after_playset():
    manager = DummyDbManager()
    importer = CardImporterService(manager, fetcher=FakeFetcher(manager))

    result = importer.import_single_card(
        set_code="neo",
        collector_number="123",
        qty=5,
        location_id=1,
        condition="NM",
        finish="nonfoil",
        purchase_price=0,
        is_tradeable=0,
    )

    assert result["success"] is True
    assert result["copies_inserted"] == 5
    assert result["fallback_price_applied"] is True

    rows = manager.cursor.execute(
        "SELECT is_surplus FROM inventory ORDER BY instance_id ASC"
    ).fetchall()
    assert [r["is_surplus"] for r in rows] == [0, 0, 0, 0, 1]

    card = manager.cursor.execute(
        "SELECT current_price, current_price_foil FROM card_printings WHERE scryfall_id = ?",
        ("sf-neo-123",),
    ).fetchone()
    assert card["current_price"] == 2.34
    assert card["current_price_foil"] == 4.56

    history = manager.cursor.execute(
        "SELECT source, price_usd, price_foil FROM price_history WHERE scryfall_id = ?",
        ("sf-neo-123",),
    ).fetchone()
    assert history["source"] == "scryfall"
    assert history["price_usd"] == 2.34
    assert history["price_foil"] == 4.56


def test_import_single_card_skips_fallback_when_current_price_already_exists():
    manager = DummyDbManager()
    importer = CardImporterService(manager, fetcher=FakeFetcher(manager, existing_price=9.99))

    result = importer.import_single_card(
        set_code="mh3",
        collector_number="10",
        qty=1,
        location_id=1,
        condition="NM",
        finish="nonfoil",
        purchase_price=0,
        is_tradeable=0,
    )

    assert result["success"] is True
    assert result["fallback_price_applied"] is False

    history_count = manager.cursor.execute(
        "SELECT COUNT(*) AS c FROM price_history WHERE scryfall_id = ?",
        ("sf-mh3-10",),
    ).fetchone()["c"]
    assert history_count == 0


def test_bulk_import_caches_repeated_set_collector_lookups():
    manager = DummyDbManager()
    fake_fetcher = FakeFetcher(manager)
    importer = CardImporterService(manager, fetcher=fake_fetcher, commit_batch_size=2)

    result = importer.import_bulk_rows(
        [
            {"set_code": "ktk", "collector_number": "5", "qty": "1", "finish": "nonfoil"},
            {"set_code": "ktk", "collector_number": "5", "qty": "1", "finish": "foil"},
            {"set_code": "ktk", "collector_number": "6", "qty": "1", "finish": "nonfoil"},
        ],
        default_location_id=1,
    )

    assert result["rows_processed"] == 3
    assert result["cards_resolved"] == 2
    assert result["copies_inserted"] == 3
    assert len(fake_fetcher.calls) == 2
