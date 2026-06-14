import os
import shutil
import time
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from services.tcgcsv_prices import (
    TCGCSV_SOURCE_LOCAL_ONLY,
    update_prices_for_scryfall_ids_from_tcgcsv,
    update_single_card_price_from_tcgcsv,
)

# Global headers for Scryfall API compliance
headers = {'User-Agent': 'Mozilla/5.0 (MTG-Collection-Tracker/1.0)', 'Accept': 'application/json'}
IMAGE_PATH = os.environ.get('IMAGE_PATH')

SCRYFALL_TIMEOUT_SECONDS = 20
IMAGE_TIMEOUT_SECONDS = 10
RATE_LIMIT_DELAY_SECONDS = 0.1


@dataclass
class NormalizedCard:
    scryfall_id: str
    oracle_id: str
    set_code: str
    collector_number: str
    rarity: str
    local_img_path: str
    name: str
    mana_cost: str
    cmc: float
    type_line: str
    oracle_text: str
    color: str
    color_identity: str
    tcgplayer_id: int
    tcgplayer_etched_id: int


class ScryfallApiClient:
    def __init__(self):
        self.base_url = "https://api.scryfall.com/cards"

    def _request_json(self, url):
        response = requests.get(url, headers=headers, timeout=SCRYFALL_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        return response.json()

    def get_set(self, set_code):
        return self._request_json(f"https://api.scryfall.com/sets/{set_code}")

    def get_card_by_set_collector(self, set_code, collector_number):
        return self._request_json(f"{self.base_url}/{set_code}/{collector_number}")

    def iter_set_cards(self, set_code):
        search_url = f"https://api.scryfall.com/cards/search?q=set:{set_code}+-type:basic&unique=prints"

        while search_url:
            page = self._request_json(search_url)
            if not page:
                break

            for card in page.get('data', []):
                yield card

            search_url = page.get('next_page')
            if search_url:
                time.sleep(RATE_LIMIT_DELAY_SECONDS)


class CardPersistenceService:
    def __init__(self, db_manager):
        self.db = db_manager

    def set_exists(self, set_code):
        self.db.cursor.execute("SELECT set_code FROM sets WHERE set_code = ?", (set_code,))
        return self.db.cursor.fetchone() is not None

    def upsert_set(self, set_code, set_data, is_standard_legal, local_icon_path):
        self.db.cursor.execute("""
            INSERT OR REPLACE INTO sets (set_code, set_name, set_type, standard_legal, released_at, icon_svg_uri)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            set_code,
            set_data['name'],
            set_data.get('set_type'),
            is_standard_legal,
            set_data.get('released_at'),
            local_icon_path,
        ))

    def upsert_card_definition(self, card):
        self.db.cursor.execute("""
            INSERT OR IGNORE INTO card_definitions
            (oracle_id, name, mana_cost, cmc, type_line, oracle_text, color, color_identity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            card.oracle_id,
            card.name,
            card.mana_cost,
            card.cmc,
            card.type_line,
            card.oracle_text,
            card.color,
            card.color_identity,
        ))

    def upsert_card_printing(self, card):
        self.db.cursor.execute("""
            INSERT OR IGNORE INTO card_printings
                (
                    scryfall_id,
                    oracle_id,
                    set_code,
                    collector_number,
                    rarity,
                    image_url,
                    tcgplayer_id,
                    tcgplayer_etched_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scryfall_id) DO UPDATE SET
                    oracle_id = excluded.oracle_id,
                    set_code = excluded.set_code,
                    collector_number = excluded.collector_number,
                    rarity = excluded.rarity,
                    image_url = excluded.image_url,
                    tcgplayer_id = COALESCE(excluded.tcgplayer_id, card_printings.tcgplayer_id),
                    tcgplayer_etched_id = COALESCE(excluded.tcgplayer_etched_id, card_printings.tcgplayer_etched_id)
        """, (
            card.scryfall_id,
            card.oracle_id,
            card.set_code,
            card.collector_number,
            card.rarity,
            card.local_img_path,
            card.tcgplayer_id,
            card.tcgplayer_etched_id,
        ))

    def commit(self):
        self.db.commit()


class ScryfallOrchestrator:
    def __init__(self, db_manager, api_client, persistence, image_path=None):
        self.db = db_manager
        self.api = api_client
        self.persistence = persistence
        self.image_path = image_path or IMAGE_PATH or os.environ.get('IMAGE_PATH') or os.path.join('static', 'images')

    def _download_binary(self, url, destination_path, stream=False):
        if not url or os.path.exists(destination_path):
            return

        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        try:
            if stream:
                with requests.get(url, stream=True, headers=headers, timeout=IMAGE_TIMEOUT_SECONDS) as response:
                    if response.status_code == 200:
                        with open(destination_path, 'wb') as file_handle:
                            shutil.copyfileobj(response.raw, file_handle)
            else:
                response = requests.get(url, headers=headers, timeout=IMAGE_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    with open(destination_path, 'wb') as file_handle:
                        file_handle.write(response.content)
        except Exception as error:
            print(f"Asset download skipped for {destination_path}: {error}")

    def _extract_oracle_id(self, card_payload):
        oracle_id = card_payload.get('oracle_id')
        if oracle_id:
            return oracle_id

        faces = card_payload.get('card_faces') or []
        if faces:
            return faces[0].get('oracle_id')

        return None

    def _extract_image_url(self, card_payload):
        if card_payload.get('image_uris'):
            return card_payload.get('image_uris', {}).get('normal', '')

        faces = card_payload.get('card_faces') or []
        if faces:
            return faces[0].get('image_uris', {}).get('normal', '')

        return ''

    def _extract_text_fields(self, card_payload):
        mana_cost = card_payload.get('mana_cost')
        type_line = card_payload.get('type_line')
        oracle_text = card_payload.get('oracle_text')

        faces = card_payload.get('card_faces') or []
        if not faces:
            return mana_cost, type_line, oracle_text

        face = faces[0]
        if not mana_cost:
            mana_cost = face.get('mana_cost')
        if not type_line:
            type_line = face.get('type_line')
        if not oracle_text:
            front_text = face.get('oracle_text', '')
            back_text = faces[1].get('oracle_text', '') if len(faces) > 1 else ''
            oracle_text = f"{front_text} // {back_text}" if back_text else front_text

        return mana_cost, type_line, oracle_text

    def _normalize_card(self, card_payload, set_code):
        scryfall_id = card_payload.get('id')
        oracle_id = self._extract_oracle_id(card_payload)
        if not scryfall_id or not oracle_id:
            return None

        image_url = self._extract_image_url(card_payload)
        local_img_path = f"img/cards/{set_code}/{scryfall_id}.jpg"
        full_img_path = os.path.join(self.image_path, local_img_path)

        if image_url:
            self._download_binary(image_url, full_img_path, stream=True)

        mana_cost, type_line, oracle_text = self._extract_text_fields(card_payload)

        return NormalizedCard(
            scryfall_id=scryfall_id,
            oracle_id=oracle_id,
            set_code=set_code,
            collector_number=card_payload.get('collector_number'),
            rarity=card_payload.get('rarity'),
            local_img_path=local_img_path,
            name=card_payload.get('name'),
            mana_cost=mana_cost,
            cmc=card_payload.get('cmc', 0.0),
            type_line=type_line,
            oracle_text=oracle_text,
            color="".join(card_payload.get('colors', [])),
            color_identity="".join(card_payload.get('color_identity', [])),
            tcgplayer_id=card_payload.get('tcgplayer_id'),
            tcgplayer_etched_id=card_payload.get('tcgplayer_etched_id'),
        )

    def _update_batch_prices(self, scryfall_ids):
        try:
            updated_count = self._call_update_prices_for_scryfall_ids_from_tcgcsv(
                scryfall_ids
            )
            self.persistence.commit()
            return updated_count
        except Exception as error:
            print(f"TCGCSV batch price sync skipped: {error}")
            return 0

    def _call_update_prices_for_scryfall_ids_from_tcgcsv(self, scryfall_ids):
        sync_fn = update_prices_for_scryfall_ids_from_tcgcsv

        try:
            parameters = inspect.signature(sync_fn).parameters
        except (TypeError, ValueError):
            parameters = {}

        if "data_source" in parameters or "allow_remote_group_lookup" in parameters:
            return sync_fn(
                self.db,
                scryfall_ids,
                data_source=TCGCSV_SOURCE_LOCAL_ONLY,
                allow_remote_group_lookup=False,
            )

        return sync_fn(self.db, scryfall_ids)

    def ensure_set_is_fully_populated(self, set_code):
        result = {
            'set_code': set_code,
            'set_inserted': False,
            'cards_synced': 0,
            'cards_failed': 0,
            'prices_updated': 0,
            'skipped': False,
        }

        if self.persistence.set_exists(set_code):
            result['skipped'] = True
            return result

        set_data = self.api.get_set(set_code)
        if not set_data:
            result['skipped'] = True
            return result

        set_type = set_data.get('set_type')
        is_expansion = set_type in {'expansion', 'core'}
        release_date_str = set_data.get('released_at')
        is_recent = False

        if release_date_str:
            release_date = datetime.strptime(release_date_str, '%Y-%m-%d')
            is_recent = release_date > (datetime.now() - timedelta(days=1095))

        is_standard_legal = 1 if (is_expansion and is_recent) else 0

        icon_url = set_data.get('icon_svg_uri')
        local_icon_path = f"img/icons/{set_code}.svg"
        icon_full_path = os.path.join(self.image_path, local_icon_path)
        if icon_url:
            self._download_binary(icon_url, icon_full_path, stream=False)

        self.persistence.upsert_set(set_code, set_data, is_standard_legal, local_icon_path)
        self.persistence.commit()
        result['set_inserted'] = True

        if not (is_expansion and is_recent):
            print(f"Skipping bulk card download for {set_code.upper()} (Not a recent expansion).")
            result['skipped'] = True
            return result

        print(f"Standard Expansion confirmed: {set_code.upper()}. Syncing all cards...")
        imported_ids = []

        for raw_card in self.api.iter_set_cards(set_code):
            normalized = self._normalize_card(raw_card, set_code)
            if not normalized:
                result['cards_failed'] += 1
                continue

            try:
                self.persistence.upsert_card_definition(normalized)
                self.persistence.upsert_card_printing(normalized)
                imported_ids.append(normalized.scryfall_id)
                result['cards_synced'] += 1
            except Exception as error:
                result['cards_failed'] += 1
                print(f"Card persistence skipped for {raw_card.get('name', 'unknown')}: {error}")

        self.persistence.commit()
        result['prices_updated'] = self._update_batch_prices(imported_ids)
        print(f"Set {set_code.upper()} fully synchronized.")
        return result

    def fetch_and_add(self, set_code, collector_number, sync_prices=True, return_context=False):
        data = self.api.get_card_by_set_collector(set_code, collector_number)
        if not data:
            print(f"Error: Could not find {set_code} {collector_number}")
            return False

        normalized = self._normalize_card(data, set_code)
        if not normalized:
            print(f"Error: Missing oracle_id/scryfall_id for {set_code} {collector_number}")
            return False

        scryfall_prices = data.get('prices', {}) if isinstance(data, dict) else {}
        scryfall_price_usd = scryfall_prices.get('usd')
        scryfall_price_foil = scryfall_prices.get('usd_foil')

        try:
            self.persistence.upsert_card_definition(normalized)
            self.persistence.upsert_card_printing(normalized)
            self.persistence.commit()

            if sync_prices:
                try:
                    updated = self._call_update_prices_for_scryfall_ids_from_tcgcsv(
                        [normalized.scryfall_id]
                    )
                    if not updated:
                        update_single_card_price_from_tcgcsv(
                            self.db,
                            normalized.scryfall_id,
                            data_source=TCGCSV_SOURCE_LOCAL_ONLY,
                            allow_remote_group_lookup=False,
                        )
                    self.persistence.commit()
                except Exception as error:
                    print(f"TCGCSV price update skipped for {normalized.scryfall_id}: {error}")

            time.sleep(RATE_LIMIT_DELAY_SECONDS)
            if return_context:
                return {
                    'scryfall_id': normalized.scryfall_id,
                    'scryfall_price_usd': scryfall_price_usd,
                    'scryfall_price_foil': scryfall_price_foil,
                }

            return normalized.scryfall_id
        except Exception as error:
            print(f"DB Error: {error}")
            return False


class ScryfallFetcher:
    def __init__(self, db_manager, setting = 0):
        self.db = db_manager
        self.setting = setting
        self.image_root = IMAGE_PATH or os.environ.get('IMAGE_PATH') or os.path.join('static', 'images')
        self.image_dir = f"{self.image_root}/img/cards"
        self.icon_dir = f"{self.image_root}/img/icons"
        self.api = ScryfallApiClient()
        self.persistence = CardPersistenceService(self.db)
        self.orchestrator = ScryfallOrchestrator(self.db, self.api, self.persistence, image_path=self.image_root)
        
        # Ensure directories exist
        for directory in [self.image_dir, self.icon_dir]:
            if not os.path.exists(directory):
                os.makedirs(directory)
    
    def ensure_set_is_fully_populated(self, set_code):
        """Checks if a set is in the DB; if not, pulls every card printing for that set."""
        return self.orchestrator.ensure_set_is_fully_populated(set_code.lower())

    def fetch_and_add(self, set_code, collector_number, sync_prices=True, return_context=False):     
        set_code = set_code.lower()
        # Trigger the full set sync first to ensure checklist is ready
        if self.setting == 0:
            self.ensure_set_is_fully_populated(set_code)

        return self.orchestrator.fetch_and_add(
            set_code,
            collector_number,
            sync_prices=sync_prices,
            return_context=return_context,
        )