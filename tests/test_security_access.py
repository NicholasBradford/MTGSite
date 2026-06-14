def test_inventory_requires_login(client, seed_cards):
    response = client.get("/inventory", follow_redirects=False)

    assert response.status_code in (302, 401)
    assert "/login" in response.headers.get("Location", "")


def test_trade_binder_requires_login(client, seed_cards):
    response = client.get("/binder/trades", follow_redirects=False)

    assert response.status_code in (302, 401)
    assert "/login" in response.headers.get("Location", "")


def test_add_inventory_requires_admin(auth_client):
    response = auth_client.get("/add/inventory", follow_redirects=False)

    assert response.status_code == 403


def test_bulk_import_page_requires_admin(auth_client):
    response = auth_client.get("/add/inventory/bulk", follow_redirects=False)

    assert response.status_code == 403


def test_bulk_import_post_requires_admin(auth_client):
    response = auth_client.post("/add/inventory/bulk", data={}, follow_redirects=False)

    assert response.status_code == 403


def test_delete_card_requires_admin(auth_client, seed_cards):
    response = auth_client.post("/delete_card/1", data={}, follow_redirects=False)

    assert response.status_code == 403


def test_manage_locations_requires_admin(auth_client):
    response = auth_client.get("/admin/locations", follow_redirects=False)

    assert response.status_code == 403


def test_manage_locations_post_requires_admin(auth_client):
    response = auth_client.post(
        "/admin/locations",
        data={"action": "add", "location_name": "Should Not Work"},
        follow_redirects=False,
    )

    assert response.status_code == 403
