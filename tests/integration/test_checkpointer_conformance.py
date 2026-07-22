"""Official base-conformance validation for the custom SQLite saver."""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.conformance import checkpointer_test, validate

from stakeholder_intelligence_agent.persistence.checkpointer import (
    open_sqlite_checkpointer,
)


@pytest.mark.integration
async def test_custom_sqlite_checkpointer_passes_official_base_conformance(
    tmp_path: Path,
) -> None:
    async def provider() -> AsyncGenerator[BaseCheckpointSaver[Any], None]:
        async with open_sqlite_checkpointer(tmp_path / "conformance.sqlite3") as saver:
            yield saver

    registered = checkpointer_test(name="ProjectAsyncSqliteSaver")(provider)
    report = await validate(registered)

    assert report.passed_all_base(), report.to_dict()
    assert report.results["delete_for_runs"].passed is True, report.to_dict()
    assert report.results["prune"].passed is True, report.to_dict()
    assert report.passed_all(), report.to_dict()
