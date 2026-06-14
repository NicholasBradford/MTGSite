from ScryfallFetcher import ScryfallApiClient, ScryfallFetcher


class DBAdapter:
    def __init__(self, conn):
        self.conn = conn
        self.cursor = conn.cursor()

    def commit(self):
        self.conn.commit()


def test_fetch_and_add_single_card_success(app, clean_db, monkeypatch):
    def fake_card(self, set_code, collector_number):
        return {
            "id": "sid-1",
            "oracle_id": "oid-1",
            "name": "Test Card",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "type_line": "Creature - Elf",
            "oracle_text": "Test rules text",
            "colors": ["G"],
            "color_identity": ["G"],
            "collector_number": collector_number,
            "rarity": "common",
            "tcgplayer_id": 123,
            "tcgplayer_etched_id": None,
        }

    monkeypatch.setattr(ScryfallApiClient, "get_card_by_set_collector", fake_card)
    monkeypatch.setattr("ScryfallFetcher.update_prices_for_scryfall_ids_from_tcgcsv", lambda db, ids: 1)

    fetcher = ScryfallFetcher(DBAdapter(clean_db), setting=1)
    scryfall_id = fetcher.fetch_and_add("rtr", "1")

    assert scryfall_id == "sid-1"

    definition = clean_db.execute(
        "SELECT * FROM card_definitions WHERE oracle_id = ?",
        ("oid-1",),
    ).fetchone()
    printing = clean_db.execute(
        "SELECT * FROM card_printings WHERE scryfall_id = ?",
        ("sid-1",),
    ).fetchone()

    assert definition is not None
    assert printing is not None


def test_ensure_set_skips_non_recent_expansion(app, clean_db, monkeypatch):
    def fake_set(self, set_code):
        return {
            "name": "Legacy Set",
            "set_type": "expansion",
            "released_at": "2019-01-01",
            "icon_svg_uri": None,
        }

    def fail_if_called(self, set_code):
        raise AssertionError("iter_set_cards should not be called for non-recent sets")

    monkeypatch.setattr(ScryfallApiClient, "get_set", fake_set)
    monkeypatch.setattr(ScryfallApiClient, "iter_set_cards", fail_if_called)

    fetcher = ScryfallFetcher(DBAdapter(clean_db), setting=1)
    summary = fetcher.ensure_set_is_fully_populated("old")

    assert summary["skipped"] is True

    set_row = clean_db.execute(
        "SELECT * FROM sets WHERE set_code = ?",
        ("old",),
    ).fetchone()
    card_count = clean_db.execute("SELECT COUNT(*) FROM card_printings").fetchone()[0]

    assert set_row is not None
    assert card_count == 0


def test_ensure_set_best_effort_and_batch_price_sync(app, clean_db, monkeypatch):
    def fake_set(self, set_code):
        return {
            "name": "Recent Set",
            "set_type": "expansion",
            "released_at": "2030-01-01",
            "icon_svg_uri": None,
        }

    def fake_cards(self, set_code):
        yield {
            "id": "sid-good",
            "oracle_id": "oid-good",
            "name": "Good Card",
            "cmc": 1,
            "type_line": "Instant",
            "oracle_text": "Do thing",
            "colors": ["U"],
            "color_identity": ["U"],
            "collector_number": "10",
            "rarity": "uncommon",
            "tcgplayer_id": 456,
            "tcgplayer_etched_id": None,
        }
        yield {
            "id": "sid-bad",
            "name": "Bad Card Missing Oracle",
            "collector_number": "11",
            "rarity": "rare",
        }

    captured = {"ids": []}

    def fake_batch_price(db, ids):
        captured["ids"] = list(ids)
        return len(ids)

    monkeypatch.setattr(ScryfallApiClient, "get_set", fake_set)
    monkeypatch.setattr(ScryfallApiClient, "iter_set_cards", fake_cards)
    monkeypatch.setattr("ScryfallFetcher.update_prices_for_scryfall_ids_from_tcgcsv", fake_batch_price)

    fetcher = ScryfallFetcher(DBAdapter(clean_db), setting=1)
    summary = fetcher.ensure_set_is_fully_populated("new")

    assert summary["cards_synced"] == 1
    assert summary["cards_failed"] == 1
    assert summary["prices_updated"] == 1
    assert captured["ids"] == ["sid-good"]

    printing = clean_db.execute(
        "SELECT * FROM card_printings WHERE scryfall_id = ?",
        ("sid-good",),
    ).fetchone()
    assert printing is not None
