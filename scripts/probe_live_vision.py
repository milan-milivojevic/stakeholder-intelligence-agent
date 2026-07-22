"""Run one sanitized Gemini vision probe without emitting configured values."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.contracts.source import ImageRegionLocation
from stakeholder_intelligence_agent.ingestion.adapters import GeminiVisionEnricher

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ingestion" / "alpha-organization-chart.png"
HTTP_STATUS_MIN = 100
HTTP_STATUS_MAX = 599
OUT_OF_SCOPE_OUTPUT = "Probe output must remain inside the project."
SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")


def _safe_model_identity(model_id: str) -> str:
    """Expose only a validated non-secret model identifier in test evidence."""
    return model_id if SAFE_MODEL_ID.fullmatch(model_id) else "redacted-invalid-model-id"


def _safe_exception_chain(error: BaseException) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        item: dict[str, Any] = {
            "module": type(current).__module__,
            "type": type(current).__name__,
        }
        statuses: list[str | int] = []
        for attribute in ("status_code", "code"):
            value = getattr(current, attribute, None)
            if isinstance(value, int) and HTTP_STATUS_MIN <= value <= HTTP_STATUS_MAX:
                statuses.append(value)
            elif isinstance(value, Enum):
                statuses.append(value.name)
            elif re.fullmatch(r"[1-5][0-9]{2}", str(value)):
                statuses.append(int(str(value)))
        if statuses:
            item["statuses"] = statuses
        chain.append(item)
        current = current.__cause__ or current.__context__
    return chain


async def _run() -> dict[str, Any]:
    started = time.monotonic()
    model_identity = "unavailable"
    try:
        settings = Settings()
        model_identity = _safe_model_identity(settings.gemini_vision_model)
        description = await GeminiVisionEnricher(settings).describe(
            content=FIXTURE.read_bytes(),
            media_type="image/png",
            filename=FIXTURE.name,
            location=ImageRegionLocation(
                filename=FIXTURE.name,
                image_index=1,
                region="whole_image",
            ),
        )
    except BaseException as error:  # noqa: BLE001
        return {
            "result": "FAIL",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model_identity": model_identity,
            "exception_chain": _safe_exception_chain(error),
        }
    return {
        "result": "PASS",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model_identity": model_identity,
        "description_characters": len(description),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    try:
        output.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise SystemExit(OUT_OF_SCOPE_OUTPUT) from error
    output.parent.mkdir(parents=True, exist_ok=True)
    logging.disable(logging.CRITICAL)
    result = asyncio.run(_run())
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Vision probe result: {result['result']}")
    print(f"Evidence: {output.relative_to(PROJECT_ROOT)}")
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
