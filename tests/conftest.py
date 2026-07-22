"""Shared deterministic test fixtures."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from stakeholder_intelligence_agent.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Build fully valid local settings without reading credentials or external state."""
    data_root = tmp_path / "data"
    return Settings(
        environment="test",
        google_api_key=SecretStr("test-google-key"),
        gemini_primary_chat_model="gemini-test-primary",
        gemini_fallback_chat_model="gemini-test-fallback",
        gemini_vision_model="gemini-test-vision",
        gemini_embedding_model="gemini-test-embedding",
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
