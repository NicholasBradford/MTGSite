import pytest


def test_market_dashboard_requires_login(client):
    response = client.get("/market/dashboard", follow_redirects=False)

    assert response.status_code in (302, 401)


def test_market_dashboard_loads_for_logged_in_user(auth_client):
    response = auth_client.get("/market/dashboard")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "Market" in page_text
    assert "Run Market Price Sync" in page_text


def test_market_dashboard_renders_empty_states_when_no_market_data(client, login_user):
    """
    The dashboard should not crash when price history is empty.
    It should render the shell and empty-state messaging.
    """
    login_user()

    response = client.get("/market/dashboard")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "No market opportunities yet" in page_text
    assert "No gainers found" in page_text
    assert "No losers found" in page_text


def test_market_dashboard_renders_spike_and_drop_cards(client, login_user, app):
    """
    Seeds enough data to create one market gainer and one market loser.

    Assumes:
    - inventory.scryfall_id joins card_printings.scryfall_id
    - card_printings.oracle_id joins card_definitions.oracle_id
    - price_history has scraped_at with either explicit insert support or default timestamp
    """
    login_user()

    with app.app_context():
        from db.db_manager import CardDB

        manager = CardDB()

        try:
            cursor = manager.cursor

            # Card definitions
            cursor.execute(
                """
                INSERT OR IGNORE INTO card_definitions (
                    oracle_id,
                    name
                )
                VALUES (?, ?)
                """,
                ("oracle-spike", "Test Market Spike"),
            )

            cursor.execute(
                """
                INSERT OR IGNORE INTO card_definitions (
                    oracle_id,
                    name
                )
                VALUES (?, ?)
                """,
                ("oracle-drop", "Test Market Drop"),
            )

            # Card printings
            cursor.execute(
                """
                INSERT OR IGNORE INTO card_printings (
                    scryfall_id,
                    oracle_id,
                    image_url,
                    set_code,
                    collector_number,
                    current_price,
                    current_price_foil
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scryfall-spike",
                    "oracle-spike",
                    "test-spike.jpg",
                    "TST",
                    "001",
                    5.00,
                    None,
                ),
            )

            cursor.execute(
                """
                INSERT OR IGNORE INTO card_printings (
                    scryfall_id,
                    oracle_id,
                    image_url,
                    set_code,
                    collector_number,
                    current_price,
                    current_price_foil
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scryfall-drop",
                    "oracle-drop",
                    "test-drop.jpg",
                    "TST",
                    "002",
                    2.00,
                    None,
                ),
            )

            # Inventory
            cursor.execute(
                """
                INSERT INTO inventory (
                    scryfall_id,
                    finish,
                    condition,
                    is_tradeable,
                    purchase_price,
                    location_id,
                    is_surplus,
                    in_deck
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scryfall-spike",
                    "nonfoil",
                    "NM",
                    1,
                    1.00,
                    None,
                    0,
                    0,
                ),
            )

            cursor.execute(
                """
                INSERT INTO inventory (
                    scryfall_id,
                    finish,
                    condition,
                    is_tradeable,
                    purchase_price,
                    location_id,
                    is_surplus,
                    in_deck
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "scryfall-drop",
                    "nonfoil",
                    "NM",
                    0,
                    4.00,
                    None,
                    0,
                    0,
                ),
            )

            # Price history: two snapshots per card.
            # Explicit scraped_at values avoid relying on defaults during tests.
            cursor.execute(
                """
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at
                )
                VALUES (?, ?, ?, ?)
                """,
                ("scryfall-spike", 1.00, None, "2026-01-01 00:00:00"),
            )

            cursor.execute(
                """
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at
                )
                VALUES (?, ?, ?, ?)
                """,
                ("scryfall-spike", 5.00, None, "2026-01-02 00:00:00"),
            )

            cursor.execute(
                """
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at
                )
                VALUES (?, ?, ?, ?)
                """,
                ("scryfall-drop", 5.00, None, "2026-01-01 00:00:00"),
            )

            cursor.execute(
                """
                INSERT INTO price_history (
                    scryfall_id,
                    price_usd,
                    price_foil,
                    scraped_at
                )
                VALUES (?, ?, ?, ?)
                """,
                ("scryfall-drop", 2.00, None, "2026-01-02 00:00:00"),
            )

            manager.commit()

        finally:
            manager.close()

    response = client.get("/market/dashboard")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "Test Market Spike" in page_text
    assert "Test Market Drop" in page_text
    assert "Market Gainers" in page_text
    assert "Market Losers" in page_text


def test_run_price_update_requires_login(client):
    response = client.get("/run-price-update", follow_redirects=False)

    assert response.status_code in (302, 401)


def test_run_price_update_blocks_non_admin_user(client, login_user):
    """
    The sync route should be more protected than the dashboard because it calls
    Scryfall, writes to card_printings, and inserts price_history rows.
    """
    login_user()

    response = client.get("/run-price-update", follow_redirects=False)

    assert response.status_code == 403
    
def test_market_dashboard_renders_expected_sections(auth_client):
    response = auth_client.get("/market/dashboard")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert (
        "Market Gainers" in page_text
        or "Market Spikes" in page_text
    )

    assert (
        "Market Losers" in page_text
        or "Market Drops" in page_text
    )

    assert (
        "Market Opportunities" in page_text
        or "No market opportunities yet" in page_text
    )
    
def test_run_price_update_streams_for_admin_without_hitting_scryfall(admin_client, monkeypatch):
    from routes import markets

    def fake_update_prices():
        yield 'data: {"progress": 100, "status": "Test complete"}\n\n'

    monkeypatch.setattr(markets, "update_prices", fake_update_prices)

    response = admin_client.get("/run-price-update")

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"

    body = response.get_data(as_text=True)

    assert "Test complete" in body
    assert '"progress": 100' in body