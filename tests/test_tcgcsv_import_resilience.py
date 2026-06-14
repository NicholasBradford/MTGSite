"""
Resilience tests: card imports must succeed even when the local TCGCSV price
snapshot is absent or when pricing functions raise unexpectedly.

Policy:
- card ingestion (manual add, bulk, wishlist add) always completes
- price updates are silently skipped on pricing failure
- market sync route returns a warning SSE event and exits cleanly
"""

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

def test_run_price_update_downloads_snapshot_then_syncs(
    admin_client_fixture, monkeypatch
):
    """
    GET /run-price-update for an admin should:
    - Drive stream_refresh_daily_price_snapshot_if_needed to download the snapshot
    - Report the download status in the SSE stream
    - Continue to read local prices (mocked to empty groups → no prices, clean exit)
    """
    import routes.markets as markets_module

    def _fake_stream_refresh(**kw):
        yield (0, 3, 0, "2026-06-12T00:00:00Z", "downloading")
        yield (1, 3, 0, "2026-06-12T00:00:00Z", "downloading")
        yield (2, 3, 0, "2026-06-12T00:00:00Z", "downloading")
        yield (3, 3, 0, "2026-06-12T00:00:00Z", "downloading")
        yield (0, 0, 0, "2026-06-12T00:00:00Z", "complete")

    monkeypatch.setattr(
        markets_module,
        "stream_refresh_daily_price_snapshot_if_needed",
        _fake_stream_refresh,
    )
    monkeypatch.setattr(markets_module, "load_local_group_prices", lambda **kw: {})

    response = admin_client_fixture.get("/run-price-update")

    assert response.status_code == 200
    assert "text/event-stream" in response.content_type

    body = response.get_data(as_text=True)

    assert "2026-06-12" in body, (
        "Expected snapshot timestamp in SSE, got: " + body[:500]
    )


# ---------------------------------------------------------------------------
# Test 4 (updated): Market price-sync emits error SSE when download fails
#                   and no local snapshot exists
# ---------------------------------------------------------------------------

def test_run_price_update_emits_error_sse_when_download_fails_and_no_snapshot(
    admin_client_fixture, monkeypatch
):
    """
    GET /run-price-update when TCGCSV is unreachable AND there is no local
    snapshot must:
    - Return 200 text/event-stream
    - Contain an error SSE payload
    - NOT raise a 500
    """
    import services.tcgcsv_prices as tcgcsv_prices
    import routes.markets as markets_module

    def _raising_generator(**kw):
        raise RuntimeError("TCGCSV unreachable (test)")
        yield  # pragma: no cover

    monkeypatch.setattr(markets_module, "stream_refresh_daily_price_snapshot_if_needed", _raising_generator)
    monkeypatch.setattr(markets_module, "local_snapshot_exists", lambda: False)

    response = admin_client_fixture.get("/run-price-update")

    assert response.status_code == 200
    assert "text/event-stream" in response.content_type

    body = response.get_data(as_text=True)

    assert '"progress": 100' in body or "100" in body, (
        "Expected final progress=100 SSE event, got: " + body[:400]
    )
    assert (
        "unreachable" in body.lower()
        or "no snapshot" in body.lower()
        or "tcgcsv" in body.lower()
    ), "Expected an error message about TCGCSV or snapshot in SSE, got: " + body[:400]


# ---------------------------------------------------------------------------
# Test 5: Market price-sync uses stale local snapshot when download fails
# ---------------------------------------------------------------------------

def test_run_price_update_falls_back_to_existing_snapshot_on_download_error(
    admin_client_fixture, monkeypatch
):
    """
    When TCGCSV is unreachable but a local snapshot already exists, the sync
    should continue with the stale snapshot rather than failing.
    """
    import services.tcgcsv_prices as tcgcsv_prices
    import routes.markets as markets_module

    def _raising_generator(**kw):
        raise RuntimeError("TCGCSV unreachable (test)")
        yield  # pragma: no cover

    monkeypatch.setattr(markets_module, "stream_refresh_daily_price_snapshot_if_needed", _raising_generator)
    monkeypatch.setattr(markets_module, "local_snapshot_exists", lambda: True)
    monkeypatch.setattr(
        tcgcsv_prices,
        "get_local_snapshot_last_updated",
        lambda **kw: "2026-06-10T00:00:00Z",
    )
    # Empty snapshot → no groups → sync exits cleanly
    monkeypatch.setattr(markets_module, "load_local_group_prices", lambda **kw: {})

    response = admin_client_fixture.get("/run-price-update")

    assert response.status_code == 200
    assert "text/event-stream" in response.content_type

    body = response.get_data(as_text=True)

    # Should warn about TCGCSV being unreachable but still proceed
    assert (
        "existing" in body.lower()
        or "local snapshot" in body.lower()
        or "proceeding" in body.lower()
    ), "Expected fallback-to-local message in SSE, got: " + body[:400]


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
