"""Separate migrated SQLite persistence for access and business-domain records."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

from stakeholder_intelligence_agent.persistence.checkpointer import prepare_database_path
from stakeholder_intelligence_agent.persistence.migrations import (
    MIGRATIONS,
    validate_migration_inventory,
)

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
)
"""


class DomainDatabase:
    """Open short-lived configured connections to the domain SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    async def initialize(self) -> None:
        """Apply every pending migration under one immediate write lock."""
        validate_migration_inventory()
        async with self.connection() as connection:
            await connection.execute(_MIGRATION_TABLE_SQL)
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute("SELECT version FROM schema_migrations")
                rows = await cursor.fetchall()
                applied = {int(row["version"]) for row in rows}
                for migration in MIGRATIONS:
                    if migration.version in applied:
                        continue
                    for statement in migration.statements:
                        await connection.execute(statement)
                    await connection.execute(
                        """
                        INSERT INTO schema_migrations(version, name, applied_at)
                        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """,
                        (migration.version, migration.name),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def migration_versions(self) -> tuple[int, ...]:
        """Return the applied schema versions for readiness and verification."""
        async with self.connection() as connection:
            cursor = await connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            return tuple(int(row["version"]) for row in await cursor.fetchall())

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a connection with required durability and integrity controls."""
        path = await asyncio.to_thread(prepare_database_path, self.path)
        connection = await aiosqlite.connect(path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = True) -> AsyncIterator[aiosqlite.Connection]:
        """Yield an explicit transaction and roll back every exceptional exit."""
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
