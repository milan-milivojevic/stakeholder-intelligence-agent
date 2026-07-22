"""Evaluation metric definitions and versioned dataset integrity."""

import json
from pathlib import Path

import pytest

from stakeholder_intelligence_agent.retrieval.evaluation import (
    RankingMetrics,
    evaluate_ranking,
    macro_average,
)

pytestmark = pytest.mark.evaluation

DATASET = Path(__file__).parents[1] / "fixtures" / "retrieval" / "evaluation-v1.json"


def test_metric_definitions_use_binary_recall_mrr_graded_ndcg_and_source_coverage() -> None:
    metrics = evaluate_ranking(
        ("irrelevant", "relevant-a", "relevant-b"),
        {"relevant-a": 3, "relevant-b": 1, "missed": 2},
        {
            "relevant-a": "source-a",
            "relevant-b": "source-a",
            "missed": "source-b",
            "irrelevant": "source-c",
        },
    )

    assert metrics.cutoff == 5
    assert metrics.recall == pytest.approx(2 / 3)
    assert metrics.mrr == pytest.approx(1 / 2)
    assert 0 < metrics.ndcg < 1
    assert metrics.source_coverage == pytest.approx(1 / 2)

    average = macro_average((metrics, RankingMetrics(5, 1.0, 1.0, 1.0, 1.0)))
    assert average.recall == pytest.approx(5 / 6)


def test_macro_average_rejects_mixed_cutoffs() -> None:
    with pytest.raises(ValueError, match="one cutoff"):
        macro_average(
            (
                RankingMetrics(5, 1.0, 1.0, 1.0, 1.0),
                RankingMetrics(10, 1.0, 1.0, 1.0, 1.0),
            )
        )


def test_versioned_dataset_has_unique_rankings_and_complete_relevance_sources() -> None:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    documents = {item["chunkId"]: item for item in dataset["documents"]}

    assert dataset["datasetId"] == "stakeholder-retrieval-evaluation-v1"
    assert dataset["version"] == 1
    assert len(documents) == len(dataset["documents"])
    assert len(dataset["queries"]) == 5
    for query in dataset["queries"]:
        dense = query["denseRanking"]
        sparse = query["sparseRanking"]
        judgments = query["judgments"]
        assert len(dense) == len(set(dense))
        assert len(sparse) == len(set(sparse))
        assert set(dense) | set(sparse) | set(judgments) <= set(documents)
        assert any(grade > 0 for grade in judgments.values())
