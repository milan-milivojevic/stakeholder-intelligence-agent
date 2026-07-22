"""Client-friendly interview prompt and completion-guidance checks."""

from stakeholder_intelligence_agent.interview.prompts import (
    COMPLETION_RECOMMENDATION,
    INTERVIEW_SYSTEM_PROMPT,
    completion_is_recommended,
)


def test_completion_recommendation_requires_the_exact_agent_guidance() -> None:
    assert completion_is_recommended((COMPLETION_RECOMMENDATION,)) is True
    assert completion_is_recommended(("The interview might be complete.",)) is False
    assert completion_is_recommended(()) is False


def test_prompt_makes_ai_recommendation_the_earliest_finalization_gate() -> None:
    assert "Before we finish, is there anything important" in INTERVIEW_SYSTEM_PROMPT
    assert COMPLETION_RECOMMENDATION in INTERVIEW_SYSTEM_PROMPT
    assert "unlocks the separate Finish interview action" in INTERVIEW_SYSTEM_PROMPT
    assert "assistant determines the earliest point" in INTERVIEW_SYSTEM_PROMPT
