"""Required BAAI/bge-reranker-base cross-encoder adapter."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import torch
from huggingface_hub import snapshot_download
from sentence_transformers import CrossEncoder

from stakeholder_intelligence_agent.errors import RerankingError
from stakeholder_intelligence_agent.retrieval.types import RerankResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stakeholder_intelligence_agent.config import Settings


_REQUIRED_SNAPSHOT_FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
_LOGGER = logging.getLogger(__name__)


class BgeReranker:
    """Lazily load and execute the mandatory BGE reranker on CUDA or CPU."""

    def __init__(self, settings: Settings, *, local_files_only: bool = False) -> None:
        if settings.reranker_model != "BAAI/bge-reranker-base":
            raise RerankingError
        self._model_id = settings.reranker_model
        self._revision = settings.reranker_revision
        self._cache = str(settings.model_cache_root / "sentence-transformers")
        self._batch_size = settings.reranker_batch_size
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._local_files_only = local_files_only
        self._model: CrossEncoder | None = None
        self._model_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()

    @property
    def model_id(self) -> str:
        """Return the exact required model identity for safe evaluation records."""
        return self._model_id

    @property
    def device(self) -> str:
        """Return the selected execution device without loading model weights."""
        return self._device

    @property
    def revision(self) -> str:
        """Return the immutable Hugging Face snapshot revision."""
        return self._revision

    async def rerank(self, query: str, texts: Sequence[str]) -> RerankResult:
        """Score every query-text pair and fail instead of bypassing reranking."""
        if not query.strip() or not texts or any(not text.strip() for text in texts):
            raise RerankingError
        started = perf_counter()
        stage = "model_load"
        try:
            model = await self._get_model()
            stage = "prediction"
            async with self._inference_lock:
                raw = await asyncio.to_thread(
                    self._predict,
                    model,
                    [(query, text) for text in texts],
                )
            stage = "score_validation"
            values = np.asarray(raw, dtype=float).reshape(-1)
        except RerankingError:
            raise
        except Exception as error:
            _LOGGER.error(  # noqa: TRY400 - never log provider/model exception detail
                "BGE reranking failed at stage=%s error_type=%s.",
                stage,
                type(error).__name__,
            )
            raise RerankingError from error
        scores = tuple(float(value) for value in values.tolist())
        if len(scores) != len(texts) or any(not math.isfinite(value) for value in scores):
            raise RerankingError
        return RerankResult(
            scores=scores,
            model_id=self._model_id,
            device=self._device,
            duration_ms=(perf_counter() - started) * 1_000,
        )

    async def _get_model(self) -> CrossEncoder:
        if self._model is not None:
            return self._model
        async with self._model_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> CrossEncoder:
        snapshot_path = snapshot_download(
            repo_id=self._model_id,
            revision=self._revision,
            cache_dir=self._cache,
            local_files_only=self._local_files_only,
            allow_patterns=list(_REQUIRED_SNAPSHOT_FILES),
        )
        kwargs: dict[str, Any] = {
            "device": self._device,
            "trust_remote_code": False,
            "local_files_only": True,
        }
        return cast("CrossEncoder", CrossEncoder(snapshot_path, **kwargs))

    def _predict(self, model: CrossEncoder, pairs: list[tuple[str, str]]) -> Any:
        predict = cast("Any", model.predict)
        with torch.inference_mode():
            return predict(
                pairs,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
