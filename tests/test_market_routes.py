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


def test_market_dashboard_renders_spike_and_drop_cards(auth_client, monkeypatch):
    from routes import markets

    sample_spike = {
        "scryfall_id": "scryfall-spike",
        "name": "Test Market Spike",
        "set_code": "TST",
        "collector_number": "001",
        "image_url": "test-spike.jpg",
        "finish": "nonfoil",
        "qty": 1,
        "old_price": 1.00,
        "new_price": 5.00,
        "location_name": "Unsorted Box",
        "is_tradeable": 1,
    }
    sample_drop = {
        "scryfall_id": "scryfall-drop",
        "name": "Test Market Drop",
        "set_code": "TST",
        "collector_number": "002",
        "image_url": "test-drop.jpg",
        "finish": "nonfoil",
        "qty": 1,
        "old_price": 5.00,
        "new_price": 2.00,
        "location_name": "Unsorted Box",
        "is_tradeable": 0,
    }

    def fake_market_movers(*args, **kwargs):
        return [sample_spike], [sample_drop]

    monkeypatch.setattr(markets, "get_market_movers", fake_market_movers)

    response = auth_client.get("/market/dashboard")

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