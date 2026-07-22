"""Gemini-only model construction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_google_genai import ChatGoogleGenerativeAI

from stakeholder_intelligence_agent.errors import ProviderPolicyError
from stakeholder_intelligence_agent.gemini_runtime import (
    GeminiQuotaBackoffHandler,
    GeminiRequestRateLimiter,
    get_shared_gemini_rate_limiter,
)

if TYPE_CHECKING:
    from langchain_core.rate_limiters import BaseRateLimiter

    from stakeholder_intelligence_agent.config import Settings


def _model_identity_key(model_id: str) -> str:
    return model_id.casefold().removeprefix("models/")


def create_chat_model(
    settings: Settings,
    model_id: str,
    *,
    rate_limiter: BaseRateLimiter | None = None,
) -> ChatGoogleGenerativeAI:
    """Create a deterministic Gemini chat client from validated settings."""
    if "gemini" not in model_id.lower():
        raise ProviderPolicyError
    limiter = rate_limiter or get_shared_gemini_rate_limiter(settings)
    callbacks = (
        [GeminiQuotaBackoffHandler(limiter)]
        if isinstance(limiter, GeminiRequestRateLimiter)
        else None
    )
    return ChatGoogleGenerativeAI(
        model=model_id,
        api_key=settings.google_api_key,
        temperature=0,
        request_timeout=float(settings.provider_timeout_seconds),
        rate_limiter=limiter,
        callbacks=callbacks,
        # SDK-internal retries are disabled because they are not observable through
        # LangChain callbacks. In the installed adapter, retries=1 means exactly
        # one initial SDK attempt; retries=0 silently enables Google SDK defaults.
        retries=settings.provider_sdk_retries + 1,
    )


def create_primary_and_fallback(
    settings: Settings,
) -> tuple[ChatGoogleGenerativeAI, ChatGoogleGenerativeAI | None]:
    """Create shared-paced clients and suppress a same-model fallback retry."""
    limiter = get_shared_gemini_rate_limiter(settings)
    primary = create_chat_model(
        settings,
        settings.gemini_primary_chat_model,
        rate_limiter=limiter,
    )
    if _model_identity_key(settings.gemini_primary_chat_model) == _model_identity_key(
        settings.gemini_fallback_chat_model
    ):
        return primary, None
    return (
        primary,
        create_chat_model(
            settings,
            settings.gemini_fallback_chat_model,
            rate_limiter=limiter,
        ),
    )


def safe_gemini_runtime_summary(settings: Settings) -> dict[str, object]:
    """Return non-secret model and pacing identities for retained evidence."""
    limiter = get_shared_gemini_rate_limiter(settings)
    same_model = _model_identity_key(settings.gemini_primary_chat_model) == _model_identity_key(
        settings.gemini_fallback_chat_model
    )
    return {
        "primary_model": settings.gemini_primary_chat_model,
        "fallback_model": settings.gemini_fallback_chat_model,
        "same_model": same_model,
        "fallback_enabled": not same_model,
        "requests_per_minute_limit": settings.gemini_requests_per_minute_limit,
        "headroom_requests": settings.gemini_requests_per_minute_headroom,
        "effective_requests_per_minute": settings.gemini_effective_requests_per_minute,
        "minimum_interval_seconds": round(limiter.minimum_interval_seconds, 6),
        "sdk_initial_attempts": settings.provider_sdk_retries + 1,
        "sdk_extra_retries": settings.provider_sdk_retries,
    }
