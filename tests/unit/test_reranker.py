"""Regression coverage for the bounded BGE reranker snapshot."""

import asyncio
import time
from pathlib import Path
from threading import Lock
from typing import Any

from stakeholder_intelligence_agent.retrieval import reranker as reranker_module
from stakeholder_intelligence_agent.retrieval.reranker import BgeReranker


def test_bge_loader_downloads_only_the_required_safetensors_snapshot(
    settings: Any,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Do not fetch duplicate PyTorch or ONNX weights during a live insight run."""
    snapshot_calls: list[dict[str, Any]] = []
    encoder_calls: list[tuple[Path, dict[str, Any]]] = []

    def fake_snapshot_download(**kwargs: Any) -> str:
        snapshot_calls.append(kwargs)
        return str(tmp_path)

    def fake_cross_encoder(path: str, **kwargs: Any) -> object:
        encoder_calls.append((Path(path), kwargs))
        return object()

    monkeypatch.setattr(reranker_module, "snapshot_download", fake_snapshot_download)
    monkeypatch.setattr(reranker_module, "CrossEncoder", fake_cross_encoder)

    reranker = BgeReranker(settings)
    loaded = reranker._load_model()  # noqa: SLF001

    assert loaded is not None
    assert len(snapshot_calls) == 1
    allowed = snapshot_calls[0]["allow_patterns"]
    assert "model.safetensors" in allowed
    assert "pytorch_model.bin" not in allowed
    assert not any("onnx" in pattern for pattern in allowed)
    assert encoder_calls == [
        (
            tmp_path,
            {
                "device": reranker.device,
                "trust_remote_code": False,
                "local_files_only": True,
            },
        )
    ]


async def test_shared_bge_model_serializes_concurrent_predictions(
    settings: Any,
    monkeypatch: Any,
) -> None:
    """Parallel researchers must not execute one CrossEncoder concurrently."""
    state_lock = Lock()
    active = 0
    maximum_active = 0

    async def fake_get_model(self: BgeReranker) -> object:
        del self
        return object()

    def fake_predict(
        self: BgeReranker,
        model: object,
        pairs: list[tuple[str, str]],
    ) -> list[float]:
        nonlocal active, maximum_active
        del self, model
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return [0.5] * len(pairs)

    monkeypatch.setattr(BgeReranker, "_get_model", fake_get_model)
    monkeypatch.setattr(BgeReranker, "_predict", fake_predict)
    reranker = BgeReranker(settings)

    results = await asyncio.gather(
        reranker.rerank("query one", ("evidence one",)),
        reranker.rerank("query two", ("evidence two",)),
    )

    assert len(results) == 2
    assert maximum_active == 1
