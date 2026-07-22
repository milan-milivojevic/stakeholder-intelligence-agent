"""Deterministic checks for the revisioned live demo seed boundary."""

# The test intentionally constructs the small credential-safe client without
# network activation so it can isolate private hash/idempotency boundaries.
# ruff: noqa: S105, SLF001, TRY003

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, cast

from scripts import seed_demo_engagements as seed_module

if TYPE_CHECKING:
    from pathlib import Path


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _DocumentClient:
    def __init__(self, listed_hash: str, uploaded_hash: str) -> None:
        stakeholder = seed_module.ENGAGEMENTS[0].stakeholders[0]
        self._listed = {
            "documents": [
                {
                    "source": {"original_filename": stakeholder.document_filename},
                    "latest_version": {"content_hash": listed_hash, "state": "READY"},
                }
            ]
        }
        self._uploaded = {
            "document": {
                "source": {"original_filename": stakeholder.document_filename},
                "latest_version": {"content_hash": uploaded_hash, "state": "READY"},
            }
        }
        self.post_count = 0

    def get(self, _path: str, *, headers: dict[str, str]) -> _Response:
        assert headers["Authorization"].startswith("Bearer ")
        return _Response(200, self._listed)

    def post(
        self,
        _path: str,
        *,
        files: dict[str, tuple[str, Any, str]],
        headers: dict[str, str],
    ) -> _Response:
        assert files["upload"][0].endswith(".docx")
        assert headers["Authorization"].startswith("Bearer ")
        self.post_count += 1
        return _Response(201, self._uploaded)


class _TurnResponse(_Response):
    def __init__(self, text: str) -> None:
        super().__init__(200, {})
        self.text = text


class _TurnClient:
    def __init__(self) -> None:
        self.calls = 0

    def post(
        self,
        _path: str,
        *,
        json: dict[str, str],
        headers: dict[str, str],
    ) -> _TurnResponse:
        assert json["message_id"] == "stable-message"
        assert headers["Authorization"].startswith("Bearer ")
        self.calls += 1
        if self.calls == 1:
            return _TurnResponse("event: failure\ndata: {}\n\n")
        return _TurnResponse("event: message\ndata: {}\n\n")


class _InsightClient:
    def __init__(self, question: str) -> None:
        self._question = question
        self.post_count = 0

    def get(self, path: str, *, headers: dict[str, str]) -> _Response:
        assert headers["Authorization"].startswith("Bearer ")
        if path.endswith("/insights"):
            return _Response(
                200,
                {
                    "runs": [
                        {
                            "run_id": "run-discovered",
                            "requested_question": self._question,
                            "status": "complete",
                        }
                    ]
                },
            )
        if path.endswith("/run-discovered/report"):
            return _Response(
                200,
                {
                    "report": {"citations": [{"citation_id": "citation-1"}]},
                    "metrics": {"model_calls": 4},
                },
            )
        raise AssertionError(f"Unexpected GET {path}")

    def post(self, _path: str, **_kwargs: Any) -> _Response:
        self.post_count += 1
        raise AssertionError("A completed question must be reused without a new insight run.")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _api_with_document_client(
    document_root: Path,
    client: _DocumentClient,
) -> seed_module.LiveApi:
    api = object.__new__(seed_module.LiveApi)
    api._document_root = document_root
    api._client = cast("Any", client)
    api._pm_token = "test-pm-token"
    return api


def test_document_preparation_is_revision_and_hash_idempotent(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    document_root = tmp_path / "documents"
    manifest_path = tmp_path / "manifest.json"

    first = seed_module.prepare_documents(document_root, manifest_path)
    first_hashes = {path.name: _sha256(path) for path in first}

    def reject_regeneration(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(
            "A matching seed revision and hash must reuse the existing DOCX bytes."
        )

    monkeypatch.setattr(seed_module, "build_supporting_document", reject_regeneration)
    second = seed_module.prepare_documents(document_root, manifest_path)

    assert {path.name: _sha256(path) for path in second} == first_hashes
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["seed_revision"] == seed_module.SEED_REVISION
    assert {
        filename: record["content_sha256"] for filename, record in manifest["documents"].items()
    } == first_hashes


def test_document_reuse_requires_exact_ingested_hash(tmp_path: Path) -> None:
    stakeholder = seed_module.ENGAGEMENTS[0].stakeholders[0]
    document_root = tmp_path / "documents"
    document_root.mkdir()
    document_path = document_root / stakeholder.document_filename
    document_path.write_bytes(b"current revision bytes")
    current_hash = _sha256(document_path)
    client = _DocumentClient(listed_hash=current_hash, uploaded_hash=current_hash)
    api = _api_with_document_client(document_root, client)

    document = api.ensure_document("stakeholder-token", stakeholder)

    assert client.post_count == 0
    assert document["latest_version"]["content_hash"] == current_hash


def test_document_hash_mismatch_uploads_a_new_version(tmp_path: Path) -> None:
    stakeholder = seed_module.ENGAGEMENTS[0].stakeholders[0]
    document_root = tmp_path / "documents"
    document_root.mkdir()
    document_path = document_root / stakeholder.document_filename
    document_path.write_bytes(b"new revision bytes")
    current_hash = _sha256(document_path)
    client = _DocumentClient(listed_hash="0" * 64, uploaded_hash=current_hash)
    api = _api_with_document_client(document_root, client)

    document = api.ensure_document("stakeholder-token", stakeholder)

    assert client.post_count == 1
    assert document["latest_version"]["content_hash"] == current_hash


def test_insight_reuse_is_limited_to_the_current_seed_revision() -> None:
    question = "What is supported?"
    engagement = {
        "name": "Example engagement",
        "insights": [{"question": question, "run_id": "run_current"}],
    }

    assert seed_module._reusable_run_ids(
        {"seed_revision": seed_module.SEED_REVISION, "engagements": [engagement]}
    ) == {("Example engagement", question): "run_current"}
    assert (
        seed_module._reusable_run_ids(
            {"seed_revision": seed_module.SEED_REVISION - 1, "engagements": [engagement]}
        )
        == {}
    )


def test_completed_insight_is_discovered_by_exact_question_without_saved_summary_id() -> None:
    question = "What responsibilities are supported?"
    client = _InsightClient(question)
    api = object.__new__(seed_module.LiveApi)
    api._client = cast("Any", client)
    api._pm_token = "test-pm-token"

    result = api.run_insight("engagement-1", question, None)

    assert result["run_id"] == "run-discovered"
    assert result["citation_count"] == 1
    assert client.post_count == 0


def test_interview_turn_retries_same_idempotency_key_after_safe_failure(
    monkeypatch: Any,
) -> None:
    client = _TurnClient()
    api = object.__new__(seed_module.LiveApi)
    api._client = cast("Any", client)
    delays: list[int] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    api.submit_answer("stakeholder-token", "Same answer.", "stable-message")

    assert client.calls == 2
    assert delays == [seed_module.INTERVIEW_TURN_RETRY_SECONDS]


def test_seed_is_two_cross_functional_engagements_with_e2e_evidence() -> None:
    assert len(seed_module.ENGAGEMENTS) == 2
    assert all(len(engagement.stakeholders) == 3 for engagement in seed_module.ENGAGEMENTS)
    assert all(len(engagement.insight_questions) >= 3 for engagement in seed_module.ENGAGEMENTS)
    assert all(
        stakeholder.document_filename.endswith(".docx")
        and stakeholder.answers
        and stakeholder.final_check_answer
        for engagement in seed_module.ENGAGEMENTS
        for stakeholder in engagement.stakeholders
    )
    expiring = [
        stakeholder
        for engagement in seed_module.ENGAGEMENTS
        for stakeholder in engagement.stakeholders
        if stakeholder.expire_after_answers is not None
    ]
    assert len(expiring) == 1
    assert expiring[0].expire_after_answers == 2
