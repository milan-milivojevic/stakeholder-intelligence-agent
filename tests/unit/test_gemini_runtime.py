"""Deterministic Gemini pacing, quota-sanitization, and fallback tests."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Never, cast

import pytest
from google.genai.errors import ClientError
from langchain.agents.middleware.types import ModelResponse

from stakeholder_intelligence_agent.errors import (
    ProviderPacingTimeoutError,
    ProviderQuotaExhaustedError,
    ProviderTransientExhaustedError,
)
from stakeholder_intelligence_agent.gemini_runtime import (
    GeminiQuotaBackoffHandler,
    GeminiQuotaRetryMiddleware,
    GeminiRateLimiterHooks,
    GeminiRequestRateLimiter,
    safe_gemini_quota_observation,
)
from stakeholder_intelligence_agent.insight import graph as insight_graph
from stakeholder_intelligence_agent.insight.graph import InsightGraphDependencies
from stakeholder_intelligence_agent.models import (
    create_primary_and_fallback,
    safe_gemini_runtime_summary,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from stakeholder_intelligence_agent.config import Settings


@dataclass(slots=True)
class ManualClock:
    """Advance time only when a deterministic test explicitly requests it."""

    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def _limiter(
    clock: ManualClock,
    *,
    max_wait_seconds: float = 120.0,
    async_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> GeminiRequestRateLimiter:
    hooks = GeminiRateLimiterHooks(
        clock=clock.monotonic,
        sync_sleep=clock.sleep,
        async_sleep=async_sleep or asyncio.sleep,
    )
    return GeminiRequestRateLimiter(
        requests_per_minute_limit=15,
        headroom_requests=1,
        max_wait_seconds=max_wait_seconds,
        hooks=hooks,
    )


def _quota_error(
    *,
    retry_delay: object | None = None,
    quota_metric: str = "generativelanguage.googleapis.com/generate_content_free_tier_requests",
    quota_id: str = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
) -> ClientError:
    details: list[dict[str, object]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {
                    "quotaMetric": quota_metric,
                    "quotaId": quota_id,
                    "quotaDimensions": {
                        "model": "gemini-3.1-flash-lite",
                        "location": "global",
                        "project": "secret-project-number",
                    },
                    "quotaValue": "15",
                    "description": "sensitive provider prose",
                }
            ],
        },
        {
            "@type": "type.googleapis.com/google.rpc.Help",
            "links": [{"url": "https://example.invalid/private-project"}],
        },
    ]
    if retry_delay is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        )
    return ClientError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "secret provider message and prompt content",
                "details": details,
            }
        },
    )


def test_pacing_keeps_every_rolling_minute_below_fifteen_requests() -> None:
    clock = ManualClock()
    limiter = _limiter(clock)

    timestamps: list[float] = []
    for _ in range(15):
        assert limiter.acquire()
        timestamps.append(clock.monotonic())

    assert timestamps[0] == 0
    assert timestamps[13] < 60
    assert timestamps[14] > 60
    assert timestamps[14] == pytest.approx(14 * limiter.minimum_interval_seconds)
    assert sum(timestamp < 60 for timestamp in timestamps) == 14
    assert all(
        later - earlier == pytest.approx(limiter.minimum_interval_seconds)
        for earlier, later in pairwise(timestamps)
    )


def test_paid_quota_can_disable_headroom_and_pace_at_exact_limit() -> None:
    limiter = GeminiRequestRateLimiter(
        requests_per_minute_limit=100,
        headroom_requests=0,
        max_wait_seconds=120,
    )

    assert limiter.effective_requests_per_minute == 100
    assert limiter.minimum_interval_seconds == pytest.approx(0.601)


def test_concurrent_nonblocking_callers_can_consume_only_one_slot() -> None:
    clock = ManualClock()
    limiter = _limiter(clock)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _: limiter.acquire(blocking=False), range(16)))

    assert results.count(True) == 1
    assert results.count(False) == 15


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_a_future_slot() -> None:
    clock = ManualClock()
    sleep_started = asyncio.Event()
    never_release = asyncio.Event()

    async def cancellable_sleep(_: float) -> None:
        sleep_started.set()
        await never_release.wait()

    limiter = _limiter(clock, async_sleep=cancellable_sleep)
    assert await limiter.aacquire(blocking=False)

    waiter = asyncio.create_task(limiter.aacquire())
    await sleep_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    clock.advance(limiter.minimum_interval_seconds)
    assert await limiter.aacquire(blocking=False)


@pytest.mark.asyncio
async def test_wait_longer_than_configured_timeout_fails_without_provider_call() -> None:
    clock = ManualClock()
    limiter = _limiter(clock, max_wait_seconds=1)
    assert await limiter.aacquire(blocking=False)

    with pytest.raises(ProviderPacingTimeoutError):
        await limiter.aacquire()

    assert clock.monotonic() == 0


def test_structured_429_retains_only_allowlisted_quota_and_retry_facts() -> None:
    provider_error = _quota_error(retry_delay={"seconds": "37", "nanos": 500_000_000})
    wrapped = RuntimeError("safe wrapper")
    wrapped.__cause__ = provider_error

    observation = safe_gemini_quota_observation(wrapped)

    assert observation is not None
    assert observation.status_code == 429
    assert observation.provider_status == "RESOURCE_EXHAUSTED"
    assert observation.retry_delay_seconds == pytest.approx(37.5)
    assert len(observation.violations) == 1
    retained = observation.as_dict()
    assert retained["violations"][0]["quota_dimensions"] == {
        "location": "global",
        "model": "gemini-3.1-flash-lite",
    }
    serialized = json.dumps(retained, sort_keys=True)
    assert "secret" not in serialized
    assert "project" not in serialized
    assert "message" not in serialized
    assert "description" not in serialized
    assert "private-project" not in serialized


def test_429_retry_info_defers_the_shared_limiter_without_retrying() -> None:
    clock = ManualClock()
    limiter = _limiter(clock)
    handler = GeminiQuotaBackoffHandler(limiter)
    assert limiter.acquire(blocking=False)

    handler.on_llm_error(_quota_error(retry_delay="37.5s"))

    clock.advance(37.49)
    assert not limiter.acquire(blocking=False)
    clock.advance(0.01)
    assert limiter.acquire(blocking=False)


@pytest.mark.asyncio
async def test_quota_retry_middleware_retries_once_after_shared_pacing_delay() -> None:
    clock = ManualClock()

    async def advance(seconds: float) -> None:
        clock.advance(seconds)

    limiter = _limiter(clock, async_sleep=advance)
    middleware = GeminiQuotaRetryMiddleware(limiter)
    calls = 0

    async def call_model(_: object) -> ModelResponse[Any]:
        nonlocal calls
        assert await limiter.aacquire()
        calls += 1
        if calls == 1:
            raise _quota_error(retry_delay="1s")
        return ModelResponse(result=[])

    response = await middleware.awrap_model_call(
        cast("Any", object()),
        cast("Any", call_model),
    )

    assert response.result == []
    assert calls == 2
    assert clock.monotonic() == pytest.approx(limiter.minimum_interval_seconds)


@pytest.mark.asyncio
async def test_input_token_quota_retry_waits_for_a_full_rolling_window() -> None:
    clock = ManualClock()

    async def advance(seconds: float) -> None:
        clock.advance(seconds)

    limiter = _limiter(clock, async_sleep=advance)
    middleware = GeminiQuotaRetryMiddleware(limiter)
    calls = 0

    async def call_model(_: object) -> ModelResponse[Any]:
        nonlocal calls
        assert await limiter.aacquire()
        calls += 1
        if calls == 1:
            raise _quota_error(
                retry_delay="1s",
                quota_metric=(
                    "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count"
                ),
                quota_id="GenerateContentInputTokensPerModelPerMinute-FreeTier",
            )
        return ModelResponse(result=[])

    await middleware.awrap_model_call(
        cast("Any", object()),
        cast("Any", call_model),
    )

    assert calls == 2
    assert clock.monotonic() == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_quota_retry_middleware_maps_second_429_to_safe_domain_failure() -> None:
    clock = ManualClock()

    async def advance(seconds: float) -> None:
        clock.advance(seconds)

    limiter = _limiter(clock, async_sleep=advance)
    middleware = GeminiQuotaRetryMiddleware(limiter)
    calls = 0

    async def call_model(_: object) -> ModelResponse[Any]:
        nonlocal calls
        assert await limiter.aacquire()
        calls += 1
        raise _quota_error(retry_delay="1s")

    with pytest.raises(ProviderQuotaExhaustedError):
        await middleware.awrap_model_call(
            cast("Any", object()),
            cast("Any", call_model),
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_retry_middleware_retries_one_allowlisted_transient_failure() -> None:
    class ServiceUnavailableError(RuntimeError):
        status_code = 503

    middleware = GeminiQuotaRetryMiddleware(_limiter(ManualClock()))
    calls = 0

    async def call_model(_: object) -> ModelResponse[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ServiceUnavailableError
        return ModelResponse(result=[])

    response = await middleware.awrap_model_call(
        cast("Any", object()),
        cast("Any", call_model),
    )

    assert response.result == []
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_middleware_maps_second_transient_failure_to_safe_error() -> None:
    class GatewayTimeoutError(RuntimeError):
        status_code = 504

    middleware = GeminiQuotaRetryMiddleware(_limiter(ManualClock()))
    calls = 0

    async def call_model(_: object) -> Never:
        nonlocal calls
        calls += 1
        raise GatewayTimeoutError

    with pytest.raises(ProviderTransientExhaustedError):
        await middleware.awrap_model_call(
            cast("Any", object()),
            cast("Any", call_model),
        )

    assert calls == 2


@pytest.mark.asyncio
async def test_retry_middleware_does_not_retry_nontransient_provider_failure() -> None:
    class BadRequestError(RuntimeError):
        status_code = 400

    middleware = GeminiQuotaRetryMiddleware(_limiter(ManualClock()))
    calls = 0

    async def call_model(_: object) -> Never:
        nonlocal calls
        calls += 1
        raise BadRequestError

    with pytest.raises(BadRequestError):
        await middleware.awrap_model_call(
            cast("Any", object()),
            cast("Any", call_model),
        )

    assert calls == 1


def test_same_model_fallback_is_disabled_and_sdk_retries_are_really_zero(
    settings: Settings,
) -> None:
    same_model = settings.model_copy(
        update={
            "gemini_primary_chat_model": "models/Gemini-3.1-Flash-Lite",
            "gemini_fallback_chat_model": "gemini-3.1-flash-lite",
        }
    )

    primary, fallback = create_primary_and_fallback(same_model)
    summary = safe_gemini_runtime_summary(same_model)

    assert fallback is None
    assert primary.max_retries == 1
    assert isinstance(primary.rate_limiter, GeminiRequestRateLimiter)
    assert isinstance(primary.callbacks, list)
    assert any(isinstance(callback, GeminiQuotaBackoffHandler) for callback in primary.callbacks)
    assert summary["same_model"] is True
    assert summary["fallback_enabled"] is False
    assert summary["effective_requests_per_minute"] == 14
    assert summary["sdk_initial_attempts"] == 1
    assert summary["sdk_extra_retries"] == 0


def test_distinct_fallback_shares_the_exact_primary_limiter(
    settings: Settings,
) -> None:
    distinct_models = settings.model_copy(
        update={
            "gemini_primary_chat_model": "gemini-test-primary",
            "gemini_fallback_chat_model": "gemini-test-fallback-distinct",
        }
    )

    primary, fallback = create_primary_and_fallback(distinct_models)

    assert fallback is not None
    assert primary.rate_limiter is fallback.rate_limiter
    assert primary.max_retries == 1
    assert fallback.max_retries == 1


def test_same_model_insight_graph_never_constructs_fallback_middleware(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_model = settings.model_copy(
        update={"gemini_fallback_chat_model": settings.gemini_primary_chat_model}
    )

    def reject_same_model_fallback(*_: object, **__: object) -> Never:
        raise AssertionError

    monkeypatch.setattr(
        insight_graph,
        "ModelFallbackMiddleware",
        reject_same_model_fallback,
    )

    graph = insight_graph.build_insight_graph(
        same_model,
        dependencies=InsightGraphDependencies(),
    )

    assert graph.name == "stakeholder_insight"
