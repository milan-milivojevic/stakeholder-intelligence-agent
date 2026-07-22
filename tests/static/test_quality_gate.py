"""Regression checks for the locked backend quality gate."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_backend_quality_gate_enforces_branch_coverage_floor() -> None:
    quality_script = (PROJECT_ROOT / "scripts" / "run-quality.ps1").read_text(encoding="utf-8")
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage_configuration = configuration["tool"]["coverage"]

    assert "--cov=stakeholder_intelligence_agent" in quality_script
    assert "--cov-report=term-missing" in quality_script
    assert coverage_configuration["run"]["branch"] is True
    assert coverage_configuration["report"]["fail_under"] == 80
