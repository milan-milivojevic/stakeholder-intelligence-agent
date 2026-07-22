"""Shared canonical contract primitives."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

OpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
OriginalText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200_000),
]
ExternalFilename = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\\\x00\r\n]+$"),
]


def _lower_string(value: object) -> object:
    """Normalize case before constrained-string pattern validation."""
    return value.lower() if isinstance(value, str) else value


ContentHash = Annotated[
    str,
    BeforeValidator(_lower_string),
    StringConstraints(pattern=r"^[a-f0-9]{64}$"),
]
FailureCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z][A-Z0-9_]{0,63}$"),
]
Permission = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*$",
    ),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _normalize_utc(value: datetime) -> datetime:
    """Normalize every accepted aware timestamp to UTC."""
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_normalize_utc)]


class CanonicalModel(BaseModel):
    """Immutable, closed base for every first-party domain contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)
