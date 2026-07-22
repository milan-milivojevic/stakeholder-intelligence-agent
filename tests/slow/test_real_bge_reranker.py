"""Real local execution of the mandatory BAAI BGE cross-encoder."""

import pytest

from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.retrieval import BgeReranker

pytestmark = [pytest.mark.slow, pytest.mark.timeout(900)]


async def test_real_required_bge_model_scores_fused_candidates_on_recorded_device(
    settings: Settings,
) -> None:
    reranker = BgeReranker(settings, local_files_only=True)

    result = await reranker.rerank(
        "What operational risk is caused by unclear approval ownership?",
        (
            "No one owns final approval after the regional handoff, so work waits overnight.",
            "The escalation owner is blank in the responsibility matrix.",
            "The office lease review is scheduled for the fourth quarter.",
        ),
    )

    assert result.model_id == "BAAI/bge-reranker-base"
    assert reranker.revision == "2cfc18c9415c912f9d8155881c133215df768a70"
    assert result.device in {"cpu", "cuda"}
    assert len(result.scores) == 3
    assert result.scores[0] > result.scores[2]
    assert result.duration_ms > 0
