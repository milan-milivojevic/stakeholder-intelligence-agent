"""Isolated real-process runtimes for the production React browser tests."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

PROJECT_ROOT = Path(__file__).parents[2].resolve()
AGENT_PORT = 2024
QDRANT_PORT = 6333
PROCESS_STOP_SECONDS = 20
SERVICE_START_SECONDS = 180


def _random_secret() -> str:
    return secrets.token_urlsafe(48)


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_http(url: str, process: subprocess.Popen[str] | None = None) -> None:
    deadline = time.monotonic() + SERVICE_START_SECONDS
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("A required local test process exited during startup.")
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("A required local test service did not become healthy in time.")


def _stop_process(process: subprocess.Popen[str] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=PROCESS_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=PROCESS_STOP_SECONDS)
    return process.returncode


@dataclass
class BrowserRuntime:
    """Own the real local services and non-persisted test security material."""

    run_id: str
    evidence_dir: Path
    runtime_root: Path
    pm_bootstrap_secret: str = field(repr=False)
    token_pepper: str = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    transient_secrets: list[str] = field(default_factory=list, repr=False)
    agent_process: subprocess.Popen[str] | None = field(default=None, repr=False)
    _agent_log: TextIO | None = field(default=None, repr=False)
    restart_count: int = 0

    @property
    def agent_url(self) -> str:
        return f"http://127.0.0.1:{AGENT_PORT}"

    @property
    def known_secrets(self) -> tuple[str, ...]:
        return (self.pm_bootstrap_secret, self.token_pepper, *self.transient_secrets)

    def track_secret(self, value: str) -> None:
        """Keep a runtime-only value available for post-run evidence scanning and redaction."""
        if value and value not in self.transient_secrets:
            self.transient_secrets.append(value)

    def start_agent(
        self,
        *,
        config_path: Path | None = None,
        allow_blocking: bool = False,
    ) -> None:
        executable = PROJECT_ROOT / ".venv" / "Scripts" / "langgraph.exe"
        if not executable.is_file():
            raise RuntimeError("The locked LangGraph CLI is unavailable.")
        self._agent_log = (self.evidence_dir / "logs" / "agent-server.log").open(
            "a",
            encoding="utf-8",
            errors="replace",
        )
        arguments = [
            str(executable),
            "dev",
            "--no-browser",
            "--no-reload",
            "--host",
            "127.0.0.1",
            "--port",
            str(AGENT_PORT),
        ]
        if config_path is not None:
            arguments.extend(["--config", str(config_path)])
        if allow_blocking:
            arguments.append("--allow-blocking")
        self.agent_process = subprocess.Popen(  # noqa: S603
            arguments,
            cwd=PROJECT_ROOT,
            env=self.environment,
            stdout=self._agent_log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _wait_for_http(
            f"{self.agent_url}/api/v1/system/health",
            self.agent_process,
        )

    def start_acceptance_backend(self) -> None:
        """Start the real API/domain stack with deterministic test-only model doubles."""
        executable = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        runner = PROJECT_ROOT / "scripts" / "run_ui_test_backend.py"
        if not executable.is_file():
            raise RuntimeError("The locked Python executable is unavailable.")
        if not runner.is_file():
            raise RuntimeError("The deterministic UI acceptance backend is unavailable.")
        self._agent_log = (self.evidence_dir / "logs" / "acceptance-backend.log").open(
            "a",
            encoding="utf-8",
            errors="replace",
        )
        self.agent_process = subprocess.Popen(  # noqa: S603
            [str(executable), "-m", "scripts.run_ui_test_backend", "--port", str(AGENT_PORT)],
            cwd=PROJECT_ROOT,
            env=self.environment,
            stdout=self._agent_log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _wait_for_http(
            f"{self.agent_url}/api/v1/system/health",
            self.agent_process,
        )

    def stop_agent(self) -> int | None:
        return_code = _stop_process(self.agent_process)
        self.agent_process = None
        if self._agent_log is not None:
            self._agent_log.close()
            self._agent_log = None
        return return_code

    def restart_agent(self) -> None:
        self.stop_agent()
        deadline = time.monotonic() + PROCESS_STOP_SECONDS
        while _port_is_open(AGENT_PORT) and time.monotonic() < deadline:
            time.sleep(0.25)
        if _port_is_open(AGENT_PORT):
            raise RuntimeError("The Agent Server port remained occupied after shutdown.")
        self.restart_count += 1
        self.start_agent()


def _run_docker(arguments: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the real Qdrant browser test service.")
    result = subprocess.run(  # noqa: S603
        [docker, "compose", *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    with log_path.open("a", encoding="utf-8", errors="replace") as stream:
        stream.write(result.stdout)
        stream.write(result.stderr)
    return result


def _build_runtime(run_id: str, evidence_dir: Path) -> BrowserRuntime:
    runtime_root = PROJECT_ROOT / ".cache" / "browser-e2e-runtime" / run_id
    data_root = runtime_root / "data"
    for path in (
        evidence_dir,
        evidence_dir / "logs",
        evidence_dir / "screenshots",
        evidence_dir / "traces",
        evidence_dir / "accessibility",
        runtime_root,
        data_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
    pm_secret = _random_secret()
    pepper = _random_secret()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONUTF8": "1",
            "TEMP": str(runtime_root / "temp"),
            "TMP": str(runtime_root / "temp"),
            "STAKEHOLDER_AI_ENVIRONMENT": "test",
            "STAKEHOLDER_AI_LOG_LEVEL": "WARNING",
            "LOG_LEVEL": "WARNING",
            "STAKEHOLDER_AI_PM_BOOTSTRAP_TOKEN": pm_secret,
            "STAKEHOLDER_AI_TOKEN_PEPPER": pepper,
            "STAKEHOLDER_AI_INVITATION_TTL_MINUTES": "5",
            "STAKEHOLDER_AI_DATA_ROOT": str(data_root),
            "STAKEHOLDER_AI_DOMAIN_DATABASE": str(data_root / "domain.sqlite3"),
            "STAKEHOLDER_AI_CHECKPOINT_DATABASE": str(data_root / "checkpoints.sqlite3"),
            "STAKEHOLDER_AI_ORIGINALS_ROOT": str(data_root / "originals"),
            "STAKEHOLDER_AI_DERIVED_ROOT": str(data_root / "derived"),
            "STAKEHOLDER_AI_AGENT_ARTIFACTS_ROOT": str(data_root / "agent-artifacts"),
            "STAKEHOLDER_AI_AUDIT_ROOT": str(data_root / "audit"),
            "STAKEHOLDER_AI_MODEL_CACHE_ROOT": str(PROJECT_ROOT / ".cache"),
            "STAKEHOLDER_AI_QDRANT_URL": f"http://127.0.0.1:{QDRANT_PORT}",
            "STAKEHOLDER_AI_QDRANT_COLLECTION": f"browser_e2e_{run_id.lower()}",
            "LANGSMITH_TRACING": "false",
        }
    )
    (runtime_root / "temp").mkdir(parents=True, exist_ok=True)
    return BrowserRuntime(
        run_id=run_id,
        evidence_dir=evidence_dir,
        runtime_root=runtime_root,
        pm_bootstrap_secret=pm_secret,
        token_pepper=pepper,
        environment=environment,
    )


def _sanitize_evidence_logs(runtime: BrowserRuntime) -> None:
    replacements = runtime.known_secrets
    for path in (runtime.evidence_dir / "logs").glob("*.log"):
        content = path.read_text(encoding="utf-8", errors="replace")
        for secret in replacements:
            content = content.replace(secret, "[redacted-local-test-secret]")
        content = re.sub(
            r"(?i)gemini[-A-Za-z0-9._]+",
            "[redacted-gemini-model]",
            content,
        )
        content = re.sub(
            r"(?i)(/models/)[^:\"\s]+",
            r"\1[redacted-model]",
            content,
        )
        path.write_text(content, encoding="utf-8")


def _scan_known_secret_artifacts(runtime: BrowserRuntime) -> dict[str, object]:
    encoded = tuple(secret.encode("utf-8") for secret in runtime.known_secrets if secret)
    inspected_files = 0
    matches = 0
    for root in (runtime.evidence_dir, runtime.runtime_root):
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            inspected_files += 1
            payload = path.read_bytes()
            matches += sum(payload.count(secret) for secret in encoded)
    return {
        "inspected_artifacts": inspected_files,
        "known_raw_secret_matches": matches,
        "source_maps_present": any(
            path.suffix.casefold() == ".map"
            for path in (PROJECT_ROOT / "frontend" / "dist").rglob("*")
            if path.is_file()
        ),
    }


@pytest.fixture(scope="session")
def react_browser_runtime() -> Iterator[BrowserRuntime]:
    """Start the built React SPA on the real isolated LangGraph Agent Server origin."""
    run_id = os.environ.get("STAKEHOLDER_REACT_E2E_RUN_ID", "manual-react").strip()
    evidence_value = os.environ.get("STAKEHOLDER_REACT_E2E_EVIDENCE_DIR", "").strip()
    if not evidence_value:
        pytest.fail("Run the React suite through scripts/run-react-browser-e2e.ps1.")
    evidence_dir = Path(evidence_value).resolve()
    try:
        evidence_dir.relative_to(PROJECT_ROOT)
    except ValueError:
        pytest.fail("The React browser evidence directory must remain inside the project.")
    if _port_is_open(AGENT_PORT):
        pytest.fail("The Agent Server browser-test port is already occupied.")
    if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.fail("Build the locked React frontend before running browser verification.")

    runtime = _build_runtime(run_id, evidence_dir)
    runtime.environment.update(
        {
            "GOOGLE_API_KEY": _random_secret(),
            "STAKEHOLDER_AI_GEMINI_PRIMARY_CHAT_MODEL": "gemini-test-primary",
            "STAKEHOLDER_AI_GEMINI_FALLBACK_CHAT_MODEL": "gemini-test-fallback",
            "STAKEHOLDER_AI_GEMINI_VISION_MODEL": "gemini-test-vision",
            "STAKEHOLDER_AI_GEMINI_EMBEDDING_MODEL": "gemini-test-embedding",
            "STAKEHOLDER_AI_BROWSER_ORIGIN": f"http://127.0.0.1:{AGENT_PORT}",
        }
    )
    docker_log = evidence_dir / "logs" / "qdrant.log"
    status = _run_docker(["ps", "--services", "--status", "running"], docker_log)
    qdrant_was_running = status.returncode == 0 and "qdrant" in status.stdout.split()
    started = _run_docker(["up", "-d", "qdrant"], docker_log)
    if started.returncode != 0:
        pytest.fail("The project-local Qdrant service could not be started.")
    _wait_for_http(f"http://127.0.0.1:{QDRANT_PORT}/healthz")

    agent_return_code: int | None = None
    try:
        runtime.start_agent()
        yield runtime
    finally:
        agent_return_code = runtime.stop_agent()
        if not qdrant_was_running:
            _run_docker(["stop", "qdrant"], docker_log)
        _sanitize_evidence_logs(runtime)
        summary = {
            "run_id": run_id,
            "services": {
                "agent_server": "real local LangGraph Agent Server",
                "frontend": "built React SPA served by the Agent Server",
                "browser": "Chromium via Python Playwright",
                "qdrant": "qdrant/qdrant:v1.18.2-unprivileged",
            },
            "agent_restart_count": runtime.restart_count,
            "process_return_codes_after_controlled_stop": {"agent_server": agent_return_code},
            "runtime_root": str(runtime.runtime_root.relative_to(PROJECT_ROOT)),
        }
        (evidence_dir / "runtime.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        security_scan = _scan_known_secret_artifacts(runtime)
        (evidence_dir / "artifact-secret-scan.json").write_text(
            json.dumps(security_scan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if security_scan["known_raw_secret_matches"] != 0:
            pytest.fail("Raw browser-test security material remained in retained artifacts.")
        if security_scan["source_maps_present"] is not False:
            pytest.fail("Production source maps remained in the React build.")


@pytest.fixture(scope="session")
def react_preflight_runtime() -> Iterator[BrowserRuntime]:
    """Run deterministic custom services inside the real LangGraph development host."""
    run_id = os.environ.get("STAKEHOLDER_REACT_E2E_RUN_ID", "manual-browser-e2e").strip()
    evidence_value = os.environ.get("STAKEHOLDER_REACT_E2E_EVIDENCE_DIR", "").strip()
    if not evidence_value:
        pytest.fail("Run the React suite through scripts/run-react-browser-e2e.ps1.")
    evidence_dir = Path(evidence_value).resolve()
    try:
        evidence_dir.relative_to(PROJECT_ROOT)
    except ValueError:
        pytest.fail("The React browser evidence directory must remain inside the project.")
    if _port_is_open(AGENT_PORT):
        pytest.fail("The Agent Server browser-test port is already occupied.")
    if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.fail("Build the locked React frontend before running browser verification.")

    config_path = PROJECT_ROOT / "langgraph.e2e.json"
    if not config_path.is_file():
        pytest.fail("The LangGraph browser-test configuration is unavailable.")
    runtime = _build_runtime(run_id, evidence_dir)
    runtime.environment.update(
        {
            "GOOGLE_API_KEY": _random_secret(),
            "STAKEHOLDER_AI_GEMINI_PRIMARY_CHAT_MODEL": "gemini-test-primary",
            "STAKEHOLDER_AI_GEMINI_FALLBACK_CHAT_MODEL": "gemini-test-fallback",
            "STAKEHOLDER_AI_GEMINI_VISION_MODEL": "gemini-test-vision",
            "STAKEHOLDER_AI_GEMINI_EMBEDDING_MODEL": "gemini-test-embedding",
            "STAKEHOLDER_AI_BROWSER_ORIGIN": f"http://127.0.0.1:{AGENT_PORT}",
            "UI_TEST_PM_BOOTSTRAP_TOKEN": runtime.pm_bootstrap_secret,
            "UI_TEST_DATA_ROOT": str(runtime.runtime_root / "data"),
            "STAKEHOLDER_E2E_PORT": str(AGENT_PORT),
        }
    )
    agent_return_code: int | None = None
    try:
        runtime.start_agent(config_path=config_path)
        yield runtime
    finally:
        agent_return_code = runtime.stop_agent()
        _sanitize_evidence_logs(runtime)
        summary = {
            "run_id": run_id,
            "services": {
                "host": "real local LangGraph Agent Server",
                "http_app": "real custom FastAPI routes on the Agent Server origin",
                "frontend": "built React SPA served by the Agent Server",
                "browser": "Chromium via Python Playwright",
                "domain": "real SQLite persistence, checkpointer, ingestion, and interview graph",
                "provider_boundary": "deterministic test doubles; no live Gemini",
                "vector_store": "in-memory Qdrant client through the production adapter",
            },
            "process_return_codes_after_controlled_stop": {"agent_server": agent_return_code},
            "runtime_root": str(runtime.runtime_root.relative_to(PROJECT_ROOT)),
        }
        (evidence_dir / "runtime.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        security_scan = _scan_known_secret_artifacts(runtime)
        (evidence_dir / "artifact-secret-scan.json").write_text(
            json.dumps(security_scan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if security_scan["known_raw_secret_matches"] != 0:
            pytest.fail("Raw browser-test security material remained in retained artifacts.")
        if security_scan["source_maps_present"] is not False:
            pytest.fail("Production source maps remained in the React build.")


@pytest.fixture(scope="session")
def react_acceptance_runtime() -> Iterator[BrowserRuntime]:
    """Start the built React SPA on the real deterministic acceptance backend."""
    run_id = os.environ.get("STAKEHOLDER_REACT_E2E_RUN_ID", "manual-react-acceptance").strip()
    evidence_value = os.environ.get("STAKEHOLDER_REACT_E2E_EVIDENCE_DIR", "").strip()
    if not evidence_value:
        pytest.fail("Run the React suite through scripts/run-react-browser-e2e.ps1.")
    evidence_dir = Path(evidence_value).resolve()
    try:
        evidence_dir.relative_to(PROJECT_ROOT)
    except ValueError:
        pytest.fail("The React browser evidence directory must remain inside the project.")
    if _port_is_open(AGENT_PORT):
        pytest.fail("The Agent Server browser-test port is already occupied.")
    if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.fail("Build the locked React frontend before running browser verification.")

    runtime = _build_runtime(run_id, evidence_dir)
    runtime.environment.update(
        {
            "UI_TEST_PM_BOOTSTRAP_TOKEN": runtime.pm_bootstrap_secret,
            "UI_TEST_DATA_ROOT": str(runtime.runtime_root / "data"),
        }
    )
    backend_return_code: int | None = None
    try:
        runtime.start_acceptance_backend()
        yield runtime
    finally:
        backend_return_code = runtime.stop_agent()
        _sanitize_evidence_logs(runtime)
        summary = {
            "run_id": run_id,
            "services": {
                "backend": (
                    "real local FastAPI domain, retrieval, persistence, and Deep Agent stack"
                ),
                "model_boundary": "deterministic test-only model doubles; no live Gemini",
                "frontend": "built React SPA served by the same FastAPI origin",
                "browser": "Chromium via Python Playwright",
                "vector_store": "in-memory Qdrant client through the production adapter",
            },
            "process_return_codes_after_controlled_stop": {
                "acceptance_backend": backend_return_code
            },
            "runtime_root": str(runtime.runtime_root.relative_to(PROJECT_ROOT)),
        }
        (evidence_dir / "runtime.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        security_scan = _scan_known_secret_artifacts(runtime)
        (evidence_dir / "artifact-secret-scan.json").write_text(
            json.dumps(security_scan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if security_scan["known_raw_secret_matches"] != 0:
            pytest.fail("Raw browser-test security material remained in retained artifacts.")
        if security_scan["source_maps_present"] is not False:
            pytest.fail("Production source maps remained in the React build.")
