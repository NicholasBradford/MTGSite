def test_wishlist_page_loads(client, seed_cards):
    response = client.get("/wishlist")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data


def test_wishlist_does_not_show_inventory_only_cards(client, seed_cards):
    response = client.get("/wishlist")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data
    assert b"Sol Ring" not in response.data
    assert b"Command Tower" not in response.data


def test_wishlist_search_finds_wishlist_card(client, seed_cards):
    response = client.get("/wishlist?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data


def test_wishlist_search_hides_nonmatching_cards(client, seed_cards):
    response = client.get("/wishlist?q=Sol")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" not in response.data


def test_wishlist_ajax_table_mode_returns_table_rows(client, seed_cards, ajax_table_headers):
    response = client.get("/wishlist", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Jace, Wielder of Mysteries" in response.data


def test_wishlist_ajax_grid_mode_returns_card_items(client, seed_cards, ajax_grid_headers):
    response = client.get("/wishlist", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"<tr" not in response.data
    assert b"Jace, Wielder of Mysteries" in response.data