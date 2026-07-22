"""Agent Server custom SQLite checkpointer lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from stakeholder_intelligence_agent.config import get_settings

_UNSUPPORTED_PRUNE_STRATEGY = "Unsupported checkpoint prune strategy."


class MaintainedAsyncSqliteSaver(AsyncSqliteSaver):
    """SQLite saver with Agent Server rollback and retention maintenance hooks."""

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        """Delete checkpoints and writes whose serialized metadata belongs to a run."""
        normalized = tuple(dict.fromkeys(str(run_id) for run_id in run_ids))
        if not normalized:
            return
        await self.setup()
        async with self.lock, self.conn.cursor() as cursor:
            try:
                for run_id in normalized:
                    await cursor.execute(
                        """
                        DELETE FROM writes
                        WHERE EXISTS (
                            SELECT 1 FROM checkpoints
                            WHERE checkpoints.thread_id = writes.thread_id
                                AND checkpoints.checkpoint_ns = writes.checkpoint_ns
                                AND checkpoints.checkpoint_id = writes.checkpoint_id
                                AND CASE
                                    WHEN json_valid(CAST(checkpoints.metadata AS TEXT))
                                    THEN json_extract(
                                        CAST(checkpoints.metadata AS TEXT), '$.run_id'
                                    )
                                END = ?
                        )
                        """,
                        (run_id,),
                    )
                    await cursor.execute(
                        """
                        DELETE FROM checkpoints
                        WHERE CASE
                            WHEN json_valid(CAST(metadata AS TEXT))
                            THEN json_extract(CAST(metadata AS TEXT), '$.run_id')
                        END = ?
                        """,
                        (run_id,),
                    )
                await cursor.execute(
                    """
                    UPDATE checkpoints AS child
                    SET parent_checkpoint_id = NULL
                    WHERE parent_checkpoint_id IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM checkpoints AS parent
                            WHERE parent.thread_id = child.thread_id
                                AND parent.checkpoint_ns = child.checkpoint_ns
                                AND parent.checkpoint_id = child.parent_checkpoint_id
                        )
                    """
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def aprune(
        self,
        thread_ids: Sequence[str],
        *,
        strategy: str = "keep_latest",
    ) -> None:
        """Keep one latest checkpoint per namespace or delete selected threads."""
        normalized = tuple(dict.fromkeys(str(thread_id) for thread_id in thread_ids))
        if not normalized:
            return
        if strategy in {"delete", "delete_all"}:
            for thread_id in normalized:
                await self.adelete_thread(thread_id)
            return
        if strategy != "keep_latest":
            raise ValueError(_UNSUPPORTED_PRUNE_STRATEGY)

        await self.setup()
        async with self.lock, self.conn.cursor() as cursor:
            try:
                for thread_id in normalized:
                    await cursor.execute(
                        """
                        DELETE FROM writes
                        WHERE thread_id = ? AND EXISTS (
                            SELECT 1 FROM checkpoints
                            WHERE checkpoints.thread_id = writes.thread_id
                                AND checkpoints.checkpoint_ns = writes.checkpoint_ns
                                AND checkpoints.checkpoint_id = writes.checkpoint_id
                                AND checkpoints.checkpoint_id <> (
                                    SELECT MAX(latest.checkpoint_id)
                                    FROM checkpoints AS latest
                                    WHERE latest.thread_id = checkpoints.thread_id
                                        AND latest.checkpoint_ns = checkpoints.checkpoint_ns
                                )
                        )
                        """,
                        (thread_id,),
                    )
                    await cursor.execute(
                        """
                        DELETE FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_id <> (
                            SELECT MAX(latest.checkpoint_id)
                            FROM checkpoints AS latest
                            WHERE latest.thread_id = checkpoints.thread_id
                                AND latest.checkpoint_ns = checkpoints.checkpoint_ns
                        )
                        """,
                        (thread_id,),
                    )
                    await cursor.execute(
                        """
                        UPDATE checkpoints SET parent_checkpoint_id = NULL
                        WHERE thread_id = ?
                        """,
                        (thread_id,),
                    )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise


def prepare_database_path(database: Path) -> Path:
    """Resolve the database and synchronously create its local parent."""
    resolved = database.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@asynccontextmanager
async def open_sqlite_checkpointer(database: Path) -> AsyncIterator[MaintainedAsyncSqliteSaver]:
    """Open, initialize, yield, and close one SQLite saver."""
    resolved = await asyncio.to_thread(prepare_database_path, database)
    async with MaintainedAsyncSqliteSaver.from_conn_string(str(resolved)) as raw_saver:
        saver = cast("MaintainedAsyncSqliteSaver", raw_saver)
        await saver.setup()
        yield saver


@asynccontextmanager
async def generate_checkpointer() -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Provide the custom saver expected by LangGraph Agent Server."""
    settings = await asyncio.to_thread(get_settings)
    async with open_sqlite_checkpointer(settings.checkpoint_database) as saver:
        yield saver
