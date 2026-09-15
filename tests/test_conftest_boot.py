def test_app_boots_with_temp_database(auth_client, seed_cards):
    response = auth_client.get("/inventory")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Opt" in response.data


def test_login_fixture_works(auth_client, seed_cards):
    response = auth_client.get("/inventory")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data


def test_inventory_ajax_table_mode_returns_table_rows(auth_client, seed_cards, ajax_table_headers):
    response = auth_client.get("/inventory", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data


def test_inventory_ajax_grid_mode_returns_card_items(auth_client, seed_cards, ajax_grid_headers):
    response = auth_client.get("/inventory", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"<tr" not in response.data