"""Physical and virtual artifact isolation tests."""

from pathlib import Path

import pytest

from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.errors import (
    ArtifactNotFoundError,
    ArtifactPathError,
)
from tests.helpers import pm_access


def test_artifact_store_separates_threads(tmp_path: Path) -> None:
    store = ScopedArtifactStore(tmp_path / "artifacts")
    first = pm_access(thread_id="thread-a")
    second = pm_access(thread_id="thread-b")

    stored = store.write_text(first, "/research/topic-a/findings.md", "Scoped finding.\n")

    assert stored.is_relative_to(tmp_path / "artifacts" / "engagement-a" / "thread-a")
    assert store.read_text(first, "/research/topic-a/findings.md") == "Scoped finding.\n"
    with pytest.raises(ArtifactNotFoundError):
        store.read_text(second, "/research/topic-a/findings.md")


def test_project_store_and_deep_agent_backend_share_one_storage_path(tmp_path: Path) -> None:
    store = ScopedArtifactStore(tmp_path / "artifacts")
    access = pm_access(thread_id="thread-shared-backend")
    backend = store.backend(access)

    written = backend.write("/research/topic-a/findings.md", "Backend finding.\n")
    assert written.error is None
    assert store.read_text(access, "/research/topic-a/findings.md") == "Backend finding.\n"

    store.write_text(access, "/report/insight_report.json", '{"status":"complete"}\n')
    loaded = backend.read("/report/insight_report.json", limit=1_000)
    assert loaded.error is None
    assert loaded.file_data == {
        "content": '{"status":"complete"}\n',
        "encoding": "utf-8",
    }


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/research/../outside.txt", r"C:\outside.txt", "~/.env"],
)
def test_artifact_store_rejects_host_or_traversal_paths(tmp_path: Path, path: str) -> None:
    store = ScopedArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactPathError):
        store.write_text(pm_access(), path, "blocked")
