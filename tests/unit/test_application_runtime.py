"""Production application assembly and event-loop safety tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import get_ident
from typing import TYPE_CHECKING, Any, cast

import pytest

from stakeholder_intelligence_agent.api import dependencies

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from stakeholder_intelligence_agent.api.runtime import ApplicationServices
    from stakeholder_intelligence_agent.config import Settings

pytestmark = pytest.mark.unit


@dataclass(slots=True)
class _FakeRuntime:
    initialized_on_thread: int | None = None

    async def initialize(self) -> None:
        self.initialized_on_thread = get_ident()


async def test_production_dependency_assembly_runs_outside_event_loop(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every synchronous production constructor behind asyncio.to_thread."""
    event_loop_thread = get_ident()
    getter_threads: list[int] = []
    assembly_threads: list[int] = []
    database = object()
    access = object()
    saver = object()
    fake_runtime = _FakeRuntime()

    def settings_provider() -> Settings:
        getter_threads.append(get_ident())
        return settings

    def database_provider() -> object:
        getter_threads.append(get_ident())
        return database

    def access_provider() -> object:
        getter_threads.append(get_ident())
        return access

    def assemble(
        supplied_settings: object,
        supplied_database: object,
        supplied_access: object,
        supplied_saver: object,
    ) -> ApplicationServices:
        assembly_threads.append(get_ident())
        assert supplied_settings is settings
        assert supplied_database is database
        assert supplied_access is access
        assert supplied_saver is saver
        return cast("ApplicationServices", cast("Any", fake_runtime))

    @asynccontextmanager
    async def checkpointer(_: object) -> AsyncIterator[object]:
        yield saver

    monkeypatch.setattr(dependencies, "get_settings", settings_provider)
    monkeypatch.setattr(dependencies, "get_domain_database", database_provider)
    monkeypatch.setattr(dependencies, "get_access_service", access_provider)
    monkeypatch.setattr(dependencies, "_assemble_application_services", assemble)
    monkeypatch.setattr(dependencies, "open_sqlite_checkpointer", checkpointer)

    async with dependencies.open_application_services() as runtime:
        assert id(runtime) == id(fake_runtime)

    assert getter_threads
    assert all(thread_id != event_loop_thread for thread_id in getter_threads)
    assert assembly_threads
    assert assembly_threads[0] != event_loop_thread
    assert fake_runtime.initialized_on_thread == event_loop_thread
