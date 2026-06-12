def test_trade_binder_only_shows_tradeable_cards(auth_client, seed_cards):
    response = auth_client.get("/binder/trades")

    assert response.status_code == 200

    # Tradeable in seed data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" in response.data

    # Non-tradeable in seed data
    assert b"Command Tower" not in response.data
    assert b"Jace, Wielder of Mysteries" not in response.data


def test_trade_binder_search_finds_tradeable_card(auth_client, seed_cards):
    response = auth_client.get("/binder/trades?q=Sol")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data


def test_trade_binder_search_does_not_show_nonmatching_tradeable_card(auth_client, seed_cards):
    response = auth_client.get("/binder/trades?q=Sol")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data


def test_trade_binder_search_does_not_show_nontradeable_card_even_if_name_matches(auth_client, seed_cards):
    response = auth_client.get("/binder/trades?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" not in response.data


def test_trade_binder_modal_is_hidden_on_initial_render(admin_client, seed_cards):
    response = admin_client.get("/binder/trades")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)
    modal_marker = 'id="editModal"'
    modal_index = page_text.find(modal_marker)

    assert modal_index != -1

    tag_start = page_text.rfind("<div", 0, modal_index)
    tag_end = page_text.find(">", modal_index)
    modal_tag = page_text[tag_start:tag_end + 1]

    assert 'aria-hidden="true"' in modal_tag
    assert " hidden" in modal_tag


def test_trade_binder_identity_u_includes_tradeable_multicolor_ur(auth_client, seed_cards):
    response = auth_client.get("/binder/trades?q=id:U")

    assert response.status_code == 200
    assert b"Niv-Mizzet" in response.data
    assert b"Jace, Wielder of Mysteries" not in response.data