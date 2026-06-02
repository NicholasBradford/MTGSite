from flask import Blueprint, request, redirect, url_for, render_template
from db.db_manager import CardDB
from datetime import datetime, timedelta

sets_bp = Blueprint('sets', __name__)


@sets_bp.route('/sets', methods=['GET', 'POST'])
def set_gallery():
    manager = CardDB()

    query = """
        WITH target_cards AS (
            SELECT
                s.set_code,
                cp.oracle_id
            FROM sets s
            JOIN card_printings cp
                ON cp.set_code = s.set_code
            WHERE s.standard_legal = 1
              AND s.set_type NOT IN ('promo', 'token', 'memorabilia')
              AND cp.oracle_id IS NOT NULL
            GROUP BY s.set_code, cp.oracle_id
        ),

        binder_locations AS (
            SELECT
                s.set_code,
                l.location_id AS binder_location_id,
                l.name AS binder_name
            FROM sets s
            LEFT JOIN locations l
                ON LOWER(l.name) = LOWER(s.set_code || ' Master Set Binder')
            WHERE s.standard_legal = 1
        ),

        binder_counts AS (
            SELECT
                tc.set_code,
                tc.oracle_id,
                COUNT(i.instance_id) AS binder_count
            FROM target_cards tc
            LEFT JOIN binder_locations bl
                ON bl.set_code = tc.set_code
            LEFT JOIN inventory i
                ON i.location_id = bl.binder_location_id
            LEFT JOIN card_printings cp_inv
                ON cp_inv.scryfall_id = i.scryfall_id
               AND cp_inv.oracle_id = tc.oracle_id
            GROUP BY tc.set_code, tc.oracle_id
        ),

        total_counts AS (
            SELECT
                tc.set_code,
                tc.oracle_id,
                COUNT(i.instance_id) AS total_owned
            FROM target_cards tc
            LEFT JOIN inventory i
                ON 1 = 1
            LEFT JOIN card_printings cp_inv
                ON cp_inv.scryfall_id = i.scryfall_id
               AND cp_inv.oracle_id = tc.oracle_id
            GROUP BY tc.set_code, tc.oracle_id
        ),

        set_completion AS (
            SELECT
                tc.set_code,
                COUNT(tc.oracle_id) AS target_card_count,

                SUM(
                    CASE
                        WHEN COALESCE(bc.binder_count, 0) >= 4 THEN 1
                        ELSE 0
                    END
                ) AS binder_complete_count,

                SUM(
                    CASE
                        WHEN COALESCE(bc.binder_count, 0) < 4
                         AND COALESCE(tc_total.total_owned, 0) >= 4
                        THEN 1
                        ELSE 0
                    END
                ) AS completable_elsewhere_count,

                SUM(COALESCE(bc.binder_count, 0)) AS binder_copy_count,

                SUM(
                    CASE
                        WHEN COALESCE(bc.binder_count, 0) < 4
                        THEN 4 - COALESCE(bc.binder_count, 0)
                        ELSE 0
                    END
                ) AS binder_missing_copies,

                SUM(
                    CASE
                        WHEN COALESCE(bc.binder_count, 0) < 4
                         AND COALESCE(tc_total.total_owned, 0) >= 4
                        THEN 4 - COALESCE(bc.binder_count, 0)
                        ELSE 0
                    END
                ) AS movable_copies_needed
            FROM target_cards tc
            LEFT JOIN binder_counts bc
                ON bc.set_code = tc.set_code
               AND bc.oracle_id = tc.oracle_id
            LEFT JOIN total_counts tc_total
                ON tc_total.set_code = tc.set_code
               AND tc_total.oracle_id = tc.oracle_id
            GROUP BY tc.set_code
        ),

        set_values AS (
            SELECT
                s.set_code,
                SUM(
                    CASE
                        WHEN i.finish = 'foil'
                          OR i.finish = 'etched'
                          OR i.finish = 'rainbow_foil'
                        THEN COALESCE(cp_val.current_price_foil, 0)
                        ELSE COALESCE(cp_val.current_price, 0)
                    END
                ) AS set_value
            FROM sets s
            JOIN card_printings cp_val
                ON cp_val.set_code = s.set_code
                OR cp_val.set_code = 'p' || s.set_code
            JOIN inventory i
                ON i.scryfall_id = cp_val.scryfall_id
            GROUP BY s.set_code
        )

        SELECT
            s.*,
            COALESCE(sv.set_value, 0) AS set_value,

            bl.binder_location_id,
            bl.binder_name,

            COALESCE(sc.target_card_count, 0) AS target_card_count,
            COALESCE(sc.binder_complete_count, 0) AS binder_complete_count,
            COALESCE(sc.completable_elsewhere_count, 0) AS completable_elsewhere_count,
            COALESCE(sc.binder_copy_count, 0) AS binder_copy_count,
            COALESCE(sc.binder_missing_copies, 0) AS binder_missing_copies,
            COALESCE(sc.movable_copies_needed, 0) AS movable_copies_needed,

            CASE
                WHEN s.standard_legal = 1
                 AND COALESCE(sc.target_card_count, 0) > 0
                THEN ROUND(
                    100.0 * COALESCE(sc.binder_complete_count, 0)
                    / sc.target_card_count,
                    1
                )
                ELSE NULL
            END AS binder_completion_percent

        FROM sets s
        LEFT JOIN set_values sv
            ON sv.set_code = s.set_code
        LEFT JOIN binder_locations bl
            ON bl.set_code = s.set_code
        LEFT JOIN set_completion sc
            ON sc.set_code = s.set_code

        WHERE EXISTS (
            SELECT 1
            FROM inventory i
            JOIN card_printings cp
                ON i.scryfall_id = cp.scryfall_id
            WHERE cp.set_code = s.set_code
        )
        AND s.set_type NOT IN ('promo', 'token', 'memorabilia')

        ORDER BY s.standard_legal DESC, s.released_at DESC
    """

    sets = manager.cursor.execute(query).fetchall()
    manager.close()

    return render_template('set_gallery.html', sets=sets)


@sets_bp.route('/set/<set_code>')
def set_detail(set_code):
    manager = CardDB()

    query_set_info = """
        SELECT
            s.set_code,
            s.set_name,
            s.standard_legal,
            l.location_id AS binder_location_id,
            l.name AS binder_name
        FROM sets s
        LEFT JOIN locations l
            ON LOWER(l.name) = LOWER(s.set_code || ' Master Set Binder')
        WHERE s.set_code = ?
    """

    query = """
        WITH target_cards AS (
            SELECT
                cp.oracle_id,
                MIN(cp.scryfall_id) AS display_scryfall_id
            FROM card_printings cp
            WHERE cp.set_code = ?
              AND cp.oracle_id IS NOT NULL
            GROUP BY cp.oracle_id
        ),

        display_printings AS (
            SELECT
                tc.oracle_id,
                cp.scryfall_id,
                cp.collector_number,
                cp.image_url,
                cd.name
            FROM target_cards tc
            JOIN card_printings cp
                ON cp.scryfall_id = (
                    SELECT inner_cp.scryfall_id
                    FROM card_printings inner_cp
                    WHERE inner_cp.oracle_id = tc.oracle_id
                      AND inner_cp.set_code = ?
                    ORDER BY
                        LENGTH(inner_cp.collector_number) ASC,
                        inner_cp.collector_number ASC
                    LIMIT 1
                )
            JOIN card_definitions cd
                ON cd.oracle_id = tc.oracle_id
        ),

        binder_location AS (
            SELECT l.location_id
            FROM locations l
            WHERE LOWER(l.name) = LOWER(? || ' Master Set Binder')
            LIMIT 1
        ),

        binder_counts AS (
            SELECT
                dp.oracle_id,
                COUNT(cp_inv.scryfall_id) AS binder_count
            FROM display_printings dp
            LEFT JOIN binder_location bl
                ON 1 = 1
            LEFT JOIN inventory i
                ON i.location_id = bl.location_id
            LEFT JOIN card_printings cp_inv
                ON cp_inv.scryfall_id = i.scryfall_id
               AND cp_inv.oracle_id = dp.oracle_id
            GROUP BY dp.oracle_id
        ),

        total_counts AS (
            SELECT
                dp.oracle_id,
                COUNT(cp_inv.scryfall_id) AS total_owned
            FROM display_printings dp
            LEFT JOIN inventory i
                ON 1 = 1
            LEFT JOIN card_printings cp_inv
                ON cp_inv.scryfall_id = i.scryfall_id
               AND cp_inv.oracle_id = dp.oracle_id
            GROUP BY dp.oracle_id
        ),
        
        location_summary AS (
            SELECT
                dp.oracle_id,
                GROUP_CONCAT(
                    loc.name || ': ' || location_counts.qty,
                    ' | '
                ) AS location_tooltip
            FROM display_printings dp
            JOIN binder_counts bc
                ON bc.oracle_id = dp.oracle_id
            AND COALESCE(bc.binder_count, 0) < 4
            JOIN (
                SELECT
                    cp_inv.oracle_id,
                    i.location_id,
                    COUNT(i.instance_id) AS qty
                FROM inventory i
                JOIN card_printings cp_inv
                    ON cp_inv.scryfall_id = i.scryfall_id
                LEFT JOIN binder_location bl
                    ON 1 = 1
                WHERE bl.location_id IS NULL
                OR i.location_id != bl.location_id
                GROUP BY cp_inv.oracle_id, i.location_id
            ) location_counts
                ON location_counts.oracle_id = dp.oracle_id
            JOIN locations loc
                ON loc.location_id = location_counts.location_id
            GROUP BY dp.oracle_id
        )

        SELECT
            dp.name,
            dp.collector_number,
            dp.image_url,
            dp.scryfall_id,

            COALESCE(bc.binder_count, 0) AS binder_count,
            COALESCE(tc.total_owned, 0) AS total_owned,
            COALESCE(ls.location_tooltip, '') AS location_tooltip,

            CASE
                WHEN COALESCE(bc.binder_count, 0) >= 4 THEN 1
                ELSE 0
            END AS has_4x_in_binder,

            CASE
                WHEN COALESCE(bc.binder_count, 0) < 4
                 AND COALESCE(tc.total_owned, 0) >= 4
                THEN 1
                ELSE 0
            END AS can_complete_from_elsewhere,

            CASE
                WHEN COALESCE(bc.binder_count, 0) < 4
                THEN 4 - COALESCE(bc.binder_count, 0)
                ELSE 0
            END AS binder_missing_count,

            CASE
                WHEN COALESCE(tc.total_owned, 0) - COALESCE(bc.binder_count, 0) > 0
                THEN COALESCE(tc.total_owned, 0) - COALESCE(bc.binder_count, 0)
                ELSE 0
            END AS elsewhere_count

        FROM display_printings dp
        LEFT JOIN binder_counts bc
            ON bc.oracle_id = dp.oracle_id
        LEFT JOIN total_counts tc
            ON tc.oracle_id = dp.oracle_id
        LEFT JOIN location_summary ls
            ON ls.oracle_id = dp.oracle_id

        ORDER BY
            LENGTH(dp.collector_number) ASC,
            dp.collector_number ASC
    """

    set_info = manager.cursor.execute(query_set_info, (set_code,)).fetchone()
    cards = manager.cursor.execute(query, (set_code, set_code, set_code)).fetchall()

    manager.close()

    return render_template(
        'set_detail.html',
        cards=cards,
        set=set_info,
        set_code=set_code
    )