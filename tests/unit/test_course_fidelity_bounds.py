"""Deterministic bounds that supplement real Deep Agent trajectory tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolCall
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import ResearchPlan, ResearchTopic
from stakeholder_intelligence_agent.errors import CourseFidelityError
from stakeholder_intelligence_agent.middleware.course_fidelity import (
    validate_researcher_wave,
)


def _researcher_call(index: int) -> ToolCall:
    call: ToolCall = {
        "name": "task",
        "args": {
            "description": f"topic_id=topic-{index} Research one topic.",
            "subagent_type": "topic-researcher",
        },
        "id": f"call-{index}",
        "type": "tool_call",
    }
    return call


@pytest.mark.parametrize("count", [1, 2, 3])
def test_approved_researcher_wave_sizes_pass(count: int) -> None:
    message = AIMessage(content="", tool_calls=[_researcher_call(i) for i in range(count)])

    validate_researcher_wave([message], maximum=3)


def test_four_researchers_in_one_wave_are_rejected() -> None:
    message = AIMessage(content="", tool_calls=[_researcher_call(i) for i in range(4)])

    with pytest.raises(CourseFidelityError):
        validate_researcher_wave([message], maximum=3)


def _topic(index: int) -> ResearchTopic:
    return ResearchTopic(
        topic_id=f"topic-{index}",
        title=f"Topic {index}",
        objective=f"Research topic {index}.",
        questions=(f"What supports topic {index}?",),
        priority=index,
    )


def test_five_research_topics_are_accepted() -> None:
    plan = ResearchPlan(
        plan_id="plan-five",
        run_id="run-five",
        engagement_id="engagement-five",
        question="What is supported across five topics?",
        topics=tuple(_topic(index) for index in range(1, 6)),
        source_strategy=("document", "interview"),
        completion_criteria=("Every claim uses registered evidence.",),
        created_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert len(plan.topics) == 5


def test_six_research_topics_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchPlan(
            plan_id="plan-six",
            run_id="run-six",
            engagement_id="engagement-six",
            question="This plan exceeds the approved topic bound.",
            topics=tuple(_topic(index) for index in range(1, 7)),
            source_strategy=("document",),
            completion_criteria=("Every claim uses registered evidence.",),
            created_at=datetime(2026, 7, 15, tzinfo=UTC),
        )
