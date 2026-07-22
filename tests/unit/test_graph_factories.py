"""Agent Server graph factories must not assemble synchronous dependencies on-loop."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from stakeholder_intelligence_agent.insight import graph as insight_graph
from stakeholder_intelligence_agent.interview import graph as interview_graph

if TYPE_CHECKING:
    from types import ModuleType


@pytest.mark.parametrize("module", [interview_graph, insight_graph])
async def test_agent_server_graph_factory_offloads_dependency_assembly(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
) -> None:
    event_loop_thread = threading.get_ident()

    def fake_build_server_graph() -> Any:
        return threading.get_ident()

    monkeypatch.setattr(module, "_build_server_graph", fake_build_server_graph)

    factory_thread = await module.make_graph()

    assert factory_thread != event_loop_thread
