"""Evaluate course-aligned hybrid retrieval with the mandatory real BGE reranker."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

from docx import Document
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.config import PROJECT_ROOT, Settings
from stakeholder_intelligence_agent.contracts.retrieval import RetrievalFilter
from stakeholder_intelligence_agent.contracts.source import (
    DocumentSource,
    DocumentVersion,
    SearchChunk,
    SourceElement,
)
from stakeholder_intelligence_agent.ingestion.adapters import GeminiBm25Vectorizer
from stakeholder_intelligence_agent.ingestion.docling_adapter import DoclingExtractor
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.ingestion.normalization import (
    build_chunk_seeds,
    materialize_chunks,
)
from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
from stakeholder_intelligence_agent.retrieval.evaluation import (
    RankingMetrics,
    evaluate_ranking,
    macro_average,
)
from stakeholder_intelligence_agent.retrieval.qdrant import QdrantHybridSearcher
from stakeholder_intelligence_agent.retrieval.reranker import BgeReranker


class _InvalidFusionPayloadError(TypeError):
    """Raised when the local native-RRF evaluation receives malformed payload."""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "tests/fixtures/retrieval/evaluation-v1.json",
    )
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument(
        "--live-pipeline",
        action="store_true",
        help=(
            "Run generated DOCX inputs through Docling, normalization, real Gemini/BM25, "
            "Qdrant RRF, and BGE."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _settings() -> Settings:
    data_root = PROJECT_ROOT / ".cache/evaluation-data"
    return Settings(
        environment="test",
        google_api_key=SecretStr("offline-provider-not-called"),
        gemini_primary_chat_model="gemini-offline-primary",
        gemini_fallback_chat_model="gemini-offline-fallback",
        gemini_vision_model="gemini-offline-vision",
        gemini_embedding_model="gemini-offline-embedding",
        pm_bootstrap_token=SecretStr("p" * 32),
        token_pepper=SecretStr("t" * 32),
        data_root=data_root,
        domain_database=data_root / "domain.sqlite3",
        checkpoint_database=data_root / "checkpoints.sqlite3",
        originals_root=data_root / "originals",
        derived_root=data_root / "derived",
        agent_artifacts_root=data_root / "agent-artifacts",
        audit_root=data_root / "audit",
    )


async def _native_rrf(
    dense_ranking: list[str],
    sparse_ranking: list[str],
    *,
    limit: int,
) -> tuple[tuple[tuple[str, float], ...], float]:
    """Execute the same Qdrant-native RRF used by the production retrieval path."""
    client = AsyncQdrantClient(location=":memory:")
    collection = "native-rrf-evaluation"
    try:
        await client.create_collection(
            collection_name=collection,
            vectors_config={"dense": models.VectorParams(size=1, distance=models.Distance.DOT)},
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        dense_scores = {
            chunk_id: float(len(dense_ranking) - rank + 1)
            for rank, chunk_id in enumerate(dense_ranking, start=1)
        }
        sparse_scores = {
            chunk_id: float(len(sparse_ranking) - rank + 1)
            for rank, chunk_id in enumerate(sparse_ranking, start=1)
        }
        union = tuple(dict.fromkeys((*dense_ranking, *sparse_ranking)))
        points: list[models.PointStruct] = []
        for point_id, chunk_id in enumerate(union, start=1):
            vectors: dict[str, Any] = {}
            if chunk_id in dense_scores:
                vectors["dense"] = [dense_scores[chunk_id]]
            if chunk_id in sparse_scores:
                vectors["sparse"] = models.SparseVector(
                    indices=[0],
                    values=[sparse_scores[chunk_id]],
                )
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vectors,
                    payload={"chunk_id": chunk_id},
                )
            )
        await client.upsert(collection_name=collection, points=points, wait=True)
        started = perf_counter()
        response = await client.query_points(
            collection_name=collection,
            prefetch=[
                models.Prefetch(
                    query=[1.0],
                    using="dense",
                    limit=len(dense_ranking),
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=[0], values=[1.0]),
                    using="sparse",
                    limit=len(sparse_ranking),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        elapsed_ms = (perf_counter() - started) * 1_000
        fused: list[tuple[str, float]] = []
        for point in response.points:
            payload = point.payload
            if not isinstance(payload, dict) or not isinstance(payload.get("chunk_id"), str):
                raise _InvalidFusionPayloadError
            fused.append((payload["chunk_id"], float(point.score)))
        return tuple(fused), elapsed_ms
    finally:
        await client.close()


def _metrics(
    ranking: list[str] | tuple[str, ...],
    judgments: dict[str, int],
    sources: dict[str, str],
    *,
    cutoff: int,
) -> RankingMetrics:
    return evaluate_ranking(ranking, judgments, sources, cutoff=cutoff)


def _aggregate(
    query_rankings: list[tuple[dict[str, tuple[str, ...]], dict[str, int]]],
    sources: dict[str, str],
    *,
    cutoff: int,
) -> dict[str, RankingMetrics]:
    channels = query_rankings[0][0]
    return {
        channel: macro_average(
            tuple(
                _metrics(rankings[channel], judgments, sources, cutoff=cutoff)
                for rankings, judgments in query_rankings
            )
        )
        for channel in channels
    }


def _quality_gate(
    at_five: dict[str, RankingMetrics],
    at_production: dict[str, RankingMetrics],
) -> dict[str, Any]:
    rrf_five = at_five["rrf"]
    bge_five = at_five["rrfPlusBge"]
    rrf_production = at_production["rrf"]
    bge_production = at_production["rrfPlusBge"]
    checks = {
        "bgeImprovesOrPreservesMrrAt5": bge_five.mrr >= rrf_five.mrr,
        "bgeImprovesOrPreservesNdcgAt5": bge_five.ndcg >= rrf_five.ndcg,
        "bgePreservesRecallAtProductionCutoff": (bge_production.recall >= rrf_production.recall),
        "bgePreservesSourceCoverageAtProductionCutoff": (
            bge_production.source_coverage >= rrf_production.source_coverage
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "policy": (
            "BGE is mandatory. It must improve or preserve top-5 ranking quality and must "
            "preserve RRF recall and relevant-source coverage at the production cutoff."
        ),
    }


async def _live_rrf_rankings(  # noqa: PLR0915 - explicit end-to-end evaluation stages
    dataset: dict[str, Any],
    settings: Settings,
) -> tuple[list[tuple[str, ...]], dict[str, float]]:
    """Run the curated corpus through Docling and the real production retrieval adapters."""
    client = AsyncQdrantClient(location=":memory:")
    evaluation_settings = settings.model_copy(
        update={"qdrant_collection": "live-retrieval-evaluation"}
    )
    vectorizer = GeminiBm25Vectorizer(evaluation_settings)
    stager = QdrantVectorStager(evaluation_settings, client=client)
    searcher = QdrantHybridSearcher(evaluation_settings, client=client)
    engagement_id = "retrieval-evaluation"
    chunk_to_fixture: dict[str, str] = {}
    version_ids: list[str] = []
    try:
        extraction_started = perf_counter()
        seeds_by_document: list[tuple[DocumentSource, DocumentVersion, tuple[Any, ...]]] = []
        with tempfile.TemporaryDirectory(prefix="stakeholder-retrieval-evaluation-") as directory:
            root = Path(directory)
            extractor = DoclingExtractor(evaluation_settings)
            validator = UploadValidator(evaluation_settings)
            for item in dataset["documents"]:
                filename = f"{item['chunkId']}.docx"
                path = root / filename
                document = Document()
                document.add_paragraph(item["text"])
                document.save(str(path))
                content = await asyncio.to_thread(path.read_bytes)
                upload = validator.validate_envelope(
                    filename=filename,
                    declared_media_type=(
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                    content=content,
                )
                validator.validate_structure(upload)
                bundle = await asyncio.to_thread(extractor.extract, path, upload)
                source = DocumentSource(
                    document_id=item["sourceId"],
                    engagement_id=engagement_id,
                    stakeholder_id=None,
                    role=None,
                    department=None,
                    doc_type="docx",
                    source_type="engagement_document",
                    original_filename=filename,
                    media_type=upload.media_type,
                    created_at=datetime.now(UTC),
                )
                version_id = f"version-{item['chunkId']}"
                version_ids.append(version_id)
                version = DocumentVersion(
                    document_version_id=version_id,
                    document_id=source.document_id,
                    version_number=1,
                    content_hash=sha256(content).hexdigest(),
                    state="INDEXING",
                    is_active=False,
                    original_artifact_id=f"artifact-{item['chunkId']}",
                    ingestion_key=f"ingestion-{item['chunkId']}",
                    created_at=datetime.now(UTC),
                )
                elements = tuple(
                    SourceElement(
                        element_id=stable_id("evaluation-element", version_id, draft.key),
                        document_version_id=version_id,
                        element_type=draft.element_type,
                        original_content=draft.original_content,
                        english_interpretation=draft.english_interpretation,
                        location=draft.location,
                        parent_element_id=None,
                        artifact_id=None,
                        content_hash=sha256(draft.original_content.encode()).hexdigest(),
                        extraction_method=draft.extraction_method,
                    )
                    for draft in bundle.elements
                    if draft.original_content is not None
                    and draft.element_type in {"text", "table", "ocr_text"}
                )
                seeds = build_chunk_seeds(
                    version,
                    elements,
                    chunk_characters=evaluation_settings.ingestion_chunk_characters,
                    overlap=evaluation_settings.ingestion_chunk_overlap,
                )
                for seed in seeds:
                    chunk_to_fixture[seed.chunk_id] = item["chunkId"]
                seeds_by_document.append((source, version, seeds))
        extraction_ms = (perf_counter() - extraction_started) * 1_000

        all_seeds = tuple(seed for _source, _version, seeds in seeds_by_document for seed in seeds)
        started = perf_counter()
        vectors = await vectorizer.vectorize(tuple(seed.text for seed in all_seeds))
        vectorization_ms = (perf_counter() - started) * 1_000
        chunks: list[SearchChunk] = []
        offset = 0
        for source, version, seeds in seeds_by_document:
            next_offset = offset + len(seeds)
            chunks.extend(
                materialize_chunks(
                    source,
                    version,
                    seeds,
                    vectors[offset:next_offset],
                )
            )
            offset = next_offset
        await stager.initialize()
        await stager.stage(chunks)
        for version_id in version_ids:
            expected = tuple(
                chunk.chunk_id for chunk in chunks if chunk.source_version_id == version_id
            )
            await stager.verify(version_id, expected)
            await stager.prepare_activation(version_id)
        rankings: list[tuple[str, ...]] = []
        query_vectorization_ms = 0.0
        search_ms = 0.0
        active_versions = tuple(version_ids)
        retrieval_filter = RetrievalFilter(engagement_id=engagement_id)
        for query in dataset["queries"]:
            started = perf_counter()
            query_vector = await vectorizer.vectorize_query(query["text"])
            query_vectorization_ms += (perf_counter() - started) * 1_000
            started = perf_counter()
            hits = await searcher.search_hybrid(
                query_vector,
                retrieval_filter,
                active_versions,
                prefetch_limit=evaluation_settings.max_retrieval_candidates_per_channel,
                limit=evaluation_settings.max_rerank_candidates,
            )
            search_ms += (perf_counter() - started) * 1_000
            rankings.append(
                tuple(
                    dict.fromkeys(
                        chunk_to_fixture[hit.chunk_id]
                        for hit in hits
                        if hit.chunk_id in chunk_to_fixture
                    )
                )
            )
        return rankings, {
            "doclingExtractionAndNormalization": extraction_ms,
            "documentVectorization": vectorization_ms,
            "queryVectorization": query_vectorization_ms,
            "qdrantRrf": search_ms,
        }
    finally:
        await client.close()


async def _evaluate(
    dataset_path: Path,
    *,
    local_files_only: bool,
    live_pipeline: bool = False,
) -> dict[str, Any]:
    dataset_bytes = await asyncio.to_thread(dataset_path.read_bytes)
    dataset = json.loads(dataset_bytes)
    documents = {item["chunkId"]: item for item in dataset["documents"]}
    sources = {item["chunkId"]: item["sourceId"] for item in dataset["documents"]}
    settings = Settings() if live_pipeline else _settings()
    reranker = BgeReranker(settings, local_files_only=local_files_only)
    query_records: list[dict[str, Any]] = []
    evaluated_rankings: list[tuple[dict[str, tuple[str, ...]], dict[str, int]]] = []
    live_rankings: list[tuple[str, ...]] | None = None
    live_latency: dict[str, float] | None = None
    if live_pipeline:
        live_rankings, live_latency = await _live_rrf_rankings(dataset, settings)
    for query_index, query in enumerate(dataset["queries"]):
        dense_started = perf_counter()
        dense = tuple(query["denseRanking"])
        dense_ms = (perf_counter() - dense_started) * 1_000
        sparse_started = perf_counter()
        sparse = tuple(query["sparseRanking"])
        sparse_ms = (perf_counter() - sparse_started) * 1_000
        if live_rankings is None:
            fused, fusion_ms = await _native_rrf(
                list(dense),
                list(sparse),
                limit=settings.max_rerank_candidates,
            )
        else:
            fused = tuple(
                (chunk_id, 1.0 / rank)
                for rank, chunk_id in enumerate(live_rankings[query_index], start=1)
            )
            fusion_ms = 0.0
        reranked = await reranker.rerank(
            query["text"],
            tuple(documents[chunk_id]["text"] for chunk_id, _score in fused),
        )
        final = tuple(
            chunk_id
            for (chunk_id, rrf_score), _reranker_score in sorted(
                zip(fused, reranked.scores, strict=True),
                key=lambda pair: (-pair[1], -pair[0][1], pair[0][0]),
            )
        )
        rankings = {
            "dense": dense,
            "sparse": sparse,
            "rrf": tuple(chunk_id for chunk_id, _score in fused),
            "rrfPlusBge": final,
        }
        if live_pipeline:
            rankings = {
                name: value for name, value in rankings.items() if name in {"rrf", "rrfPlusBge"}
            }
        metrics = {
            name: _metrics(ranking, query["judgments"], sources, cutoff=5)
            for name, ranking in rankings.items()
        }
        evaluated_rankings.append((rankings, query["judgments"]))
        query_records.append(
            {
                "queryId": query["queryId"],
                "candidateCounts": {
                    "dense": len(dense),
                    "sparse": len(sparse),
                    "rrf": len(fused),
                    "rrfPlusBge": len(final),
                },
                "rankings": rankings,
                "metrics": {name: asdict(value) for name, value in metrics.items()},
                "latencyMs": {
                    "denseFixtureMaterialization": dense_ms,
                    "sparseFixtureMaterialization": sparse_ms,
                    "rrf": fusion_ms,
                    "bge": reranked.duration_ms,
                },
            }
        )
    production_cutoff = settings.max_retrieval_results
    aggregate_five = _aggregate(evaluated_rankings, sources, cutoff=5)
    aggregate_production = _aggregate(
        evaluated_rankings,
        sources,
        cutoff=production_cutoff,
    )
    quality_gate = _quality_gate(aggregate_five, aggregate_production)
    return {
        "evaluationId": "EV-P2-S7-RETRIEVAL-EVALUATION-001",
        "executedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "datasetId": dataset["datasetId"],
        "datasetVersion": dataset["version"],
        "datasetSha256": sha256(dataset_bytes).hexdigest(),
        "queryCount": len(dataset["queries"]),
        "cutoffs": {"rankingQuality": 5, "production": production_cutoff},
        "vectorMode": "live_full_pipeline" if live_pipeline else "deterministic_fixture",
        "fusionMethod": "qdrant_native_rrf",
        "reranker": {
            "modelId": settings.reranker_model,
            "revision": reranker.revision,
            "device": reranker.device,
            "sentenceTransformers": version("sentence-transformers"),
            "torch": version("torch"),
        },
        "aggregateAt5": {name: asdict(value) for name, value in aggregate_five.items()},
        "aggregateAtProductionCutoff": {
            name: asdict(value) for name, value in aggregate_production.items()
        },
        "qualityGate": quality_gate,
        "livePipelineLatencyMs": live_latency,
        "queries": query_records,
        "limitations": [
            (
                "Dense and sparse rankings are deterministic offline fixture inputs when "
                "--live-pipeline is omitted. --live-pipeline executes generated DOCX inputs "
                "through Docling, normalization, real Gemini/BM25, Qdrant RRF, and BGE."
            ),
            (
                "The versioned corpus and generated DOCX files are synthetic and curated; no "
                "production stakeholder data is sent to providers."
            ),
            (
                "The BGE reranker is the real local BAAI/bge-reranker-base model and is "
                "not replaced by a test double in this evaluation."
            ),
        ],
    }


async def _main() -> None:
    args = _arguments()
    result = await _evaluate(
        args.dataset.resolve(),
        local_files_only=args.local_files_only,
        live_pipeline=args.live_pipeline,
    )
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(serialized, end="")
    else:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
        print(json.dumps({"output": str(output), "status": "completed"}, sort_keys=True))
    if result["qualityGate"]["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
