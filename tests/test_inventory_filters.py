
def test_inventory_shows_tradeable_and_nontradeable_cards(auth_client, seed_cards):
    response = auth_client.get("/inventory")

    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Command Tower" in response.data
    assert b"Jace, Wielder of Mysteries" in response.data


def test_inventory_search_finds_card_by_name(auth_client, seed_cards):
    response = auth_client.get("/inventory?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data


def test_inventory_search_hides_nonmatching_cards(auth_client, seed_cards):
    response = auth_client.get("/inventory?q=Jace")

    assert response.status_code == 200
    assert b"Jace, Wielder of Mysteries" in response.data
    assert b"Sol Ring" not in response.data
    assert b"Command Tower" not in response.data


def test_inventory_search_finds_multicolor_card(auth_client, seed_cards):
    response = auth_client.get("/inventory?q=Niv")

    assert response.status_code == 200
    assert b"Niv-Mizzet" in response.data


def test_inventory_ajax_table_search_respects_query(auth_client, seed_cards, ajax_table_headers):
    response = auth_client.get("/inventory?q=Sol", headers=ajax_table_headers)

    assert response.status_code == 200
    assert b"<tr" in response.data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data


def test_inventory_ajax_grid_search_respects_query(auth_client, seed_cards, ajax_grid_headers):
    response = auth_client.get("/inventory?q=Sol", headers=ajax_grid_headers)

    assert response.status_code == 200
    assert b"<tr" not in response.data
    assert b"Sol Ring" in response.data
    assert b"Niv-Mizzet" not in response.data


def test_inventory_modal_is_hidden_on_initial_render(admin_client, seed_cards):
    response = admin_client.get("/inventory")

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


def test_inventory_identity_u_includes_multicolor_ur(auth_client, seed_cards):
    response = auth_client.get("/inventory?q=id:U")

    assert response.status_code == 200
    assert b"Niv-Mizzet" in response.data
    assert b"Jace, Wielder of Mysteries" in response.data


def test_inventory_identity_and_color_diverge_for_dimir_card(auth_client, seed_cards, app):
    with app.app_context():
        from db.db_manager import CardDB

        manager = CardDB()

        try:
            cursor = manager.cursor

            cursor.execute(
                """
                INSERT OR IGNORE INTO card_definitions (
                    oracle_id,
                    name,
                    mana_cost,
                    cmc,
                    type_line,
                    oracle_text,
                    color,
                    color_identity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "oracle-dimir-regression",
                    "Dimir Identity Regression Card",
                    "{U}{B}",
                    2,
                    "Creature — Test",
                    "Testing identity and color matching.",
                    "U,B",
                    "U,B",
                ),
            )

            cursor.execute(
                """
                INSERT OR IGNORE INTO card_printings (
                    scryfall_id,
                    oracle_id,
                    set_code,
                    collector_number,
                    rarity,
                    image_url,
                    flavor_text,
                    current_price,
                    current_price_foil
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sf-dimir-regression",
                    "oracle-dimir-regression",
                    "tst",
                    "900",
                    "common",
                    "",
                    None,
                    0.25,
                    0.75,
                ),
            )

            cursor.execute(
                """
                INSERT OR IGNORE INTO inventory (
                    scryfall_id,
                    finish,
                    condition,
                    is_tradeable,
                    purchase_price,
                    location_id,
                    is_surplus,
                    in_deck,
                    added,
                    deck_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sf-dimir-regression",
                    "nonfoil",
                    "NM",
                    1,
                    0.10,
                    None,
                    0,
                    0,
                    "2026-01-09 10:00:00",
                    None,
                ),
            )

            manager.commit()
        finally:
            manager.close()

    identity_u = auth_client.get("/inventory?q=id:U")
    identity_b = auth_client.get("/inventory?q=id:B")
    color_u = auth_client.get("/inventory?q=c:U")
    color_b = auth_client.get("/inventory?q=c:B")
    color_ub = auth_client.get("/inventory?q=c:UB")

    assert identity_u.status_code == 200
    assert identity_b.status_code == 200
    assert color_u.status_code == 200
    assert color_b.status_code == 200
    assert color_ub.status_code == 200

    assert b"Dimir Identity Regression Card" in identity_u.data
    assert b"Dimir Identity Regression Card" in identity_b.data
    assert b"Dimir Identity Regression Card" not in color_u.data
    assert b"Dimir Identity Regression Card" not in color_b.data
    assert b"Dimir Identity Regression Card" in color_ub.data