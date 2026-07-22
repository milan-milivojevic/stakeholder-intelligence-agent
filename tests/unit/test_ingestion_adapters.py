"""Regression tests for production Gemini/FastEmbed adapter normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from stakeholder_intelligence_agent.ingestion.adapters import GeminiBm25Vectorizer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pytest_mock import MockerFixture

    from stakeholder_intelligence_agent.config import Settings


@dataclass(frozen=True)
class _Array:
    values: tuple[int | float, ...]

    def tolist(self) -> list[int | float]:
        return list(self.values)


@dataclass(frozen=True)
class _SparseResult:
    indices: _Array
    values: _Array


class _SparseModel:
    def embed(self, texts: Sequence[str]) -> tuple[_SparseResult, ...]:
        return tuple(self._result() for _text in texts)

    def query_embed(self, text: str) -> tuple[_SparseResult, ...]:
        del text
        return (self._result(),)

    @staticmethod
    def _result() -> _SparseResult:
        return _SparseResult(
            indices=_Array((19, 3, 11)),
            values=_Array((0.19, 0.03, 0.11)),
        )


class _DocumentEmbeddings:
    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * self._dimension for _text in texts]


class _QueryEmbeddings:
    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    async def aembed_query(self, text: str) -> list[float]:
        del text
        return [0.5] * self._dimension


@pytest.mark.unit
async def test_fastembed_sparse_output_is_sorted_without_detaching_values(
    settings: Settings,
    mocker: MockerFixture,
) -> None:
    vectorizer = GeminiBm25Vectorizer(settings)
    sparse = _SparseModel()

    async def get_sparse() -> Any:
        return sparse

    mocker.patch.object(
        vectorizer,
        "_dense",
        _DocumentEmbeddings(settings.gemini_embedding_dimension),
    )
    mocker.patch.object(
        vectorizer,
        "_query_dense",
        _QueryEmbeddings(settings.gemini_embedding_dimension),
    )
    mocker.patch.object(vectorizer, "_get_sparse", get_sparse)

    document_pair = (await vectorizer.vectorize(("document evidence",)))[0]
    query_pair = await vectorizer.vectorize_query("evidence question")

    for pair in (document_pair, query_pair):
        assert pair.sparse.indices == (3, 11, 19)
        assert pair.sparse.values == (0.03, 0.11, 0.19)
