def test_inventory_ajax_table_mode_returns_table_rows(client, seed_cards, ajax_table_headers):
    response = client.get("/inventory", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data


def test_inventory_ajax_grid_mode_returns_card_items(client, seed_cards, ajax_grid_headers):
    response = client.get("/inventory", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_table_mode_returns_table_rows(client, seed_cards, ajax_table_headers):
    response = client.get("/binder/trades", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data


def test_trade_binder_ajax_grid_mode_returns_card_items(client, seed_cards, ajax_grid_headers):
    response = client.get("/binder/trades", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_defaults_to_grid_when_view_mode_header_missing(client, seed_cards, ajax_headers):
    response = client.get("/binder/trades", headers=ajax_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_can_use_view_mode_query_param_as_fallback(client, seed_cards, ajax_headers):
    response = client.get("/binder/trades?view_mode=table", headers=ajax_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data