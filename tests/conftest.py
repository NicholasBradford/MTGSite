import importlib
import os
import re
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server
from werkzeug.security import generate_password_hash


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _prime_locations_table(db_path: Path) -> None:
    """
    Temporary guard for the current CardDB.create_tables() behavior.

    Right now create_tables() checks:
        SELECT * FROM locations
    and then immediately indexes fetchone()[0].

    On a brand-new empty DB, fetchone() can be None.
    This primes the locations table before app import so tests can boot cleanly.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locations (
                location_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO locations (location_id, name, description)
            VALUES (1, 'Unsorted Box', 'Cards waiting to be filed')
            """
        )
        conn.commit()
    finally:
        conn.close()


def _reload_app_module():
    """
    Reload app-related modules so each test session points at the temp DB.

    This matters because app.py creates the Flask app and initializes CardDB
    at import time.
    """
    modules_to_clear = [
        name
        for name in sys.modules
        if (
            name == "app"
            or name == "search"
            or name.startswith("routes.")
            or name.startswith("db.")
        )
    ]

    for name in modules_to_clear:
        sys.modules.pop(name, None)

    return importlib.import_module("app")


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("mtgsite_db")
    return db_dir / "test_mtg_inventory.db"


@pytest.fixture(scope="session")
def app(test_db_path):
    """
    Flask app configured for testing against a temp SQLite database.
    """
    sys.path.insert(0, str(PROJECT_ROOT))

    os.environ["DB_PATH"] = str(test_db_path)
    os.environ["SECRET_KEY"] = "pytest-secret-key"
    os.environ["ADMIN_REGISTRATION_KEY"] = "pytest-admin-key"
    os.environ["IMAGE_PATH"] = str(PROJECT_ROOT / "static" / "images")

    _prime_locations_table(test_db_path)

    app_module = _reload_app_module()
    flask_app = app_module.app

    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SERVER_NAME="localhost",
    )

    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def runner(app):
    return app.test_cli_runner()


@pytest.fixture()
def db(test_db_path):
    """
    Direct SQLite connection for test setup and assertions.
    """
    conn = sqlite3.connect(test_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def clean_db(db):
    """
    Clears mutable test data while preserving schema and default locations.

    This intentionally disables FK checks during cleanup because tests may
    create related rows across trades, trade items, inventory, users, etc.
    """
    db.rollback()
    db.execute("PRAGMA foreign_keys = OFF;")

    tables = [
        "trade_inbound_items",
        "trade_outbound_items",
        "trades",
        "wishlist",
        "inventory",
        "edh_deck_cards",
        "edh_decks",
        "card_printings",
        "card_definitions",
        "sets",
        "users",
        "locations",
    ]

    for table in tables:
        db.execute(f"DELETE FROM {table}")

    # Optional but helpful: reset autoincrement IDs between tests.
    db.execute("DELETE FROM sqlite_sequence WHERE name IN ({})".format(
        ",".join("?" for _ in tables)
    ), tables)

    db.execute(
        """
        INSERT INTO locations (location_id, name, description)
        VALUES (1, 'Unsorted Box', 'Cards waiting to be filed')
        """
    )

    db.commit()
    db.execute("PRAGMA foreign_keys = ON;")

    return db


@pytest.fixture()
def seed_locations(clean_db):
    clean_db.executemany(
        """
        INSERT OR IGNORE INTO locations (location_id, name, description)
        VALUES (?, ?, ?)
        """,
        [
            (2, "Trade Binder", "Cards available for trading"),
            (3, "Planeswalker Binder", "Planeswalker collection binder"),
            (4, "Bulk Box", "General storage"),
        ],
    )
    clean_db.commit()

    return {
        "unsorted": 1,
        "trade_binder": 2,
        "planeswalker_binder": 3,
        "bulk_box": 4,
    }


@pytest.fixture()
def seed_users(clean_db):
    users = {
        "admin": {
            "user_id": 1,
            "username": "admin",
            "password": "adminpass",
            "role": "admin",
        },
        "user": {
            "user_id": 2,
            "username": "testuser",
            "password": "userpass",
            "role": "user",
        },
    }

    for user in users.values():
        clean_db.execute(
            """
            INSERT INTO users (user_id, username, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (
                user["user_id"],
                user["username"],
                generate_password_hash(user["password"]),
                user["role"],
            ),
        )

    clean_db.commit()
    return users


@pytest.fixture()
def seed_cards(clean_db, seed_locations):
    """
    A small, high-value MTG card fixture.

    Covers:
    - normal inventory
    - tradeable inventory
    - foil/nonfoil pricing
    - multicolor color identity
    - colorless card
    - wishlist card
    - planeswalker type line
    """
    card_definitions = [
        (
            "oracle-sol-ring",
            "Sol Ring",
            "{1}",
            1,
            "Artifact",
            "{T}: Add {C}{C}.",
            "",
            "",
        ),
        (
            "oracle-opt",
            "Opt",
            "{U}",
            1,
            "Instant",
            "Scry 1. Draw a card.",
            "U",
            "U",
        ),
        (
            "oracle-command-tower",
            "Command Tower",
            "",
            0,
            "Land",
            "{T}: Add one mana of any color in your commander's color identity.",
            "",
            "",
        ),
        (
            "oracle-niv-mizzet",
            "Niv-Mizzet, Parun",
            "{U}{U}{U}{R}{R}{R}",
            6,
            "Legendary Creature — Dragon Wizard",
            "This spell can't be countered.",
            "U,R",
            "U,R",
        ),
        (
            "oracle-jace",
            "Jace, Wielder of Mysteries",
            "{1}{U}{U}{U}",
            4,
            "Legendary Planeswalker — Jace",
            "If you would draw a card while your library has no cards in it, you win the game instead.",
            "U",
            "U",
        ),
    ]

    card_printings = [
        (
            "sf-sol-ring-clu-1",
            "oracle-sol-ring",
            "clu",
            "1",
            "uncommon",
            "https://example.com/sol-ring.jpg",
            None,
            1.25,
            4.50,
        ),
        (
            "sf-opt-dom-60",
            "oracle-opt",
            "dom",
            "60",
            "common",
            "https://example.com/opt.jpg",
            None,
            0.10,
            0.25,
        ),
        (
            "sf-command-tower-cmm-1000",
            "oracle-command-tower",
            "cmm",
            "1000",
            "common",
            "",
            None,
            0.35,
            1.00,
        ),
        (
            "sf-niv-mizzet-grn-192",
            "oracle-niv-mizzet",
            "grn",
            "192",
            "rare",
            "https://example.com/niv.jpg",
            None,
            2.00,
            7.00,
        ),
        (
            "sf-jace-war-54",
            "oracle-jace",
            "war",
            "54",
            "rare",
            "https://example.com/jace.jpg",
            None,
            5.00,
            12.00,
        ),
    ]

    inventory = [
        # scryfall_id, finish, condition, is_tradeable, purchase_price, location_id, is_surplus, in_deck, added, deck_id
        ("sf-sol-ring-clu-1", "nonfoil", "NM", 1, 1.00, seed_locations["trade_binder"], 0, 0, "2026-01-01 10:00:00", None),
        ("sf-sol-ring-clu-1", "nonfoil", "NM", 1, 1.00, seed_locations["trade_binder"], 0, 0, "2026-01-02 10:00:00", None),
        ("sf-opt-dom-60", "nonfoil", "NM", 0, 0.05, seed_locations["bulk_box"], 0, 0, "2026-01-03 10:00:00", None),
        ("sf-opt-dom-60", "foil", "LP", 1, 0.20, seed_locations["trade_binder"], 0, 0, "2026-01-04 10:00:00", None),
        ("sf-command-tower-cmm-1000", "nonfoil", "NM", 0, 0.25, seed_locations["unsorted"], 0, 0, "2026-01-05 10:00:00", None),
        ("sf-niv-mizzet-grn-192", "nonfoil", "NM", 1, 1.50, seed_locations["trade_binder"], 0, 0, "2026-01-06 10:00:00", None),
        ("sf-jace-war-54", "nonfoil", "NM", 0, 4.00, seed_locations["planeswalker_binder"], 0, 0, "2026-01-07 10:00:00", None),
    ]

    wishlist = [
        ("sf-jace-war-54", "nonfoil", 3, "2026-01-08 10:00:00", "Need one for planeswalker binder"),
    ]

    clean_db.executemany(
        """
        INSERT INTO card_definitions (
            oracle_id, name, mana_cost, cmc, type_line, oracle_text, color, color_identity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        card_definitions,
    )

    clean_db.executemany(
        """
        INSERT INTO card_printings (
            scryfall_id, oracle_id, set_code, collector_number, rarity,
            image_url, flavor_text, current_price, current_price_foil
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        card_printings,
    )

    clean_db.executemany(
        """
        INSERT INTO inventory (
            scryfall_id, finish, condition, is_tradeable, purchase_price,
            location_id, is_surplus, in_deck, added, deck_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        inventory,
    )

    clean_db.executemany(
        """
        INSERT INTO wishlist (
            scryfall_id, finish, priority, added, notes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        wishlist,
    )

    clean_db.commit()

    return {
        "sol_ring": "sf-sol-ring-clu-1",
        "opt": "sf-opt-dom-60",
        "command_tower": "sf-command-tower-cmm-1000",
        "niv_mizzet": "sf-niv-mizzet-grn-192",
        "jace": "sf-jace-war-54",
    }


@pytest.fixture()
def auth_client(client, seed_users):
    login_as = _build_login_helper(client, seed_users)
    response = login_as("user")

    assert response.status_code == 200
    return client


@pytest.fixture()
def admin_client(client, seed_users):
    login_as = _build_login_helper(client, seed_users)
    response = login_as("admin")

    assert response.status_code == 200
    return client


def _build_login_helper(client, seed_users):
    def _login(role="user", follow_redirects=True, next_url=None):
        user = seed_users[role]
        form_data = {
            "username": user["username"],
            "password": user["password"],
        }

        if next_url is not None:
            form_data["next"] = next_url

        return client.post(
            "/login",
            data=form_data,
            follow_redirects=follow_redirects,
        )

    return _login


@pytest.fixture()
def login_as(client, seed_users):
    return _build_login_helper(client, seed_users)


@pytest.fixture()
def login_user(login_as):
    def _login_user(follow_redirects=True, next_url=None):
        return login_as("user", follow_redirects=follow_redirects, next_url=next_url)

    return _login_user


@pytest.fixture()
def login_admin(login_as):
    def _login_admin(follow_redirects=True, next_url=None):
        return login_as("admin", follow_redirects=follow_redirects, next_url=next_url)

    return _login_admin


@pytest.fixture()
def ajax_headers():
    return {
        "X-Requested-With": "XMLHttpRequest",
    }


@pytest.fixture()
def ajax_table_headers():
    return {
        "X-Requested-With": "XMLHttpRequest",
        "X-View-Mode": "table",
    }


@pytest.fixture()
def ajax_grid_headers():
    return {
        "X-Requested-With": "XMLHttpRequest",
        "X-View-Mode": "grid",
    }


@pytest.fixture()
def extract_csrf_token():
    hidden_input_pattern = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
    meta_tag_pattern = re.compile(r'meta\s+name="csrf-token"\s+content="([^"]+)"')

    def _extract(html):
        meta_match = meta_tag_pattern.search(html)
        if meta_match:
            return meta_match.group(1)

        for hidden_match in hidden_input_pattern.finditer(html):
            token = hidden_match.group(1)
            if token and not token.startswith("${"):
                return token

        raise AssertionError("Expected csrf token in hidden input or meta tag")

    return _extract


@pytest.fixture()
def csrf_client(app):
    previous_csrf_state = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True

    with app.test_client() as client:
        yield client

    app.config["WTF_CSRF_ENABLED"] = previous_csrf_state


@pytest.fixture()
def live_server(app):
    host = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    _, port = sock.getsockname()
    sock.close()

    server = make_server(host, port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)