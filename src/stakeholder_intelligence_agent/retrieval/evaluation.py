"""Versioned retrieval rank-quality metrics with explicit definitions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RankingMetrics:
    """Recall, reciprocal rank, graded nDCG, and relevant-source coverage."""

    cutoff: int
    recall: float
    mrr: float
    ndcg: float
    source_coverage: float


def evaluate_ranking(
    ranked_chunk_ids: Sequence[str],
    judgments: Mapping[str, int],
    source_by_chunk: Mapping[str, str],
    *,
    cutoff: int = 5,
) -> RankingMetrics:
    """Evaluate one ranking against positive integer relevance judgments."""
    if cutoff < 1 or not judgments or any(grade < 0 for grade in judgments.values()):
        raise ValueError
    relevant = {chunk_id for chunk_id, grade in judgments.items() if grade > 0}
    if not relevant or not relevant.issubset(source_by_chunk):
        raise ValueError
    if len(set(ranked_chunk_ids)) != len(ranked_chunk_ids):
        raise ValueError
    top = tuple(ranked_chunk_ids[:cutoff])
    retrieved_relevant = relevant.intersection(top)
    recall = len(retrieved_relevant) / len(relevant)
    first_rank = next(
        (rank for rank, chunk_id in enumerate(ranked_chunk_ids, start=1) if chunk_id in relevant),
        None,
    )
    reciprocal_rank = 0.0 if first_rank is None else 1.0 / first_rank
    dcg = sum(
        (2 ** judgments.get(chunk_id, 0) - 1) / math.log2(rank + 1)
        for rank, chunk_id in enumerate(top, start=1)
    )
    ideal_grades = sorted(judgments.values(), reverse=True)[:cutoff]
    ideal_dcg = sum(
        (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal_grades, start=1)
    )
    relevant_sources = {source_by_chunk[chunk_id] for chunk_id in relevant}
    retrieved_sources = {source_by_chunk[chunk_id] for chunk_id in retrieved_relevant}
    return RankingMetrics(
        cutoff=cutoff,
        recall=recall,
        mrr=reciprocal_rank,
        ndcg=0.0 if ideal_dcg == 0 else dcg / ideal_dcg,
        source_coverage=len(retrieved_sources) / len(relevant_sources),
    )


def macro_average(metrics: Sequence[RankingMetrics]) -> RankingMetrics:
    """Return an unweighted query-level macro average."""
    if not metrics:
        raise ValueError
    cutoffs = {item.cutoff for item in metrics}
    if len(cutoffs) != 1:
        raise ValueError("Ranking metrics must use one cutoff.")  # noqa: TRY003
    count = len(metrics)
    return RankingMetrics(
        cutoff=metrics[0].cutoff,
        recall=sum(item.recall for item in metrics) / count,
        mrr=sum(item.mrr for item in metrics) / count,
        ndcg=sum(item.ndcg for item in metrics) / count,
        source_coverage=sum(item.source_coverage for item in metrics) / count,
    )
