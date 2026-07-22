"""Scoped agent artifact storage."""

from stakeholder_intelligence_agent.artifacts.scoped import (
    ScopedArtifactStore,
    ScopedFilesystemBackend,
    activate_artifact_access,
)

__all__ = ["ScopedArtifactStore", "ScopedFilesystemBackend", "activate_artifact_access"]
