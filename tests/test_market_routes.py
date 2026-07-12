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


def test_market_dashboard_reuses_prefetched_market_lists(auth_client, monkeypatch):
    from routes import markets

    call_counts = {
        "movers": 0,
        "trade_alerts": 0,
    }

    def fake_prepare_market_query_cache(manager):
        manager._market_cache_ready = False

    def fake_market_summary(*args, **kwargs):
        return {
            "last_sync": "2026-07-11",
            "collection_value": 0,
            "total_gains": 0,
            "total_losses": 0,
            "trade_value": 0,
            "alert_count": 0,
            "tracked_count": 0,
            "missing_price_count": 0,
            "wishlist_drop_count": 0,
        }

    def fake_market_movers(*args, **kwargs):
        call_counts["movers"] += 1
        return [], []

    def fake_trade_alerts(*args, **kwargs):
        call_counts["trade_alerts"] += 1
        return []

    monkeypatch.setattr(markets, "prepare_market_query_cache", fake_prepare_market_query_cache)
    monkeypatch.setattr(markets, "get_market_summary", fake_market_summary)
    monkeypatch.setattr(markets, "get_market_movers", fake_market_movers)
    monkeypatch.setattr(markets, "get_trade_alerts", fake_trade_alerts)
    monkeypatch.setattr(markets, "get_wishlist_drops", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_deck_market_alerts", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_planeswalker_market_alerts", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_surplus_market_alerts", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_purchase_gain_loss_alerts", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_missing_price_cards", lambda *a, **k: [])
    monkeypatch.setattr(markets, "get_price_quality_flags", lambda *a, **k: [])

    response = auth_client.get("/market/dashboard")

    assert response.status_code == 200
    assert call_counts["movers"] == 1
    assert call_counts["trade_alerts"] == 1