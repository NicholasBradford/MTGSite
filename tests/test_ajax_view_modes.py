def test_inventory_ajax_table_mode_returns_table_rows(auth_client, seed_cards, ajax_table_headers):
    response = auth_client.get("/inventory", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data


def test_inventory_ajax_grid_mode_returns_card_items(auth_client, seed_cards, ajax_grid_headers):
    response = auth_client.get("/inventory", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_table_mode_returns_table_rows(auth_client, seed_cards, ajax_table_headers):
    response = auth_client.get("/binder/trades", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data


def test_trade_binder_ajax_grid_mode_returns_card_items(auth_client, seed_cards, ajax_grid_headers):
    response = auth_client.get("/binder/trades", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_defaults_to_grid_when_view_mode_header_missing(auth_client, seed_cards, ajax_headers):
    response = auth_client.get("/binder/trades", headers=ajax_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data


def test_trade_binder_ajax_can_use_view_mode_query_param_as_fallback(auth_client, seed_cards, ajax_headers):
    response = auth_client.get("/binder/trades?view_mode=table", headers=ajax_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data