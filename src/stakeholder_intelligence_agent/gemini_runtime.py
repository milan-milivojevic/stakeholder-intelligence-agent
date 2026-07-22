"""Shared Gemini pacing and sanitized quota-error observation."""

from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.rate_limiters import BaseRateLimiter

from stakeholder_intelligence_agent.errors import (
    ProviderPacingTimeoutError,
    ProviderQuotaExhaustedError,
    ProviderTransientExhaustedError,
)

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

_QUOTA_FAILURE_TYPE: Final = "google.rpc.QuotaFailure"
_RETRY_INFO_TYPE: Final = "google.rpc.RetryInfo"
_SAFE_DIMENSION_KEYS: Final = frozenset(
    {"api", "location", "method", "model", "region", "service", "tier"}
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_RETRY_DELAY_TEXT = re.compile(
    r"retry(?:_|)delay\s*(?:[\"']?\s*:\s*[\"']?)?\s*\{?\s*"
    r"(?:seconds\s*:\s*)?(?P<seconds>\d+(?:\.\d+)?)\s*s?",
    re.IGNORECASE,
)
_WINDOW_SECONDS: Final = 60.0
_PACING_SAFETY_SECONDS: Final = 0.001
_DEFAULT_429_COOLDOWN_SECONDS: Final = 60.0
_MAX_QUOTA_VIOLATIONS: Final = 8
_HTTP_TOO_MANY_REQUESTS: Final = 429
_TRANSIENT_PROVIDER_STATUS_CODES: Final = frozenset({408, 500, 502, 503, 504})
_INPUT_TOKEN_QUOTA_MARKERS: Final = ("input_token", "inputtokens")
_INVALID_RPM_LIMIT = "The Gemini RPM limit must be greater than one."
_INVALID_RPM_HEADROOM = "Gemini RPM headroom must be non-negative and leave one usable request."
_INVALID_WAIT_TIMEOUT = "The Gemini pacing wait timeout must be positive."
_INVALID_RETRY_DELAY = "The Gemini retry delay must be finite and non-negative."


@dataclass(frozen=True, slots=True)
class GeminiQuotaViolation:
    """Allowlisted quota fields that cannot contain prompts, keys, or project IDs."""

    quota_metric: str | None
    quota_id: str | None
    quota_dimensions: tuple[tuple[str, str], ...]
    quota_value: int | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready safe representation."""
        return {
            "quota_metric": self.quota_metric,
            "quota_id": self.quota_id,
            "quota_dimensions": dict(self.quota_dimensions),
            "quota_value": self.quota_value,
        }


@dataclass(frozen=True, slots=True)
class GeminiQuotaObservation:
    """Structured, non-disclosing facts extracted from one Gemini HTTP 429."""

    status_code: int
    provider_status: str | None
    retry_delay_seconds: float | None
    violations: tuple[GeminiQuotaViolation, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready safe representation."""
        return {
            "status_code": self.status_code,
            "provider_status": self.provider_status,
            "retry_delay_seconds": self.retry_delay_seconds,
            "violations": [violation.as_dict() for violation in self.violations],
        }


@dataclass(frozen=True, slots=True)
class GeminiRateLimiterHooks:
    """Injectable time primitives for deterministic rate-limiter verification."""

    clock: Callable[[], float] = time.monotonic
    sync_sleep: Callable[[float], None] = time.sleep
    async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class GeminiRequestRateLimiter(BaseRateLimiter):
    """Process-shared, burst-free request pacing with a rolling-minute reserve."""

    def __init__(
        self,
        *,
        requests_per_minute_limit: int,
        headroom_requests: int,
        max_wait_seconds: float,
        hooks: GeminiRateLimiterHooks | None = None,
    ) -> None:
        if requests_per_minute_limit <= 1:
            raise ValueError(_INVALID_RPM_LIMIT)
        if headroom_requests < 0 or headroom_requests >= requests_per_minute_limit:
            raise ValueError(_INVALID_RPM_HEADROOM)
        if not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError(_INVALID_WAIT_TIMEOUT)

        hooks = hooks or GeminiRateLimiterHooks()
        self.requests_per_minute_limit = requests_per_minute_limit
        self.headroom_requests = headroom_requests
        self.effective_requests_per_minute = requests_per_minute_limit - headroom_requests
        self.minimum_interval_seconds = (
            _WINDOW_SECONDS / self.effective_requests_per_minute + _PACING_SAFETY_SECONDS
        )
        self.max_wait_seconds = float(max_wait_seconds)
        self._clock = hooks.clock
        self._sync_sleep = hooks.sync_sleep
        self._async_sleep = hooks.async_sleep
        self._next_allowed_at = 0.0
        self._lock = threading.Lock()

    def _attempt(self) -> tuple[bool, float, float]:
        """Atomically acquire the current slot or return its remaining wait."""
        with self._lock:
            now = self._clock()
            if now >= self._next_allowed_at:
                self._next_allowed_at = now + self.minimum_interval_seconds
                return True, 0.0, now
            return False, self._next_allowed_at - now, now

    def defer_for(self, delay_seconds: float) -> None:
        """Push all not-yet-acquired callers behind one provider cooldown."""
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError(_INVALID_RETRY_DELAY)
        with self._lock:
            deferred_until = self._clock() + delay_seconds
            self._next_allowed_at = max(self._next_allowed_at, deferred_until)

    def acquire(self, *, blocking: bool = True) -> bool:
        """Acquire one synchronous provider slot without allowing bursts."""
        deadline = self._clock() + self.max_wait_seconds
        while True:
            acquired, wait_seconds, now = self._attempt()
            if acquired:
                return True
            if not blocking:
                return False
            if wait_seconds > deadline - now:
                raise ProviderPacingTimeoutError
            self._sync_sleep(wait_seconds)

    async def aacquire(self, *, blocking: bool = True) -> bool:
        """Acquire one asynchronous provider slot with cancellable waiting."""
        deadline = self._clock() + self.max_wait_seconds
        while True:
            acquired, wait_seconds, now = self._attempt()
            if acquired:
                return True
            if not blocking:
                return False
            if wait_seconds > deadline - now:
                raise ProviderPacingTimeoutError
            await self._async_sleep(wait_seconds)


class GeminiQuotaBackoffHandler(BaseCallbackHandler):
    """Apply a sanitized provider retry delay to the shared limiter."""

    run_inline = True

    def __init__(self, rate_limiter: GeminiRequestRateLimiter) -> None:
        self._rate_limiter = rate_limiter

    def on_llm_error(self, error: BaseException, **_: Any) -> None:
        """Defer subsequent calls after a Gemini 429 without retrying here."""
        observation = safe_gemini_quota_observation(error)
        if observation is None:
            return
        self._rate_limiter.defer_for(_quota_cooldown_seconds(observation, self._rate_limiter))


class GeminiQuotaRetryMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Retry one allowlisted transient Gemini failure through the paced boundary."""

    def __init__(self, rate_limiter: GeminiRequestRateLimiter) -> None:
        super().__init__()
        self._rate_limiter = rate_limiter

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Retry one synchronous quota failure after deferring the shared limiter."""
        try:
            return handler(request)
        except Exception as error:
            if not self._prepare_bounded_retry(error):
                raise
        try:
            return handler(request)
        except Exception as retry_error:
            if safe_gemini_quota_observation(retry_error) is not None:
                raise ProviderQuotaExhaustedError from retry_error
            if _is_transient_provider_error(retry_error):
                raise ProviderTransientExhaustedError from retry_error
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Retry one asynchronous quota failure after deferring the shared limiter."""
        try:
            return await handler(request)
        except Exception as error:
            if not self._prepare_bounded_retry(error):
                raise
        try:
            return await handler(request)
        except Exception as retry_error:
            if safe_gemini_quota_observation(retry_error) is not None:
                raise ProviderQuotaExhaustedError from retry_error
            if _is_transient_provider_error(retry_error):
                raise ProviderTransientExhaustedError from retry_error
            raise

    def _prepare_bounded_retry(self, error: BaseException) -> bool:
        observation = safe_gemini_quota_observation(error)
        if observation is not None:
            self._rate_limiter.defer_for(_quota_cooldown_seconds(observation, self._rate_limiter))
            return True
        return _is_transient_provider_error(error)


def _provider_status_code(error: BaseException) -> int | None:
    for candidate in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(error, "status", None),
    ):
        raw = getattr(candidate, "value", candidate)
        if not isinstance(raw, (int, str)) or isinstance(raw, bool):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _is_transient_provider_error(error: BaseException) -> bool:
    """Recognize only timeout and standard retryable HTTP failures through exception chains."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, TimeoutError):
            return True
        if _provider_status_code(current) in _TRANSIENT_PROVIDER_STATUS_CODES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _quota_cooldown_seconds(
    observation: GeminiQuotaObservation,
    rate_limiter: GeminiRequestRateLimiter,
) -> float:
    """Use a full rolling window for token quotas and RetryInfo for request quotas."""
    provider_delay = observation.retry_delay_seconds
    delay_seconds = _DEFAULT_429_COOLDOWN_SECONDS if provider_delay is None else provider_delay
    identifiers = tuple(
        identifier.casefold()
        for violation in observation.violations
        for identifier in (violation.quota_metric, violation.quota_id)
        if identifier is not None
    )
    if any(
        marker in identifier for marker in _INPUT_TOKEN_QUOTA_MARKERS for identifier in identifiers
    ):
        delay_seconds = max(delay_seconds, _WINDOW_SECONDS)
    return max(delay_seconds, rate_limiter.minimum_interval_seconds)


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SAFE_IDENTIFIER.fullmatch(candidate) else None


def _safe_provider_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if _SAFE_STATUS.fullmatch(candidate) else None


def _safe_quota_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _retry_delay_seconds(value: Any) -> float | None:
    seconds: float | None = None
    if isinstance(value, Mapping):
        raw_seconds = value.get("seconds", 0)
        raw_nanos = value.get("nanos", 0)
        try:
            seconds = float(raw_seconds) + (float(raw_nanos) / 1_000_000_000)
        except (TypeError, ValueError):
            return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", value)
        if match:
            seconds = float(match.group(1))

    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _quota_violation(value: Any) -> GeminiQuotaViolation | None:
    if not isinstance(value, Mapping):
        return None
    raw_dimensions = value.get("quotaDimensions", value.get("quota_dimensions", {}))
    dimensions: list[tuple[str, str]] = []
    if isinstance(raw_dimensions, Mapping):
        for raw_key, raw_value in raw_dimensions.items():
            key = str(raw_key)
            safe_value = _safe_identifier(raw_value)
            if key in _SAFE_DIMENSION_KEYS and safe_value is not None:
                dimensions.append((key, safe_value))
    dimensions.sort()

    violation = GeminiQuotaViolation(
        quota_metric=_safe_identifier(value.get("quotaMetric", value.get("quota_metric"))),
        quota_id=_safe_identifier(value.get("quotaId", value.get("quota_id"))),
        quota_dimensions=tuple(dimensions),
        quota_value=_safe_quota_value(value.get("quotaValue", value.get("quota_value"))),
    )
    if (
        violation.quota_metric is None
        and violation.quota_id is None
        and not violation.quota_dimensions
        and violation.quota_value is None
    ):
        return None
    return violation


def _structured_error_details(payload: Any) -> tuple[Any, list[Any]]:
    if not isinstance(payload, Mapping):
        return None, []
    error_payload = payload.get("error", payload)
    if not isinstance(error_payload, Mapping):
        return None, []
    raw_details = error_payload.get("details", [])
    details = list(raw_details) if isinstance(raw_details, list) else []
    return error_payload, details


def _observation_from_429(error: BaseException) -> GeminiQuotaObservation:
    payload = getattr(error, "details", None)
    error_payload, details = _structured_error_details(payload)
    provider_status = _safe_provider_status(getattr(error, "status", None))
    if isinstance(error_payload, Mapping):
        provider_status = _safe_provider_status(error_payload.get("status", provider_status))

    retry_delay: float | None = None
    violations: list[GeminiQuotaViolation] = []
    for detail in details:
        if not isinstance(detail, Mapping):
            continue
        detail_type = detail.get("@type", detail.get("type"))
        if isinstance(detail_type, str) and detail_type.endswith(_QUOTA_FAILURE_TYPE):
            raw_violations = detail.get("violations", [])
            if isinstance(raw_violations, list):
                for raw_violation in raw_violations[:_MAX_QUOTA_VIOLATIONS]:
                    violation = _quota_violation(raw_violation)
                    if violation is not None:
                        violations.append(violation)
        elif isinstance(detail_type, str) and detail_type.endswith(_RETRY_INFO_TYPE):
            retry_delay = _retry_delay_seconds(detail.get("retryDelay", detail.get("retry_delay")))

    if retry_delay is None:
        match = _RETRY_DELAY_TEXT.search(str(error)[:10_000])
        if match:
            retry_delay = _retry_delay_seconds(match.group("seconds"))

    return GeminiQuotaObservation(
        status_code=_HTTP_TOO_MANY_REQUESTS,
        provider_status=provider_status,
        retry_delay_seconds=retry_delay,
        violations=tuple(violations),
    )


def safe_gemini_quota_observation(
    error: BaseException,
) -> GeminiQuotaObservation | None:
    """Extract only allowlisted quota facts from a chained Gemini HTTP 429."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = getattr(current, "code", getattr(current, "status_code", None))
        if code == _HTTP_TOO_MANY_REQUESTS or str(code) == str(_HTTP_TOO_MANY_REQUESTS):
            return _observation_from_429(current)
        current = current.__cause__ or current.__context__
    return None


@lru_cache(maxsize=16)
def _shared_rate_limiter(
    requests_per_minute_limit: int,
    headroom_requests: int,
    max_wait_seconds: float,
) -> GeminiRequestRateLimiter:
    return GeminiRequestRateLimiter(
        requests_per_minute_limit=requests_per_minute_limit,
        headroom_requests=headroom_requests,
        max_wait_seconds=max_wait_seconds,
    )


def get_shared_gemini_rate_limiter(settings: Settings) -> GeminiRequestRateLimiter:
    """Return the one process-local limiter for the configured Gemini project."""
    return _shared_rate_limiter(
        settings.gemini_requests_per_minute_limit,
        settings.gemini_requests_per_minute_headroom,
        float(settings.gemini_rate_limit_wait_timeout_seconds),
    )
