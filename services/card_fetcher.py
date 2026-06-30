# services/card_importer.py
import os
import requests
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from dotenv import load_dotenv

load_dotenv()

SCRYFALL_HEADERS = {
    "User-Agent": "MTG-Collection-Tracker/1.0",
    "Accept": "application/json",
}


IMAGE_PATH = os.environ.get("IMAGE_PATH")
IMAGE_TIMEOUT_SECONDS = 10

SCRYFALL_BASE_URL = "https://api.scryfall.com"
SCRYFALL_TIMEOUT_SECONDS = 20
RATE_LIMIT_DELAY_SECONDS = 0.12
DEFAULT_LOCATION_ID = 5

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# DATACLASS
@dataclass
class ImportedCard:
    # Scryfall Data
    oracle_id: str
    scryfall_id: str
    tcgplayer_id: int | None = None
    tcgplayer_etched_id: int | None = None

    # TCGCSV Data
    tcgcsv_group_id: int | None = None

    # Card Identifiers
    name: str = ""
    set_code: str = ""
    collector_number: str = ""

    # Card Characteristics
    rarity: str = ""
    mana_cost: str = ""
    cmc: float = 0.0
    type_line: str = ""
    oracle_text: str = ""
    flavor_text: str = ""
    color_identity: str = ""
    color: str = ""

    # DB Specifics
    local_img_path: str = ""
    source_img_url: str = ""
    current_reg_price: Decimal | None = None
    current_foil_price: Decimal | None = None
    current_rainbow_price: Decimal | None = None
    current_etched_price: Decimal | None = None

    # Last Update
    added: datetime = field(default_factory=utc_now)
    tcgcsv_last_update: datetime | None = None
    
@dataclass
class CardImportRequest:
    set_code: str
    collector_number: str

@dataclass
class FailedImport:
    request: CardImportRequest
    reason: str

@dataclass
class MassImportResult:
    requested: int = 0
    imported: int = 0
    failed: int = 0
    failed_cards: list[FailedImport] = field(default_factory=list)
    
@dataclass
class InventoryImportRequest:
    set_code: str
    collector_number: str
    finish: str = "nonfoil"
    condition: str = "NM"
    quantity: int = 1
    location_id: int = DEFAULT_LOCATION_ID
    is_tradeable: bool = False
    purchase_price: Decimal | None = None
    is_surplus: bool = False
    in_deck: bool = False
    deck_id: int | None = None

    
# CARD ACCESS
class ScryfallCardAccess:
    def __init__(self) -> None:
        self.base_url = SCRYFALL_BASE_URL
        
    def _request_json(self, url: str) -> dict[str, Any] | None:
        try:
            response = requests.get(
                url,
                headers=SCRYFALL_HEADERS,
                timeout=SCRYFALL_TIMEOUT_SECONDS,
            )

            if response.status_code != 200:
                return None

            return response.json()

        except requests.RequestException:
            return None
        
    def get_card_by_set_collector(
        self,
        set_code: str,
        collector_number: str,
    ) -> dict[str, Any] | None:
        set_code = set_code.lower().strip()
        collector_number = collector_number.strip()

        url = f"{self.base_url}/cards/{set_code}/{collector_number}"
        return self._request_json(url)
    
    def _decimal_or_none(self, value: str | None) -> Decimal | None:
        if value is None or value == "":
            return None

        try:
            return Decimal(value)
        except InvalidOperation:
            return None
        
    def _get_oracle_id(self, card_data: dict[str, Any]) -> str | None:
        oracle_id = card_data.get("oracle_id")
        if oracle_id:
            return oracle_id

        faces = card_data.get("card_faces") or []
        if faces:
            return faces[0].get("oracle_id")

        return None
    
    def _get_image_url(self, card_data: dict[str, Any]) -> str:
        image_uris = card_data.get("image_uris")
        if image_uris:
            return image_uris.get("normal", "")

        faces = card_data.get("card_faces") or []
        if faces:
            return faces[0].get("image_uris", {}).get("normal", "")

        return ""
    
    def _get_text_fields(
        self,
        card_data: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        mana_cost = card_data.get("mana_cost") or ""
        type_line = card_data.get("type_line") or ""
        oracle_text = card_data.get("oracle_text") or ""
        flavor_text = card_data.get("flavor_text") or ""

        faces = card_data.get("card_faces") or []
        if not faces:
            return mana_cost, type_line, oracle_text, flavor_text

        if not mana_cost:
            mana_cost = faces[0].get("mana_cost") or ""

        if not type_line:
            type_line = card_data.get("type_line") or faces[0].get("type_line") or ""

        if not oracle_text:
            face_texts = [
                face.get("oracle_text", "")
                for face in faces
                if face.get("oracle_text")
            ]
            oracle_text = " // ".join(face_texts)

        if not flavor_text:
            face_flavors = [
                face.get("flavor_text", "")
                for face in faces
                if face.get("flavor_text")
            ]
            flavor_text = " // ".join(face_flavors)

        return mana_cost, type_line, oracle_text, flavor_text
    
    def _join_colors(self, values: list[str] | None) -> str:
        if not values:
            return ""

        return "".join(values)
    
    def build_imported_card(
        self,
        card_data: dict[str, Any],
    ) -> ImportedCard | None:
        scryfall_id = card_data.get("id")
        oracle_id = self._get_oracle_id(card_data)

        if not scryfall_id or not oracle_id:
            return None

        set_code = (card_data.get("set") or "").lower()
        source_img_url = self._get_image_url(card_data)
        local_img_path = ""

        if source_img_url:
            local_img_path = f"img/cards/{set_code}/{scryfall_id}.jpg"

        mana_cost, type_line, oracle_text, flavor_text = self._get_text_fields(card_data)

        prices = card_data.get("prices") or {}

        return ImportedCard(
            oracle_id=oracle_id,
            scryfall_id=scryfall_id,
            tcgplayer_id=card_data.get("tcgplayer_id"),
            tcgplayer_etched_id=card_data.get("tcgplayer_etched_id"),
            tcgcsv_group_id=None,

            name=card_data.get("name") or "",
            set_code=set_code,
            collector_number=card_data.get("collector_number") or "",

            rarity=card_data.get("rarity") or "",
            mana_cost=mana_cost,
            cmc=card_data.get("cmc") or 0.0,
            type_line=type_line,
            oracle_text=oracle_text,
            flavor_text=flavor_text,
            color_identity=self._join_colors(card_data.get("color_identity")),
            color=self._join_colors(card_data.get("colors")),

            local_img_path=local_img_path,
            source_img_url=source_img_url,
            current_reg_price=self._decimal_or_none(prices.get("usd")),
            current_foil_price=self._decimal_or_none(prices.get("usd_foil")),
            current_rainbow_price=None,
            current_etched_price=self._decimal_or_none(prices.get("usd_etched")),

            added=utc_now(),
            tcgcsv_last_update=None,
        )
        
    def fetch_imported_card(
        self,
        set_code: str,
        collector_number: str,
    ) -> ImportedCard | None:
        card_data = self.get_card_by_set_collector(set_code, collector_number)
        if not card_data:
            return None

        return self.build_imported_card(card_data)
    
# CARD IMPORT
class CardImporter:
    def __init__(self, db, image_root: str | Path | None = None) -> None:
        self.db = db
        self.image_root = Path(
            image_root
            or os.environ.get("IMAGE_PATH")
            or "static/images"
        )
        
    def upsert_card_definition(self, card: ImportedCard) -> None:
        self.db.cursor.execute(
            """
            INSERT INTO card_definitions (
                oracle_id,
                name,
                mana_cost,
                cmc,
                type_line,
                oracle_text,
                color,
                color_identity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(oracle_id) DO UPDATE SET
                name = excluded.name,
                mana_cost = excluded.mana_cost,
                cmc = excluded.cmc,
                type_line = excluded.type_line,
                oracle_text = excluded.oracle_text,
                color = excluded.color,
                color_identity = excluded.color_identity
            """,
            (
                card.oracle_id,
                card.name,
                card.mana_cost,
                card.cmc,
                card.type_line,
                card.oracle_text,
                card.color,
                card.color_identity,
            ),
        )
        
    def upsert_card_printing(self, card: ImportedCard) -> None:
        self.db.cursor.execute(
            """
            INSERT INTO card_printings (
                scryfall_id,
                oracle_id,
                set_code,
                collector_number,
                rarity,
                image_url,
                flavor_text,
                current_price,
                current_price_foil,
                tcgplayer_id,
                tcgplayer_etched_id,
                tcgcsv_group_id,
                tcgcsv_last_price_sync
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scryfall_id) DO UPDATE SET
                oracle_id = excluded.oracle_id,
                set_code = excluded.set_code,
                collector_number = excluded.collector_number,
                rarity = excluded.rarity,
                image_url = excluded.image_url,
                flavor_text = excluded.flavor_text,

                current_price = COALESCE(card_printings.current_price, excluded.current_price),
                current_price_foil = COALESCE(card_printings.current_price_foil, excluded.current_price_foil),

                tcgplayer_id = COALESCE(excluded.tcgplayer_id, card_printings.tcgplayer_id),
                tcgplayer_etched_id = COALESCE(excluded.tcgplayer_etched_id, card_printings.tcgplayer_etched_id),
                tcgcsv_group_id = COALESCE(excluded.tcgcsv_group_id, card_printings.tcgcsv_group_id),
                tcgcsv_last_price_sync = COALESCE(excluded.tcgcsv_last_price_sync, card_printings.tcgcsv_last_price_sync)
            """,
            (
                card.scryfall_id,
                card.oracle_id,
                card.set_code,
                card.collector_number,
                card.rarity,
                card.local_img_path,
                card.flavor_text,
                str(card.current_reg_price) if card.current_reg_price is not None else None,
                str(card.current_foil_price) if card.current_foil_price is not None else None,
                card.tcgplayer_id,
                card.tcgplayer_etched_id,
                card.tcgcsv_group_id,
                card.tcgcsv_last_update.isoformat() if card.tcgcsv_last_update else None,
            ),
        )
        
        
    def download_card_image(self, card: ImportedCard) -> None:
        if not card.source_img_url or not card.local_img_path:
            return

        destination = self.image_root / card.local_img_path
        
        if destination.exists():
            return

        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            response = requests.get(
                card.source_img_url,
                headers=SCRYFALL_HEADERS,
                timeout=IMAGE_TIMEOUT_SECONDS,
            )

            if response.status_code != 200:
                return

            destination.write_bytes(response.content)

        except requests.RequestException:
            return
        
    def import_card(self, card: ImportedCard) -> None:
        self.download_card_image(card)
        self.upsert_card_definition(card)
        self.upsert_card_printing(card)
        self.db.commit()
        
    def add_inventory_copy(
        self,
        card: ImportedCard,
        request: InventoryImportRequest,
    ) -> None:
            self.db.cursor.execute(
                """
                INSERT INTO inventory (
                    scryfall_id,
                    finish,
                    condition,
                    is_tradeable,
                    purchase_price,
                    location_id,
                    is_surplus,
                    in_deck,
                    added,
                    deck_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.scryfall_id,
                    request.finish,
                    request.condition,
                    1 if request.is_tradeable else 0,
                    str(request.purchase_price) if request.purchase_price is not None else None,
                    request.location_id,
                    1 if request.is_surplus else 0,
                    1 if request.in_deck else 0,
                    utc_now().isoformat(),
                    request.deck_id,
                ),
            )
    
    def add_inventory_copies(
        self,
        card: ImportedCard,
        request: InventoryImportRequest,
    ) -> None:
        for _ in range(request.quantity):
            self.add_inventory_copy(card, request)
        
    def import_owned_card(
        self,
        card: ImportedCard,
        request: InventoryImportRequest,
    ) -> None:
        self.download_card_image(card)
        self.upsert_card_definition(card)
        self.upsert_card_printing(card)
        self.add_inventory_copies(card, request)
        self.db.commit()
        
        
# MASS IMPORT
class MassCardImporter:
    def __init__(
        self,
        card_access: ScryfallCardAccess,
        card_importer: CardImporter,
        delay_seconds: float = RATE_LIMIT_DELAY_SECONDS,
    ) -> None:
        self.card_access = card_access
        self.card_importer = card_importer
        self.delay_seconds = delay_seconds

    def import_many(self, card_requests: list[CardImportRequest]) -> MassImportResult:
        result = MassImportResult(requested=len(card_requests))

        for request in card_requests:
            card = self.card_access.fetch_imported_card(
                request.set_code,
                request.collector_number,
            )

            if card is None:
                result.failed += 1
                result.failed_cards.append(
                    FailedImport(request=request, reason="Card not found on Scryfall")
                )
                time.sleep(self.delay_seconds)
                continue

            try:
                self.card_importer.import_card(card)
                result.imported += 1

            except Exception as error:
                result.failed += 1
                result.failed_cards.append(
                    FailedImport(request=request, reason=str(error))
                )
            time.sleep(self.delay_seconds)

        return result
    
    def import_owned_many(
        self,
        inventory_requests: list[InventoryImportRequest],
    ) -> MassImportResult:
        result = MassImportResult(requested=len(inventory_requests))

        for request in inventory_requests:
            card = self.card_access.fetch_imported_card(
                request.set_code,
                request.collector_number,
            )

            if card is None:
                result.failed += 1
                result.failed_cards.append(
                    FailedImport(
                        request=CardImportRequest(
                            request.set_code,
                            request.collector_number,
                        ),
                        reason="Card not found on Scryfall",
                    )
                )
                time.sleep(self.delay_seconds)
                continue

            try:
                self.card_importer.import_owned_card(card, request)
                result.imported += 1

            except Exception as error:
                result.failed += 1
                result.failed_cards.append(
                    FailedImport(
                        request=CardImportRequest(
                            request.set_code,
                            request.collector_number,
                        ),
                        reason=str(error),
                    )
                )

            time.sleep(self.delay_seconds)

        return result
    
class CardImporterService:
    def __init__(self, db, fetcher=None, commit_batch_size: int = 50) -> None:
        self.db = db
        self.card_access = ScryfallCardAccess()
        self.card_importer = CardImporter(db)
        self.mass_importer = MassCardImporter(
            self.card_access,
            self.card_importer,
        )
        self.commit_batch_size = commit_batch_size

    def import_single_card(
        self,
        set_code: str,
        collector_number: str,
        qty: int | str = 1,
        location_id: int | str | None = None,
        condition: str = "NM",
        finish: str = "nonfoil",
        purchase_price: Decimal | str | int | float | None = None,
        is_tradeable: bool | int = False,
    ) -> None:
        card = self.card_access.fetch_imported_card(set_code, collector_number)

        if card is None:
            raise ValueError(f"Card not found: {set_code} #{collector_number}")

        price_value = None
        if purchase_price not in (None, ""):
            price_value = Decimal(str(purchase_price))

        request = InventoryImportRequest(
            set_code=set_code,
            collector_number=collector_number,
            finish=finish,
            condition=condition,
            quantity=int(qty),
            location_id=int(location_id or DEFAULT_LOCATION_ID),
            purchase_price=price_value,
            is_tradeable=bool(int(is_tradeable)) if isinstance(is_tradeable, str) else bool(is_tradeable),
        )

        self.card_importer.import_owned_card(card, request)
    
    def _get_row_value(self, row: dict, *possible_names: str) -> str:
        for name in possible_names:
            value = row.get(name)
            if value not in (None, ""):
                return str(value).strip()

        return ""

    def _normalize_bulk_finish(self, value: str) -> str:
        value = (value or "").strip().lower()

        finish_map = {
            "normal": "nonfoil",
            "nonfoil": "nonfoil",
            "non-foil": "nonfoil",
            "regular": "nonfoil",
            "foil": "foil",
            "etched": "etched",
            "rainbow": "rainbow foil",
            "rainbow foil": "rainbow foil",
        }

        return finish_map.get(value, "nonfoil")

    def _normalize_bulk_condition(self, value: str) -> str:
        value = (value or "").strip().lower()

        condition_map = {
            "mint": "M",
            "near_mint": "NM",
            "near mint": "NM",
            "nm": "NM",
            "lightly_played": "LP",
            "lightly played": "LP",
            "lp": "LP",
            "moderately_played": "MP",
            "moderately played": "MP",
            "mp": "MP",
            "heavily_played": "HP",
            "heavily played": "HP",
            "hp": "HP",
            "damaged": "DMG",
            "dmg": "DMG",
        }

        return condition_map.get(value, "NM")

    def _decimal_from_bulk_value(self, value: str) -> Decimal | None:
        value = (value or "").strip()

        if not value:
            return None

        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    def _inventory_request_from_bulk_row(
        self,
        row: dict,
        default_location_id: int | str | None = None,
    ) -> InventoryImportRequest | None:
        set_code = self._get_row_value(
            row,
            "Set code",
            "set_code",
            "set",
            "Set",
        ).lower()

        collector_number = self._get_row_value(
            row,
            "Collector number",
            "collector_number",
            "collector_number",
            "cn",
            "Collector Number",
        )

        if not set_code or not collector_number:
            return None

        quantity_raw = self._get_row_value(row, "Quantity", "quantity", "qty")
        quantity = int(quantity_raw or "1")

        finish_raw = self._get_row_value(row, "Foil", "finish", "Finish")
        condition_raw = self._get_row_value(row, "Condition", "condition")
        purchase_price_raw = self._get_row_value(
            row,
            "Purchase price",
            "purchase_price",
            "price",
            "Price",
        )

        return InventoryImportRequest(
            set_code=set_code,
            collector_number=collector_number,
            finish=self._normalize_bulk_finish(finish_raw),
            condition=self._normalize_bulk_condition(condition_raw),
            quantity=quantity,
            location_id=int(default_location_id or DEFAULT_LOCATION_ID),
            purchase_price=self._decimal_from_bulk_value(purchase_price_raw),
        )
        
    def import_bulk_rows(
        self,
        rows,
        default_location_id: int | str | None = None,
    ) -> MassImportResult:
        inventory_requests: list[InventoryImportRequest] = []

        for row in rows:
            request = self._inventory_request_from_bulk_row(
                row,
                default_location_id=default_location_id,
            )

            if request is not None:
                inventory_requests.append(request)

        return self.mass_importer.import_owned_many(inventory_requests)
if __name__ == "__main__":
    from db.db_manager import CardDB

    db = CardDB()

    try:
        card_access = ScryfallCardAccess()
        card_importer = CardImporter(db)

        set_code = input("Card Set Code: ").strip().lower()
        collector_number = input("Card Collector Number: ").strip()
        finish = input("Finish [nonfoil/foil/etched/rainbow foil]: ").strip().lower() or "nonfoil"
        quantity = int(input("Quantity: ").strip() or "1")

        card = card_access.fetch_imported_card(set_code, collector_number)

        if card is not None:
            request = InventoryImportRequest(
                set_code=set_code,
                collector_number=collector_number,
                finish=finish,
                quantity=quantity,
            )

            card_importer.import_owned_card(card, request)
            print(
                f"Imported {quantity}x {card.name} "
                f"({card.set_code.upper()} #{card.collector_number}) to inventory."
            )
        else:
            print("Card not found.")

    finally:
        db.close()