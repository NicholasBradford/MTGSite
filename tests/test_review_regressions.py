import io
import sqlite3
import pytest


def test_unknown_page_has_404_status(client):
    assert client.get('/does-not-exist').status_code == 404


def test_planeswalkers_boot_on_fresh_schema(client, clean_db):
    assert client.get('/collection/planeswalkers').status_code == 200


def test_legacy_schema_is_upgraded(tmp_path):
    from db.db_manager import CardDB
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE wishlist (wish_id INTEGER PRIMARY KEY, scryfall_id TEXT, finish TEXT, priority INTEGER, added DATETIME, notes TEXT)')
        conn.execute("INSERT INTO wishlist VALUES (1, 'old-card', 'foil', 3, NULL, 'keep me')")
    manager = CardDB(path)
    try:
        manager.create_tables()
        manager.create_tables()
        row = manager.cursor.execute('SELECT notes, non_specific FROM wishlist').fetchone()
        assert tuple(row) == ('keep me', 0)
    finally:
        manager.close()


def test_every_connection_enforces_foreign_keys(tmp_path):
    from db.db_manager import CardDB
    path = tmp_path / 'foreign.db'
    manager = CardDB(path)
    manager.create_tables()
    manager.close()
    manager = CardDB(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            manager.cursor.execute("INSERT INTO inventory (scryfall_id) VALUES ('missing')")
    finally:
        manager.close()


@pytest.mark.parametrize('query', ['sort:location', 'usd>1', 'sort:usd', 'sort:added'])
def test_wishlist_inventory_aliases_do_not_crash(client, seed_cards, query):
    assert client.get('/wishlist', query_string={'q': query}).status_code == 200


def test_quoted_name_search(auth_client, seed_cards):
    response = auth_client.get('/inventory', query_string={'q': '"Sol Ring"'}, headers={'X-Requested-With': 'XMLHttpRequest', 'X-View-Mode': 'table'})
    assert b'data-scryfall-id="sf-sol-ring-clu-1"' in response.data


def test_negated_price_filter(auth_client, seed_cards):
    response = auth_client.get('/inventory', query_string={'q': '-usd>1'})
    assert b'Sol Ring' not in response.data
    assert b'Command Tower' in response.data


@pytest.mark.parametrize('query', ['qty><2', 'usd:..'])
def test_malformed_numeric_search_does_not_crash(auth_client, seed_cards, query):
    assert auth_client.get('/inventory', query_string={'q': query}).status_code == 200


def test_sort_location_does_not_crash(auth_client, seed_cards):
    assert auth_client.get('/inventory', query_string={'q': 'sort:location'}).status_code == 200


def test_deck_assigned_copies_remain_in_instance_modal(auth_client, seed_cards, db):
    db.execute('UPDATE inventory SET location_id = NULL WHERE scryfall_id = ?', (seed_cards['sol_ring'],))
    db.commit()
    response = auth_client.get('/get_instances/' + seed_cards['sol_ring'] + '/nonfoil')
    assert len(response.json['instances']) == 2


def test_user_cannot_edit_owner_wishlist(auth_client, seed_cards):
    assert auth_client.post('/api/wishlist/update/1', json={'notes': 'changed'}).status_code == 403


def test_user_cannot_assign_owner_inventory(auth_client, seed_cards, db):
    deck = db.execute('INSERT INTO edh_decks (deck_name, commander_scryfall_id) VALUES (?, ?)', ('Review', seed_cards['niv_mizzet'])).lastrowid
    db.commit()
    assert auth_client.post(f'/edh/assign_card/{deck}/oracle-sol-ring').status_code == 403


def seed_trade(db, cards, qty=1):
    db.execute("INSERT INTO trades (trade_id, user_id) VALUES ('REVIEW', 1)")
    db.execute("INSERT INTO trade_outbound_items (trade_id, scryfall_id, finish, quantity) VALUES ('REVIEW', ?, 'nonfoil', ?)", (cards['sol_ring'], qty))
    db.execute("INSERT INTO trade_inbound_items (trade_id, scryfall_id, finish, quantity) VALUES ('REVIEW', ?, 'nonfoil', 1)", (cards['jace'],))
    db.commit()


def test_trade_cannot_be_accepted_twice(admin_client, seed_cards, db):
    seed_trade(db, seed_cards)
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'REVIEW', 'action': 'accept'}).status_code == 200
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'REVIEW', 'action': 'accept'}).status_code == 409
    assert db.execute('SELECT COUNT(*) FROM inventory WHERE scryfall_id = ?', (seed_cards['jace'],)).fetchone()[0] == 2
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_trade_with_insufficient_inventory_is_atomic(admin_client, seed_cards, db):
    seed_trade(db, seed_cards, qty=3)
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'REVIEW', 'action': 'accept'}).status_code == 409
    assert db.execute('SELECT COUNT(*) FROM inventory').fetchone()[0] == 7
    assert db.execute("SELECT status FROM trades WHERE trade_id = 'REVIEW'").fetchone()[0] == 'Pending'


def test_missing_trade_returns_404(admin_client):
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'missing', 'action': 'accept'}).status_code == 404


@pytest.mark.parametrize('qty', [-1, 0, 1.5, True, 'abc'])
def test_invalid_trade_quantities_rejected(auth_client, seed_cards, qty):
    response = auth_client.post('/api/submit_trade', json={'outbound': [{'scryfall_id': seed_cards['sol_ring'], 'finish': 'nonfoil', 'qty': qty}]})
    assert response.status_code == 400


def test_failed_inbound_resolution_does_not_leave_partial_trade(auth_client, seed_cards, db, monkeypatch):
    import ScryfallFetcher
    def committing_failure(self, *args, **kwargs):
        self.db.commit()
        return False
    monkeypatch.setattr(ScryfallFetcher.ScryfallFetcher, 'fetch_and_add', committing_failure)
    response = auth_client.post('/api/submit_trade', json={'inbound': [{'scryfall_id': 'missing', 'finish': 'nonfoil', 'qty': 1, 'set_code': 'war', 'cn': '54'}]})
    assert response.status_code == 400
    assert db.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 0


def test_upload_does_not_use_client_filename(admin_client, tmp_path, monkeypatch):
    import routes.edh as edh
    target = tmp_path / 'existing.txt'
    target.write_text('original')
    def fail(*args):
        raise ValueError('test failure')
    monkeypatch.setattr(edh, 'process_and_import_deck', fail)
    response = admin_client.post('/edh/import', data={'deck_name': 'Review', 'color_identity': 'U', 'commander_name': 'Jace', 'file': (io.BytesIO(b'overwrite'), str(target))})
    assert response.status_code == 500
    assert target.read_text() == 'original'


def test_default_import_location_exists(client, clean_db):
    from services.card_fetcher import DEFAULT_LOCATION_ID
    assert clean_db.execute('SELECT 1 FROM locations WHERE location_id = ?', (DEFAULT_LOCATION_ID,)).fetchone()


def test_location_renumber_preserves_inventory(admin_client, seed_cards, db):
    response = admin_client.post('/admin/locations', data={'action': 'update_id', 'old_id': '2', 'new_id': '22', 'new_name': 'Renumbered'})
    assert response.status_code == 302
    assert db.execute('SELECT COUNT(*) FROM inventory WHERE location_id=22').fetchone()[0] == 4
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_failed_deck_resolution_leaves_no_partial_deck(app, seed_cards, db, tmp_path, monkeypatch):
    import routes.edh as edh
    path = tmp_path / 'deck.txt'
    path.write_text('1 Sol Ring\n1 Missing Card\n')
    def resolve(name, manager, fetcher):
        manager.commit()  # Mirror real fetcher metadata commits.
        return {'Commander': seed_cards['niv_mizzet'], 'Sol Ring': seed_cards['sol_ring']}.get(name)
    monkeypatch.setattr(edh, 'resolve_card_by_name', resolve)
    with app.test_request_context():
        with pytest.raises(ValueError):
            edh.process_and_import_deck(path, 'Atomic deck', 'UR', 'Commander')
    assert db.execute('SELECT COUNT(*) FROM edh_decks').fetchone()[0] == 0


@pytest.mark.parametrize('settings', ['[]', '{"features": null}', '{"features": {"edh_decks": "false"}}'])
def test_invalid_settings_keep_last_valid_config(tmp_path, settings):
    from services.app_settings import SettingsManager
    manager = SettingsManager()
    manager.path = tmp_path / 'settings.json'
    manager.load_initial()
    before = manager.all()
    manager.path.write_text(settings)
    assert manager.reload_if_changed(force=True) is False
    assert manager.all() == before


def test_bulk_template_honors_tradeable_and_location(client, clean_db, seed_locations):
    from services.card_fetcher import CardImporterService
    from db.db_manager import CardDB
    manager = CardDB()
    try:
        importer = CardImporterService(manager)
        request = importer._inventory_request_from_bulk_row({'set_code': 'dom', 'collector_number': '60', 'qty': '1', 'tradeable': 'yes', 'location': 'Trade Binder'})
        assert request.is_tradeable is True
        assert request.location_id == seed_locations['trade_binder']
    finally:
        manager.close()


def test_bulk_invalid_row_does_not_discard_valid_rows(client, clean_db, monkeypatch):
    from services.card_fetcher import CardImporterService, MassImportResult
    from db.db_manager import CardDB
    manager = CardDB()
    try:
        importer = CardImporterService(manager)
        monkeypatch.setattr(importer, 'ensure_set_is_fully_populated', lambda *args: None)
        seen = []
        def import_many(rows):
            seen.extend(rows)
            return MassImportResult(requested=len(rows), imported=len(rows))
        monkeypatch.setattr(importer.mass_importer, 'import_owned_many', import_many)
        result = importer.import_bulk_rows([{'set_code': 'dom', 'collector_number': '60', 'qty': 'oops'}, {'set_code': 'dom', 'collector_number': '60', 'qty': '2'}])
        assert (result.requested, result.imported, result.failed) == (2, 1, 1)
        assert seen[0].quantity == 2
    finally:
        manager.close()


def test_imported_copies_use_valid_default_location(client, clean_db):
    from services.card_fetcher import CardImporter, ImportedCard, InventoryImportRequest
    from db.db_manager import CardDB
    manager = CardDB()
    try:
        CardImporter(manager).import_owned_card(ImportedCard(oracle_id='new-oracle', scryfall_id='new-card', name='New card'), InventoryImportRequest('dom', '1', quantity=2))
        assert manager.cursor.execute('SELECT COUNT(*) FROM inventory WHERE location_id = 1').fetchone()[0] == 2
        assert manager.cursor.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        manager.close()


def test_http_500_handler_preserves_status(app):
    from werkzeug.exceptions import InternalServerError
    import app as app_module
    with app.test_request_context():
        body, status = app_module.internal_server_error(InternalServerError())
    assert status == 500
    assert 'Something went wrong' in body


def test_etched_price_quality_uses_foil_column(client, seed_cards, db):
    from routes.markets import FOIL_LIKE_FINISH_SQL
    expression = FOIL_LIKE_FINISH_SQL.format(finish_column='finish')
    assert db.execute(f"SELECT {expression} FROM (SELECT 'etched' AS finish)").fetchone()[0] == 1


def test_fetch_script_default_exists():
    from services.tcgcsv_prices import TCGCSV_FETCH_SCRIPT_PATH
    from pathlib import Path
    assert Path(TCGCSV_FETCH_SCRIPT_PATH).is_file()


def test_trade_page_uses_web_image_route(client, app):
    response = client.get('/trade')
    assert response.status_code == 200
    assert b'const IMAGE_BASE_PATH = "/var/data/"' in response.data
    assert app.config['IMAGE_PATH'].encode() not in response.data


def test_invalid_location_edit_returns_400(admin_client, seed_cards, db):
    before = db.execute('SELECT location_id FROM inventory WHERE instance_id=1').fetchone()[0]
    assert admin_client.post('/edit_instance/1', data={'location_id': '9999'}).status_code == 400
    assert db.execute('SELECT location_id FROM inventory WHERE instance_id=1').fetchone()[0] == before


def test_inventory_location_can_be_cleared(admin_client, seed_cards, db):
    assert admin_client.post('/edit_instance/1', data={'location_id': ''}).status_code == 200
    assert db.execute('SELECT location_id FROM inventory WHERE instance_id=1').fetchone()[0] is None


def test_public_card_click_does_not_call_missing_function(client, seed_cards):
    response = client.get('/wishlist')
    assert b'showPopup(' not in response.data


def test_trade_accept_checks_duplicate_outbound_lines(admin_client, seed_cards, db):
    seed_trade(db, seed_cards, qty=2)
    db.execute("INSERT INTO trade_outbound_items (trade_id, scryfall_id, finish, quantity) VALUES ('REVIEW', ?, 'nonfoil', 1)", (seed_cards['sol_ring'],))
    db.commit()
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'REVIEW', 'action': 'accept'}).status_code == 409
    assert db.execute('SELECT COUNT(*) FROM inventory').fetchone()[0] == 7


def test_trade_accept_excludes_cards_assigned_to_decks(admin_client, seed_cards, db):
    seed_trade(db, seed_cards, qty=2)
    db.execute("UPDATE inventory SET in_deck=1 WHERE instance_id=1")
    db.commit()
    assert admin_client.post('/api/manage_trade', json={'trade_id': 'REVIEW', 'action': 'accept'}).status_code == 409
    assert db.execute('SELECT COUNT(*) FROM inventory').fetchone()[0] == 7
