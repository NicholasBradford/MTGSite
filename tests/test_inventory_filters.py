
def test_inventory_shows_tradeable_and_nontradeable_cards(client, seed_cards):
    response = client.get("/inventory")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Command Tower" in response.data
    assert b"Jace, Wielder of Mysteries" in response.data


def test_inventory_search_finds_card_by_name(client, seed_cards):
    response = client.get("/inventory?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data


def test_inventory_search_hides_nonmatching_cards(client, seed_cards):
    response = client.get("/inventory?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data
    assert b"Sol Ring" not in response.data
    assert b"Command Tower" not in response.data


def test_inventory_search_finds_multicolor_card(client, seed_cards):
    response = client.get("/inventory?q=Niv")

    assert response.status_code == 200
    assert b"Niv-Mizzet" in response.data


def test_inventory_ajax_table_search_respects_query(client, seed_cards, ajax_table_headers):
    response = client.get("/inventory?q=Sol", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data


def test_inventory_ajax_grid_search_respects_query(client, seed_cards, ajax_grid_headers):
    response = client.get("/inventory?q=Sol", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"<tr" not in response.data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data