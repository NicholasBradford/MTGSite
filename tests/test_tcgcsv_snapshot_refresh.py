from pathlib import Path

import services.tcgcsv_prices as tcgcsv_prices


def _write_snapshot(snapshot_path: Path, snapshot_last_updated: str) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        "snapshot_last_updated,group_id,product_id,sub_type_name,market_price,mid_price,low_price,selected_price,captured_at\n"
        f"{snapshot_last_updated},123,456,Normal,1.0,1.1,0.9,1.0,2026-01-01T00:00:00\n",
        encoding="utf-8",
    )


def test_refresh_snapshot_skips_when_remote_timestamp_is_unchanged(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "daily_prices_latest.csv"
    _write_snapshot(snapshot_path, "2026-06-10T00:00:00Z")

    monkeypatch.setattr(
        tcgcsv_prices,
        "get_remote_tcgcsv_last_updated",
        lambda session=None, timeout=15: "2026-06-10T00:00:00Z",
    )

    def _unexpected_export(*args, **kwargs):
        raise AssertionError("Snapshot export should not run when timestamps are unchanged")

    monkeypatch.setattr(tcgcsv_prices, "export_daily_price_snapshot", _unexpected_export)

    result = tcgcsv_prices.refresh_daily_price_snapshot_if_needed(snapshot_path=str(snapshot_path))

    assert result["status"] == "unchanged"
    assert result["updated"] is False
    assert result["snapshot_path"] == str(snapshot_path)


def test_refresh_snapshot_updates_when_remote_timestamp_changes(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "daily_prices_latest.csv"
    _write_snapshot(snapshot_path, "2026-06-10T00:00:00Z")

    monkeypatch.setattr(
        tcgcsv_prices,
        "get_remote_tcgcsv_last_updated",
        lambda session=None, timeout=15: "2026-06-11T00:00:00Z",
    )

    captured = {}

    def _fake_export(snapshot_path, snapshot_last_updated=None):
        captured["snapshot_path"] = snapshot_path
        captured["snapshot_last_updated"] = snapshot_last_updated
        return snapshot_path

    monkeypatch.setattr(tcgcsv_prices, "export_daily_price_snapshot", _fake_export)

    result = tcgcsv_prices.refresh_daily_price_snapshot_if_needed(snapshot_path=str(snapshot_path))

    assert result["status"] == "updated"
    assert result["updated"] is True
    assert captured["snapshot_path"] == str(snapshot_path)
    assert captured["snapshot_last_updated"] == "2026-06-11T00:00:00Z"


def test_refresh_snapshot_dry_run_reports_would_update(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "daily_prices_latest.csv"
    _write_snapshot(snapshot_path, "2026-06-10T00:00:00Z")

    monkeypatch.setattr(
        tcgcsv_prices,
        "get_remote_tcgcsv_last_updated",
        lambda session=None, timeout=15: "2026-06-12T00:00:00Z",
    )

    result = tcgcsv_prices.refresh_daily_price_snapshot_if_needed(
        snapshot_path=str(snapshot_path),
        dry_run=True,
    )

    assert result["status"] == "would_update"
    assert result["updated"] is False
