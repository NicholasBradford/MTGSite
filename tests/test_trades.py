import pytest


def test_submit_trade_success(auth_client, seed_cards):
    """
    Submitting a valid trade request should create the trade successfully.

    Uses the real app fixture from conftest.py so Flask-Login is initialized.
    """

    payload = {
        "outbound": [
            {
                "scryfall_id": seed_cards["sol_ring"],
                "finish": "nonfoil",
                "qty": 1,
                "name": "Sol Ring",
                "set_code": "clu",
                "cn": "1",
                "price": 1.25,
            }
        ],
        "inbound": [
            {
                "scryfall_id": seed_cards["jace"],
                "finish": "nonfoil",
                "qty": 1,
                "name": "Jace, Wielder of Mysteries",
                "set_code": "war",
                "cn": "54",
                "price": 5.00,
            }
        ],
    }

    response = auth_client.post("/api/submit_trade", json=payload)

    assert response.status_code == 200

    data = response.get_json()
    assert data is not None
    assert data.get("success") is True


def test_submit_empty_trade_returns_error(auth_client):
    """
    Empty inbound/outbound trade request should return a 400 error.
    """

    response = auth_client.post(
        "/api/submit_trade",
        json={
            "outbound": [],
            "inbound": [],
        },
    )

    assert response.status_code == 400

    data = response.get_json()
    assert data is not None
    assert data.get("success") is False
    assert "No cards selected" in data.get("error", "")


def test_submit_trade_requires_login(client, seed_cards):
    """
    Unauthenticated users should not be able to submit trades.
    """

    payload = {
        "outbound": [
            {
                "scryfall_id": seed_cards["sol_ring"],
                "finish": "nonfoil",
                "qty": 1,
                "name": "Sol Ring",
            }
        ],
        "inbound": [],
    }

    response = client.post("/api/submit_trade", json=payload)

    assert response.status_code in (302, 401)
    
def test_submit_trade_missing_inbound_set_code_returns_clean_error(auth_client, seed_cards):
    """
    Missing inbound set_code should return a clean client error, not a 500.
    """

    payload = {
        "outbound": [],
        "inbound": [
            {
                "scryfall_id": seed_cards["jace"],
                "finish": "nonfoil",
                "qty": 1,
                "name": "Jace, Wielder of Mysteries",
                # set_code intentionally missing
                "cn": "54",
                "price": 5.00,
            }
        ],
    }

    response = auth_client.post("/api/submit_trade", json=payload)

    assert response.status_code == 400

    data = response.get_json()
    assert data is not None
    assert data.get("success") is False
    assert "set_code" in data.get("error", "")


def test_submit_trade_missing_inbound_cn_returns_clean_error(auth_client, seed_cards):
    """
    Missing inbound cn should return a clean client error, not a 500.
    """

    payload = {
        "outbound": [],
        "inbound": [
            {
                "scryfall_id": seed_cards["jace"],
                "finish": "nonfoil",
                "qty": 1,
                "name": "Jace, Wielder of Mysteries",
                "set_code": "war",
                # cn intentionally missing
                "price": 5.00,
            }
        ],
    }

    response = auth_client.post("/api/submit_trade", json=payload)

    assert response.status_code == 400

    data = response.get_json()
    assert data is not None
    assert data.get("success") is False
    assert "cn" in data.get("error", "")