def test_guest_nav_shows_login_link(client):
    response = client.get("/")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "Login" in page_text
    assert "Logout" not in page_text
    assert "Add Cards" not in page_text
    assert "Market Dashboard" not in page_text


def test_user_nav_hides_admin_links(auth_client):
    response = auth_client.get("/")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "Logout" in page_text
    assert "testuser" in page_text
    assert "Add Cards" not in page_text
    assert "Market Dashboard" not in page_text


def test_admin_nav_shows_admin_links(admin_client):
    response = admin_client.get("/")

    assert response.status_code == 200

    page_text = response.get_data(as_text=True)

    assert "Logout" in page_text
    assert "Add Cards" in page_text
    assert "Market Dashboard" in page_text
    assert "admin" in page_text


def test_inventory_redirects_to_login_with_next(client, seed_cards):
    response = client.get("/inventory", follow_redirects=False)

    assert response.status_code in (302, 401)
    assert "/login" in response.headers.get("Location", "")
    assert "next=%2Finventory" in response.headers.get("Location", "")


def test_login_honors_safe_next_redirect(login_user):
    response = login_user(follow_redirects=False, next_url="/inventory")

    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith("/inventory")


def test_login_rejects_external_next_redirect(login_user):
    response = login_user(follow_redirects=False, next_url="https://example.com/phish")

    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith("/")
    assert "example.com" not in response.headers.get("Location", "")
