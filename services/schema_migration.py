# services/schema_migrations.py

from pathlib import Path
import sqlite3
import os
import datetime


MIGRATIONS = []


def migration(version: str):
    def decorator(func):
        MIGRATIONS.append((version, func))
        return func
    return decorator


def default_db_path() -> str:
    return os.environ.get("DB_PATH", "var/data/mtgsite.db")


def backup_database(db_path: str) -> Path | None:
    db_file = Path(db_path)

    if not db_file.exists():
        return None

    backup_dir = db_file.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"{db_file.stem}_pre_migration_{stamp}{db_file.suffix}"

    source = sqlite3.connect(str(db_file))
    destination = sqlite3.connect(str(backup_file))

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    return backup_file


def ensure_migration_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def get_applied_migrations(conn: sqlite3.Connection) -> set[str]:
    ensure_migration_table(conn)

    rows = conn.execute("""
        SELECT version
        FROM schema_migrations
    """).fetchall()

    return {row[0] for row in rows}


def run_migrations(db_path: str | None = None) -> dict:
    db_path = db_path or default_db_path()

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")

        ensure_migration_table(conn)
        applied = get_applied_migrations(conn)

        pending = [
            (version, func)
            for version, func in MIGRATIONS
            if version not in applied
        ]

        if not pending:
            return {
                "updated": False,
                "message": "No database migrations needed.",
                "applied": [],
            }

        backup_file = backup_database(db_path)

        applied_now = []

        for version, func in pending:
            with conn:
                func(conn)
                conn.execute("""
                    INSERT INTO schema_migrations (version)
                    VALUES (?)
                """, (version,))

            applied_now.append(version)

        return {
            "updated": True,
            "message": "Database migrations applied.",
            "backup": str(backup_file) if backup_file else None,
            "applied": applied_now,
        }

    finally:
        conn.close()


@migration("2026_07_04_001_app_settings")
def create_app_settings(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'string',
            category TEXT NOT NULL DEFAULT 'general',
            label TEXT NOT NULL,
            description TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    defaults = [
        (
            "autosorter.enabled",
            "false",
            "bool",
            "features",
            "Inventory Sorter",
            "Enable or disable the automatic inventory sorter."
        ),
        (
            "prices.tcgcsv.enabled",
            "true",
            "bool",
            "prices",
            "Use TCGCSV Pricing",
            "Use local TCGCSV price files as the primary price source."
        ),
        (
            "prices.refresh_after_local_time",
            "15:00",
            "time",
            "prices",
            "Daily Price Refresh Time",
            "After this local time, the app may attempt to fetch the current day price file."
        ),
        (
            "ui.default_view_mode",
            "grid",
            "string",
            "ui",
            "Default Inventory View",
            "Default view mode for inventory pages."
        ),
        (
            "trades.public_enabled",
            "true",
            "bool",
            "trades",
            "Public Trades Page",
            "Enable or disable the public trade browsing page."
        ),
    ]

    conn.executemany("""
        INSERT OR IGNORE INTO app_settings (
            setting_key,
            setting_value,
            value_type,
            category,
            label,
            description
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, defaults)