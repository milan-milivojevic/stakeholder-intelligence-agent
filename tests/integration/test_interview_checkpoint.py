"""Persistent interview-thread execution with a deterministic offline model."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.interview.graph import build_interview_graph
from stakeholder_intelligence_agent.persistence.checkpointer import (
    open_sqlite_checkpointer,
)
from tests.fakes import ToolCallingFakeModel
from tests.helpers import interview_context

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


@pytest.mark.integration
async def test_interview_continues_from_sqlite_thread(
    settings: Settings,
    tmp_path: Path,
) -> None:
    context = interview_context()
    first_model = ToolCallingFakeModel(
        responses=[AIMessage(content="What part of the current handoff do you own?")]
    )
    thread_id = context.access.thread_id
    assert thread_id is not None
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    checkpoint_path = tmp_path / "interview.sqlite3"
    async with open_sqlite_checkpointer(checkpoint_path) as saver:
        first_graph = build_interview_graph(
            settings,
            primary_model=first_model,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Fallback response.")]
            ),
            checkpointer=saver,
        )
        await first_graph.ainvoke(
            {"messages": [HumanMessage(content="Start the interview.")]},
            config=config,
            context=context,
        )

    second_model = ToolCallingFakeModel(
        responses=[AIMessage(content="Where does that handoff most often break down?")]
    )
    async with open_sqlite_checkpointer(checkpoint_path) as restarted_saver:
        restarted_graph = build_interview_graph(
            settings,
            primary_model=second_model,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Restart fallback response.")]
            ),
            checkpointer=restarted_saver,
        )
        result = await restarted_graph.ainvoke(
            {"messages": [HumanMessage(content="The handoff is partly manual.")]},
            config=config,
            context=context,
        )

    message_text = [message.text for message in result["messages"]]
    assert "Start the interview." in message_text
    assert "The handoff is partly manual." in message_text
    assert "What part of the current handoff do you own?" in message_text
    assert "Where does that handoff most often break down?" in message_text
    assert first_model.call_count == 1
    assert second_model.call_count == 1
