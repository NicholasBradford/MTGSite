import os
import time
import sqlite3
import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "instance/cards.db")


def normalize_color_list(value):
    if value is None:
        return ""
    return "".join(value)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT
            cp.scryfall_id,
            cp.oracle_id,
            cp.set_code,
            cp.collector_number
        FROM card_printings cp
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE cd.oracle_id IS NULL
        ORDER BY cp.set_code, cp.collector_number;
    """).fetchall()

    print(f"Missing card_definitions: {len(rows)}")

    repaired = 0
    failed = []

    for row in rows:
        scryfall_id = row["scryfall_id"]
        print(f"Fetching {scryfall_id}...")

        url = f"https://api.scryfall.com/cards/{scryfall_id}"
        response = requests.get(url, timeout=20)

        if response.status_code != 200:
            failed.append((scryfall_id, response.status_code, response.text[:200]))
            continue

        card = response.json()

        oracle_id = card.get("oracle_id") or row["oracle_id"]
        if not oracle_id:
            failed.append((scryfall_id, "missing oracle_id", "No oracle_id returned"))
            continue

        cur.execute("""
            INSERT OR REPLACE INTO card_definitions (
                oracle_id,
                name,
                mana_cost,
                cmc,
                type_line,
                oracle_text,
                color,
                color_identity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            oracle_id,
            card.get("name", ""),
            card.get("mana_cost", ""),
            card.get("cmc", 0),
            card.get("type_line", ""),
            card.get("oracle_text", ""),
            normalize_color_list(card.get("colors", [])),
            normalize_color_list(card.get("color_identity", [])),
        ))

        cur.execute("""
            UPDATE card_printings
            SET oracle_id = ?
            WHERE scryfall_id = ?;
        """, (oracle_id, scryfall_id))

        repaired += 1
        time.sleep(0.08)

    conn.commit()

    remaining = cur.execute("""
        SELECT COUNT(*)
        FROM card_printings cp
        LEFT JOIN card_definitions cd
            ON cp.oracle_id = cd.oracle_id
        WHERE cd.oracle_id IS NULL;
    """).fetchone()[0]

    conn.close()

    print(f"Repaired: {repaired}")
    print(f"Remaining missing card_definitions: {remaining}")

    if failed:
        print("\nFailed rows:")
        for item in failed:
            print(item)


if __name__ == "__main__":
    main()