import datetime
from dataclasses import dataclass

import ScryfallFetcher


def _to_float_or_none(value):
    if value in (None, ""):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_truthy_csv_value(value):
    return str(value or "").strip().lower() in {"yes", "y", "true", "1"}


@dataclass
class ImportResult:
    rows_processed: int = 0
    copies_inserted: int = 0
    cards_resolved: int = 0
    cards_failed: int = 0
    fallback_prices_applied: int = 0


class CardImporterService:
    def __init__(self, db_manager, fetcher=None, commit_batch_size=50):
        self.db = db_manager
        self.fetcher = fetcher or ScryfallFetcher.ScryfallFetcher(db_manager, setting=1)
        self.commit_batch_size = max(1, int(commit_batch_size))

    def _ensure_card(self, set_code, collector_number, sync_prices=True):
        context = self.fetcher.fetch_and_add(
            set_code,
            collector_number,
            sync_prices=sync_prices,
            return_context=True,
        )
        if not context:
            return None

        scryfall_id = context.get("scryfall_id")
        if not scryfall_id:
            return None

        fallback_applied = self._apply_scryfall_fallback_price(
            scryfall_id,
            context.get("scryfall_price_usd"),
            context.get("scryfall_price_foil"),
        )

        return {
            "scryfall_id": scryfall_id,
            "fallback_price_applied": fallback_applied,
        }

    def _apply_scryfall_fallback_price(self, scryfall_id, scryfall_price_usd, scryfall_price_foil):
        price_usd = _to_float_or_none(scryfall_price_usd)
        price_foil = _to_float_or_none(scryfall_price_foil)
        if price_usd is None and price_foil is None:
            return False

        existing = self.db.cursor.execute(
            """
            SELECT current_price, current_price_foil
            FROM card_printings
            WHERE scryfall_id = ?
            """,
            (scryfall_id,),
        ).fetchone()

        if not existing:
            return False

        has_existing_market_price = (
            existing["current_price"] is not None or existing["current_price_foil"] is not None
        )
        if has_existing_market_price:
            return False

        self.db.cursor.execute(
            """
            UPDATE card_printings
            SET current_price = COALESCE(current_price, ?),
                current_price_foil = COALESCE(current_price_foil, ?)
            WHERE scryfall_id = ?
            """,
            (price_usd, price_foil, scryfall_id),
        )

        self.db.cursor.execute(
            """
            INSERT INTO price_history (scryfall_id, price_usd, price_foil, source)
            VALUES (?, ?, ?, 'scryfall')
            """,
            (scryfall_id, price_usd, price_foil),
        )
        self.db.commit()
        return True

    def _get_or_create_location(self, location_name):
        rec = self.db.cursor.execute(
            "SELECT location_id FROM locations WHERE name = ?",
            (location_name,),
        ).fetchone()
        if rec:
            return rec["location_id"]

        unsorted = self.db.cursor.execute(
            "SELECT location_id FROM locations WHERE name = 'Unsorted Box'"
        ).fetchone()
        if unsorted:
            return unsorted["location_id"]

        self.db.cursor.execute("INSERT INTO locations (name) VALUES ('Unsorted Box')")
        return self.db.cursor.lastrowid

    def _get_card_name(self, scryfall_id):
        rec = self.db.cursor.execute(
            """
            SELECT cd.name
            FROM card_definitions cd
            JOIN card_printings cp ON cd.oracle_id = cp.oracle_id
            WHERE cp.scryfall_id = ?
            """,
            (scryfall_id,),
        ).fetchone()
        return rec["name"] if rec else ""

    def _current_inventory_count(self, scryfall_id):
        row = self.db.cursor.execute(
            "SELECT COUNT(*) AS count FROM inventory WHERE scryfall_id = ?",
            (scryfall_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def _resolve_bulk_destination(self, set_code, card_name, finish, raw_dest, default_location_id, tradeable):
        assigned_loc_id = default_location_id

        if raw_dest == "Master":
            binder_name = f"{set_code.upper()} Master Set Binder"
            bulk_box_name = f"{set_code.upper()} Bulk Box"

            binder_loc_id = self._get_or_create_location(binder_name)
            bulk_loc_id = self._get_or_create_location(bulk_box_name)

            counts = self.db.cursor.execute(
                """
                SELECT
                    SUM(CASE WHEN i.finish = 'foil' THEN 1 ELSE 0 END) AS foil_count,
                    SUM(CASE WHEN i.finish = 'nonfoil' THEN 1 ELSE 0 END) AS nonfoil_count
                FROM inventory i
                JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                WHERE cd.name = ? AND i.location_id = ?
                """,
                (card_name, binder_loc_id),
            ).fetchone()

            foil_count = counts["foil_count"] if counts and counts["foil_count"] else 0
            nonfoil_count = counts["nonfoil_count"] if counts and counts["nonfoil_count"] else 0
            total_count = foil_count + nonfoil_count

            if finish == "nonfoil":
                if total_count >= 4:
                    tradeable = 1
                    assigned_loc_id = bulk_loc_id
                else:
                    assigned_loc_id = binder_loc_id
            elif finish == "foil":
                if foil_count >= 4:
                    tradeable = 1
                    assigned_loc_id = bulk_loc_id
                elif total_count >= 4 and nonfoil_count > 0:
                    kick_target = self.db.cursor.execute(
                        """
                        SELECT i.instance_id
                        FROM inventory i
                        JOIN card_printings cp ON i.scryfall_id = cp.scryfall_id
                        JOIN card_definitions cd ON cp.oracle_id = cd.oracle_id
                        WHERE cd.name = ?
                          AND i.location_id = ?
                          AND i.finish = 'nonfoil'
                        ORDER BY i.added DESC
                        LIMIT 1
                        """,
                        (card_name, binder_loc_id),
                    ).fetchone()
                    if kick_target:
                        self.db.cursor.execute(
                            "UPDATE inventory SET location_id = ? WHERE instance_id = ?",
                            (bulk_loc_id, kick_target["instance_id"]),
                        )
                    assigned_loc_id = binder_loc_id
                else:
                    assigned_loc_id = binder_loc_id

        elif raw_dest == "Bulk":
            tradeable = 1
            assigned_loc_id = self._get_or_create_location(f"{set_code.upper()} Bulk Box")
        elif raw_dest == "Trade":
            tradeable = 1
            assigned_loc_id = self._get_or_create_location("Trades")

        return assigned_loc_id, tradeable

    def insert_inventory_copy(
        self,
        scryfall_id,
        location_id,
        condition,
        finish,
        purchase_price,
        is_tradeable,
        commit=False,
    ):
        current_count = self._current_inventory_count(scryfall_id)
        surplus_val = 1 if current_count >= 4 else 0

        self.db.cursor.execute(
            """
            INSERT INTO inventory (
                scryfall_id,
                location_id,
                condition,
                finish,
                purchase_price,
                is_tradeable,
                added,
                is_surplus
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scryfall_id,
                location_id,
                condition,
                finish,
                purchase_price,
                is_tradeable,
                datetime.datetime.now(),
                surplus_val,
            ),
        )

        if commit:
            self.db.commit()

    def import_single_card(
        self,
        set_code,
        collector_number,
        qty,
        location_id,
        condition,
        finish,
        purchase_price,
        is_tradeable,
    ):
        qty = max(1, int(qty or 1))
        import_context = self._ensure_card(set_code, collector_number, sync_prices=True)
        if not import_context:
            return {
                "success": False,
                "copies_inserted": 0,
                "fallback_price_applied": False,
            }

        scryfall_id = import_context["scryfall_id"]
        for _ in range(qty):
            self.insert_inventory_copy(
                scryfall_id=scryfall_id,
                location_id=location_id,
                condition=condition,
                finish=finish,
                purchase_price=purchase_price,
                is_tradeable=is_tradeable,
                commit=False,
            )

        self.db.commit()
        return {
            "success": True,
            "scryfall_id": scryfall_id,
            "copies_inserted": qty,
            "fallback_price_applied": import_context["fallback_price_applied"],
        }

    def import_bulk_rows(self, rows, default_location_id=1):
        result = ImportResult()
        resolved_cache = {}
        inserts_since_commit = 0

        for row in rows:
            result.rows_processed += 1
            try:
                set_code = str(row.get("set_code", "")).strip()
                collector_number = str(row.get("collector_number", "")).strip()
                if not set_code or not collector_number:
                    result.cards_failed += 1
                    continue

                qty = max(1, int(row.get("qty", 1)))
                finish = str(row.get("finish", "nonfoil")).strip().lower()
                finish = "foil" if finish == "foil" else "nonfoil"
                raw_dest = str(row.get("location", "Unsorted Box")).strip().title()
                tradeable = 1 if _is_truthy_csv_value(row.get("tradeable")) else 0

                cache_key = f"{set_code.lower()}::{collector_number}"
                import_context = resolved_cache.get(cache_key)
                if not import_context:
                    import_context = self._ensure_card(set_code, collector_number, sync_prices=True)
                    if not import_context:
                        result.cards_failed += 1
                        continue
                    resolved_cache[cache_key] = import_context
                    result.cards_resolved += 1
                    if import_context["fallback_price_applied"]:
                        result.fallback_prices_applied += 1

                scryfall_id = import_context["scryfall_id"]
                card_name = self._get_card_name(scryfall_id)

                for _ in range(qty):
                    assigned_loc_id, assigned_tradeable = self._resolve_bulk_destination(
                        set_code=set_code,
                        card_name=card_name,
                        finish=finish,
                        raw_dest=raw_dest,
                        default_location_id=default_location_id,
                        tradeable=tradeable,
                    )

                    self.insert_inventory_copy(
                        scryfall_id=scryfall_id,
                        location_id=assigned_loc_id,
                        condition="NM",
                        finish=finish,
                        purchase_price=0,
                        is_tradeable=assigned_tradeable,
                        commit=False,
                    )
                    result.copies_inserted += 1
                    inserts_since_commit += 1

                    if inserts_since_commit >= self.commit_batch_size:
                        self.db.commit()
                        inserts_since_commit = 0

            except Exception:
                result.cards_failed += 1

        if inserts_since_commit:
            self.db.commit()

        return {
            "rows_processed": result.rows_processed,
            "copies_inserted": result.copies_inserted,
            "cards_resolved": result.cards_resolved,
            "cards_failed": result.cards_failed,
            "fallback_prices_applied": result.fallback_prices_applied,
        }