from db.db_manager import CardDB


def test_create_tables_skips_sidecar_index_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_LOCAL_PRICE_INDEX", raising=False)

    calls = {"count": 0}

    def fake_ensure(self, snapshot_path=None, force_rebuild=False):
        calls["count"] += 1
        return None

    monkeypatch.setattr(CardDB, "ensure_local_price_sidecar_index", fake_ensure)

    db_path = tmp_path / "bootstrap_default.db"
    manager = CardDB(db_path=str(db_path))

    try:
        manager.create_tables()
    finally:
        manager.close()

    assert calls["count"] == 0


def test_create_tables_bootstraps_sidecar_index_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_LOCAL_PRICE_INDEX", "1")

    calls = {"count": 0}

    def fake_ensure(self, snapshot_path=None, force_rebuild=False):
        calls["count"] += 1
        return None

    monkeypatch.setattr(CardDB, "ensure_local_price_sidecar_index", fake_ensure)

    db_path = tmp_path / "bootstrap_enabled.db"
    manager = CardDB(db_path=str(db_path))

    try:
        manager.create_tables()
    finally:
        manager.close()

    assert calls["count"] == 1
