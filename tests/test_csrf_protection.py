def _login_with_csrf(client, extract_csrf_token, username, password):
    login_page = client.get("/login")
    assert login_page.status_code == 200

    token = extract_csrf_token(login_page.get_data(as_text=True))

    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    return response


def test_login_rejects_post_without_csrf(csrf_client, seed_users):
    response = csrf_client.post(
        "/login",
        data={
            "username": seed_users["user"]["username"],
            "password": seed_users["user"]["password"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_login_accepts_valid_csrf(csrf_client, seed_users, extract_csrf_token):
    response = _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["user"]["username"],
        seed_users["user"]["password"],
    )

    assert response.headers.get("Location", "").endswith("/")


def test_manage_locations_post_rejects_missing_csrf(csrf_client, seed_users, extract_csrf_token):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["admin"]["username"],
        seed_users["admin"]["password"],
    )

    response = csrf_client.post(
        "/admin/locations",
        data={
            "action": "add",
            "location_name": "No Token Location",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_manage_locations_post_accepts_valid_csrf(csrf_client, seed_users, extract_csrf_token):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["admin"]["username"],
        seed_users["admin"]["password"],
    )

    locations_page = csrf_client.get("/admin/locations")
    assert locations_page.status_code == 200

    token = extract_csrf_token(locations_page.get_data(as_text=True))

    response = csrf_client.post(
        "/admin/locations",
        data={
            "csrf_token": token,
            "action": "add",
            "location_name": "CSRF Protected Location",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"CSRF Protected Location" in response.data


def test_edit_instance_rejects_missing_csrf(csrf_client, seed_users, extract_csrf_token, seed_cards):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["admin"]["username"],
        seed_users["admin"]["password"],
    )

    response = csrf_client.post(
        "/edit_instance/1",
        data={"location_id": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_edit_instance_accepts_header_csrf(csrf_client, seed_users, extract_csrf_token, seed_cards):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["admin"]["username"],
        seed_users["admin"]["password"],
    )

    inventory_page = csrf_client.get("/inventory")
    assert inventory_page.status_code == 200

    token = extract_csrf_token(inventory_page.get_data(as_text=True))

    response = csrf_client.post(
        "/edit_instance/1",
        data={"location_id": "1"},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "success"}


def test_submit_trade_rejects_json_post_without_csrf(csrf_client, seed_users, extract_csrf_token, seed_cards):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["user"]["username"],
        seed_users["user"]["password"],
    )

    payload = {
        "outbound": [
            {
                "scryfall_id": seed_cards["sol_ring"],
                "finish": "nonfoil",
                "qty": 1,
            }
        ],
        "inbound": [],
    }

    response = csrf_client.post(
        "/api/submit_trade",
        json=payload,
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_submit_trade_accepts_json_post_with_csrf_header(csrf_client, seed_users, extract_csrf_token, seed_cards):
    _login_with_csrf(
        csrf_client,
        extract_csrf_token,
        seed_users["user"]["username"],
        seed_users["user"]["password"],
    )

    trade_page = csrf_client.get("/binder/trades")
    assert trade_page.status_code == 200

    token = extract_csrf_token(trade_page.get_data(as_text=True))

    payload = {
        "outbound": [
            {
                "scryfall_id": seed_cards["sol_ring"],
                "finish": "nonfoil",
                "qty": 1,
            }
        ],
        "inbound": [],
    }

    response = csrf_client.post(
        "/api/submit_trade",
        json=payload,
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data is not None
    assert data.get("success") is True
