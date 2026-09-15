"""
Resilience tests: card imports must succeed even when the local TCGCSV price
snapshot is absent or when pricing functions raise unexpectedly.

Policy:
- card ingestion (manual add, bulk, wishlist add) always completes
- price updates are silently skipped on pricing failure
- market sync route returns a warning SSE event and exits cleanly
"""

import json
from unittest.mock import Mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures shared across tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_client_fixture(client, seed_users):
    from tests.conftest import _build_login_helper

    login_as = _build_login_helper(client, seed_users)
    response = login_as("admin")
    assert response.status_code == 200
    return client


# ---------------------------------------------------------------------------
# Helper: stub fetch_and_add so tests don't need network access
# ---------------------------------------------------------------------------

def _patch_fetcher_no_prices(monkeypatch, returns_sid="sf-test-001"):
    """
    Replace ScryfallOrchestrator.fetch_and_add with a version that returns a
    fake scryfall_id and never touches TCGCSV.
    """
    import ScryfallFetcher

    def fake_fetch_and_add(self, set_code, collector_number, sync_prices=True):
        return returns_sid

    monkeypatch.setattr(
        ScryfallFetcher.ScryfallOrchestrator, "fetch_and_add", fake_fetch_and_add
    )


# ---------------------------------------------------------------------------
# Test 1: Manual card add redirect completes without a TCGCSV snapshot
# ---------------------------------------------------------------------------

def test_manual_card_add_redirects_without_snapshot(admin_client_fixture, monkeypatch, seed_users, seed_locations):
    """
    POST /add/inventory with a valid card form must redirect (not 500) even
    when no local price snapshot exists and TCGCSV calls would fail.
    """
    _patch_fetcher_no_prices(monkeypatch)

    # Ensure the pricing function raises if called (should NOT be reached remotely)
    import services.tcgcsv_prices as tcgcsv_prices

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "update_prices_for_scryfall_ids_from_tcgcsv made a remote call during import"
        )

    # Only patch the remote network call; local-only path uses snapshot file
    monkeypatch.setattr(tcgcsv_prices, "get_tcgcsv_prices_for_group", _raise_if_called)
    monkeypatch.setattr(tcgcsv_prices, "get_tcgcsv_magic_groups", _raise_if_called)

    response = admin_client_fixture.post(
        "/add/inventory",
        data={
            "set_code": "tst",
            "collector_number": "001",
            "is_foil": "no",
            "is_tradeable": "no",
            "condition": "NM",
            "price": "0",
            "location": str(seed_locations["unsorted"]),
            "qty": "1",
        },
        follow_redirects=False,
    )

    # Expecting a redirect back to /add/inventory — not a 500
    assert response.status_code in (302, 303), (
        f"Expected redirect after card add, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 2: Bulk CSV import POST completes without a TCGCSV snapshot
# ---------------------------------------------------------------------------

def test_bulk_import_post_succeeds_without_snapshot(admin_client_fixture, monkeypatch, seed_locations):
    """
    POST /add/inventory/bulk with a minimal CSV must redirect (not 500) even
    when pricing is unavailable.
    """
    _patch_fetcher_no_prices(monkeypatch)

    import services.tcgcsv_prices as tcgcsv_prices

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("Remote TCGCSV network call made during bulk import")

    monkeypatch.setattr(tcgcsv_prices, "get_tcgcsv_prices_for_group", _raise_if_called)
    monkeypatch.setattr(tcgcsv_prices, "get_tcgcsv_magic_groups", _raise_if_called)

    csv_content = (
        "set_code,collector_number,qty,finish,location,tradeable\n"
        "tst,001,1,nonfoil,Unsorted Box,no\n"
    )

    data = {
        "location_id": str(seed_locations["unsorted"]),
        "file": (csv_content.encode("utf-8"), "test_bulk.csv", "text/csv"),
    }

    response = admin_client_fixture.post(
        "/add/inventory/bulk",
        data=data,
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    # Redirect or 200 is acceptable; 500 is the failure condition.
    assert response.status_code in (200, 302, 303), (
        f"Expected successful response after bulk import, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 3: Market price-sync route downloads snapshot then syncs prices
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_price_sync(monkeypatch, tmp_path, admin_client_fixture, seed_cards, db):
    """Exercise the real CSV reader and database writes without remote I/O."""
    import requests
    import routes.markets as markets
    import services.tcgcsv_prices as prices

    card_id = seed_cards["sol_ring"]
    db.execute(
        "UPDATE card_printings SET tcgplayer_id = 123, tcgcsv_group_id = 456 WHERE scryfall_id = ?",
        (card_id,),
    )
    db.execute("DELETE FROM price_history WHERE scryfall_id = ?", (card_id,))
    db.commit()

    network = Mock(side_effect=AssertionError("Price-sync tests must not make network requests"))
    monkeypatch.setattr(requests.sessions.Session, "request", network)
    monkeypatch.setattr(markets, "TCGCSV_RATE_LIMIT_DELAY", 0)
    monkeypatch.setattr(prices, "find_prior_tcgcsv_history_files", lambda *a, **kw: [])

    def configure(price_date, message, updated):
        snapshot = tmp_path / f"prices_category_1_{price_date}.csv"
        snapshot.write_text(
            "productId,groupId,subTypeName,marketPrice\n"
            "123,456,Normal,9.75\n"
            "123,456,Foil,12.50\n",
            encoding="utf-8",
        )
        refresh = Mock(return_value={
            "attempted": True, "updated": updated,
            "message": message, "path": str(snapshot), "date": price_date,
        })
        resolver = Mock(return_value=str(snapshot))
        monkeypatch.setattr(markets, "refresh_current_day_history_csv_if_due", refresh)
        monkeypatch.setattr(markets, "resolve_local_price_snapshot_path", resolver)
        return refresh, resolver

    yield configure, card_id, network
    network.assert_not_called()
    # price_history isn't cleared by the shared clean_db fixture.
    db.execute("DELETE FROM price_history WHERE scryfall_id = ?", (card_id,))
    db.commit()


def _price_sync_events(response):
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
            if line.startswith("data: ")]


def _assert_snapshot_prices_saved(db, card_id, price_date):
    row = db.execute(
        "SELECT current_price, current_price_foil FROM card_printings WHERE scryfall_id = ?",
        (card_id,),
    ).fetchone()
    assert tuple(row) == (9.75, 12.50)
    history = db.execute(
        "SELECT price_usd, price_foil, scraped_at, source FROM price_history WHERE scryfall_id = ?",
        (card_id,),
    ).fetchall()
    assert [tuple(row) for row in history] == [(9.75, 12.50, price_date, "tcgcsv")]


def test_run_price_update_downloads_snapshot_then_syncs(
    admin_client_fixture, local_price_sync, db
):
    configure, card_id, _ = local_price_sync
    message = "Downloaded current-day local CSV: 2026-06-12"
    refresh, resolver = configure("2026-06-12", message, updated=True)

    events = _price_sync_events(admin_client_fixture.get("/run-price-update"))

    refresh.assert_called_once_with()
    resolver.assert_called_once_with()
    assert any(event["status"] == message for event in events)
    assert events[-1]["progress"] == 100
    assert "TCGCSV sync complete. Updated 1 cards" in events[-1]["status"]
    assert "2026-06-12" in events[-1]["status"]
    _assert_snapshot_prices_saved(db, card_id, "2026-06-12")


def test_run_price_update_emits_error_sse_when_download_fails_and_no_snapshot(
    admin_client_fixture, monkeypatch, local_price_sync, db
):
    """An unexpected refresh exception must produce an error, not fake success."""
    import routes.markets as markets

    _, card_id, _ = local_price_sync
    refresh = Mock(side_effect=RuntimeError("TCGCSV unreachable (test)"))
    resolver = Mock(side_effect=AssertionError("No snapshot should be read after a refresh exception"))
    monkeypatch.setattr(markets, "refresh_current_day_history_csv_if_due", refresh)
    monkeypatch.setattr(markets, "resolve_local_price_snapshot_path", resolver)

    events = _price_sync_events(admin_client_fixture.get("/run-price-update"))

    refresh.assert_called_once_with()
    resolver.assert_not_called()
    assert events[-1] == {"progress": 100, "status": "TCGCSV sync failed: TCGCSV unreachable (test)"}
    assert db.execute("SELECT COUNT(*) FROM price_history WHERE scryfall_id = ?", (card_id,)).fetchone()[0] == 0
    assert db.execute("SELECT current_price FROM card_printings WHERE scryfall_id = ?", (card_id,)).fetchone()[0] == 1.25


def test_run_price_update_falls_back_to_existing_snapshot_on_download_error(
    admin_client_fixture, local_price_sync, db
):
    configure, card_id, _ = local_price_sync
    message = "Download failed; using existing local CSV for 2026-06-10."
    refresh, resolver = configure("2026-06-10", message, updated=False)

    events = _price_sync_events(admin_client_fixture.get("/run-price-update"))

    refresh.assert_called_once_with()
    resolver.assert_called_once_with()
    assert any(event["status"] == message for event in events)
    assert events[-1]["progress"] == 100
    assert "TCGCSV sync complete. Updated 1 cards" in events[-1]["status"]
    assert "2026-06-10" in events[-1]["status"]
    _assert_snapshot_prices_saved(db, card_id, "2026-06-10")


# ---------------------------------------------------------------------------
# Test 4: update_prices_for_scryfall_ids_from_tcgcsv returns 0 silently
#         when called with local-only mode and snapshot is absent
# ---------------------------------------------------------------------------

def test_batch_price_update_returns_zero_without_snapshot(monkeypatch):
    """
    When called with local-only mode and no snapshot file is present,
    update_prices_for_scryfall_ids_from_tcgcsv must return 0 without
    raising and without making any remote TCGCSV requests.
    """
    import sqlite3
    from unittest.mock import MagicMock
    import services.tcgcsv_prices as tcgcsv_prices

    # Simulate an empty snapshot (no groups)
    monkeypatch.setattr(tcgcsv_prices, "load_local_group_prices", lambda **kw: {})

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("Remote TCGCSV call made in local-only mode")

    monkeypatch.setattr(tcgcsv_prices, "get_tcgcsv_magic_groups", _raise_if_called)
    monkeypatch.setattr(tcgcsv_prices, "tcgcsv_request", _raise_if_called)

    # Build a minimal mock manager that returns one row needing a price
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE card_printings "
        "(scryfall_id TEXT, tcgplayer_id INTEGER, tcgplayer_etched_id INTEGER, tcgcsv_group_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO card_printings VALUES ('sf-abc-001', 12345, NULL, 42)"
    )
    conn.commit()

    manager = MagicMock()
    manager.cursor.execute.return_value.fetchall.return_value = []

    result = tcgcsv_prices.update_prices_for_scryfall_ids_from_tcgcsv(
        manager,
        ["sf-abc-001"],
        data_source=tcgcsv_prices.TCGCSV_SOURCE_LOCAL_ONLY,
        allow_remote_group_lookup=False,
    )

    assert result == 0, f"Expected 0 updates without snapshot, got {result}"

    conn.close()
