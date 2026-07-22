"""Configuration boundary tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from stakeholder_intelligence_agent.config import Settings


def _base_values(tmp_path: Path) -> dict[str, object]:
    data_root = tmp_path / "data"
    return {
        "environment": "test",
        "google_api_key": "test-google-key",
        "gemini_primary_chat_model": "gemini-test-primary",
        "gemini_fallback_chat_model": "gemini-test-fallback",
        "gemini_vision_model": "gemini-test-vision",
        "gemini_embedding_model": "gemini-test-embedding",
        "pm_bootstrap_token": "p" * 32,
        "token_pepper": "t" * 32,
        "data_root": data_root,
        "domain_database": data_root / "domain.sqlite3",
        "checkpoint_database": data_root / "checkpoints.sqlite3",
        "originals_root": data_root / "originals",
        "derived_root": data_root / "derived",
        "agent_artifacts_root": data_root / "agent-artifacts",
        "audit_root": data_root / "audit",
    }


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("gemini_primary_chat_model", "gpt-forbidden"),
        ("qdrant_url", "https://qdrant.example.com"),
        ("sparse_model", "different-sparse-model"),
        ("reranker_model", "different-reranker"),
        ("langsmith_tracing", True),
        ("max_research_topics", 6),
        ("max_parallel_researchers", 4),
        ("retrieval_calls_per_researcher_limit", 11),
        ("insight_run_timeout_seconds", 29),
        ("provider_sdk_retries", 1),
        ("gemini_requests_per_minute_limit", 1),
        ("gemini_requests_per_minute_headroom", -1),
        ("gemini_requests_per_minute_headroom", 15),
        ("gemini_rate_limit_wait_timeout_seconds", 0),
        ("browser_origin", "https://application.example.com"),
        ("browser_origin", "http://localhost:2024/path"),
        ("browser_origin", "http://localhost:2024/"),
    ],
)
def test_forbidden_runtime_configuration_is_rejected(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    values = _base_values(tmp_path)
    values[field_name] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_persistence_path_cannot_escape_data_root(tmp_path: Path) -> None:
    values = _base_values(tmp_path)
    values["checkpoint_database"] = tmp_path / "outside.sqlite3"
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_domain_and_checkpoint_databases_must_be_distinct(tmp_path: Path) -> None:
    values = _base_values(tmp_path)
    values["checkpoint_database"] = values["domain_database"]
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_documented_uppercase_environment_contract_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the same uppercase names used by .env.example and run scripts."""
    data_root = tmp_path / "runtime-data"
    documented_environment = {
        "GOOGLE_API_KEY": "test-google-key",
        "STAKEHOLDER_AI_ENVIRONMENT": "test",
        "STAKEHOLDER_AI_GEMINI_PRIMARY_CHAT_MODEL": "gemini-test-primary",
        "STAKEHOLDER_AI_GEMINI_FALLBACK_CHAT_MODEL": "gemini-test-fallback",
        "STAKEHOLDER_AI_GEMINI_VISION_MODEL": "gemini-test-vision",
        "STAKEHOLDER_AI_GEMINI_EMBEDDING_MODEL": "gemini-test-embedding",
        "STAKEHOLDER_AI_PM_BOOTSTRAP_TOKEN": "p" * 32,
        "STAKEHOLDER_AI_TOKEN_PEPPER": "t" * 32,
        "STAKEHOLDER_AI_INVITATION_TTL_MINUTES": "1440",
        "STAKEHOLDER_AI_STAKEHOLDER_SESSION_TTL_MINUTES": "480",
        "STAKEHOLDER_AI_PM_SESSION_TTL_MINUTES": "720",
        "STAKEHOLDER_AI_BROWSER_ORIGIN": "https://localhost:2024",
        "STAKEHOLDER_AI_DATA_ROOT": str(data_root),
        "STAKEHOLDER_AI_DOMAIN_DATABASE": str(data_root / "domain.sqlite3"),
        "STAKEHOLDER_AI_CHECKPOINT_DATABASE": str(data_root / "checkpoints.sqlite3"),
        "STAKEHOLDER_AI_ORIGINALS_ROOT": str(data_root / "originals"),
        "STAKEHOLDER_AI_DERIVED_ROOT": str(data_root / "derived"),
        "STAKEHOLDER_AI_AGENT_ARTIFACTS_ROOT": str(data_root / "agent-artifacts"),
        "STAKEHOLDER_AI_AUDIT_ROOT": str(data_root / "audit"),
        "STAKEHOLDER_AI_PROVIDER_SDK_RETRIES": "0",
        "STAKEHOLDER_AI_GEMINI_REQUESTS_PER_MINUTE_LIMIT": "15",
        "STAKEHOLDER_AI_GEMINI_REQUESTS_PER_MINUTE_HEADROOM": "1",
        "STAKEHOLDER_AI_GEMINI_RATE_LIMIT_WAIT_TIMEOUT_SECONDS": "180",
        "LANGSMITH_TRACING": "false",
    }
    for name, value in documented_environment.items():
        monkeypatch.setenv(name, value)

    loaded = Settings(_env_file=None)

    assert loaded.environment == "test"
    assert loaded.gemini_primary_chat_model == "gemini-test-primary"
    assert loaded.provider_sdk_retries == 0
    assert loaded.gemini_effective_requests_per_minute == 14
    assert loaded.gemini_rate_limit_wait_timeout_seconds == 180
    assert loaded.data_root == data_root.resolve()
    assert loaded.browser_origin == "https://localhost:2024"
    assert loaded.browser_cookie_secure is True


def test_loopback_http_origin_uses_documented_insecure_cookie_exception(
    tmp_path: Path,
) -> None:
    values = _base_values(tmp_path)
    values["browser_origin"] = "http://127.0.0.1:2024"

    loaded = Settings.model_validate(values)

    assert loaded.browser_cookie_secure is False


def test_paid_gemini_quota_can_use_exact_configured_rpm(tmp_path: Path) -> None:
    values = _base_values(tmp_path)
    values["gemini_requests_per_minute_limit"] = 100
    values["gemini_requests_per_minute_headroom"] = 0

    loaded = Settings.model_validate(values)

    assert loaded.gemini_effective_requests_per_minute == 100
