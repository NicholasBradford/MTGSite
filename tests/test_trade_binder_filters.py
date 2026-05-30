def test_trade_binder_only_shows_tradeable_cards(client, seed_cards):
    response = client.get("/binder/trades")

    assert response.status_code == 200

    # Tradeable in seed data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" in response.data

    # Non-tradeable in seed data
    assert b"Command Tower" not in response.data
    assert b"Jace, Wielder of Mysteries" not in response.data


def test_trade_binder_search_finds_tradeable_card(client, seed_cards):
    response = client.get("/binder/trades?q=Sol")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data


def test_trade_binder_search_does_not_show_nonmatching_tradeable_card(client, seed_cards):
    response = client.get("/binder/trades?q=Sol")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data


def test_trade_binder_search_does_not_show_nontradeable_card_even_if_name_matches(client, seed_cards):
    response = client.get("/binder/trades?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" not in response.data