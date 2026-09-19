"""Thin ``sqlite3`` access layer.

Deliberately not an ORM: the schema is fixed, the queries are few, and a
single file database keeps the project runnable on a plain 16GB CPU machine.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from storage.schema import (
    CLIP_COLUMNS,
    COLUMN_MIGRATIONS,
    POST_MIGRATION_STATEMENTS,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    TABLE_NAMES,
)

LOGGER = logging.getLogger(__name__)


class Database:
    """Owns the SQLite file and every connection to it."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connections -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work and commit, or roll back on error."""

        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- schema ------------------------------------------------------------
    def initialize(self) -> None:
        """Create every table/index, then apply column migrations."""

        with self.transaction() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            self._apply_column_migrations(connection)
            for statement in POST_MIGRATION_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        LOGGER.info("sqlite schema ready at %s (version %s)", self.path, SCHEMA_VERSION)

    @staticmethod
    def _apply_column_migrations(connection: sqlite3.Connection) -> int:
        """Add columns that newer versions expect (idempotent in-place upgrade)."""

        added = 0
        for table, columns in COLUMN_MIGRATIONS.items():
            existing = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column in existing:
                    continue
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                LOGGER.info("migration: added %s.%s", table, column)
                added += 1
        return added

    def table_names(self) -> list[str]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return sorted(row["name"] for row in rows)

    def missing_tables(self) -> list[str]:
        existing = set(self.table_names())
        return [name for name in TABLE_NAMES if name not in existing]

    def count(self, table: str) -> int:
        if table not in TABLE_NAMES:
            raise ValueError(f"unknown table: {table}")
        with self.transaction() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def stats(self) -> dict[str, int]:
        """Row counts per table, used by the UI footer and by ``--check``."""

        with self.transaction() as connection:
            counts = {
                name: int(connection.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
                for name in TABLE_NAMES
            }
        return counts

    # -- generic helpers ---------------------------------------------------
    def insert(self, table: str, values: dict[str, Any]) -> int:
        """Insert one row and return its id."""

        with self.transaction() as connection:
            return self._insert(connection, table, values)

    @staticmethod
    def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return int(cursor.lastrowid or 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.transaction() as connection:
            return list(connection.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, tuple(params))
            return int(cursor.rowcount)

    @staticmethod
    def clip_columns() -> tuple[str, ...]:
        return CLIP_COLUMNS
