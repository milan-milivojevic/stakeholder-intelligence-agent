"""Physical artifact isolation behind virtual engagement/thread paths."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import TYPE_CHECKING, Any

from deepagents.backends import BackendProtocol, FilesystemBackend
from langgraph.config import get_config

from stakeholder_intelligence_agent.errors import (
    ArtifactNotFoundError,
    ArtifactPathError,
    ArtifactScopeError,
    ArtifactStateError,
)

if TYPE_CHECKING:
    from deepagents.backends.protocol import (
        EditResult,
        FileDownloadResponse,
        FileUploadResponse,
        GlobResult,
        GrepResult,
        LsResult,
        ReadResult,
        WriteResult,
    )

    from stakeholder_intelligence_agent.contracts.access import AccessContext

_ACTIVE_ARTIFACT_ACCESS: ContextVar[AccessContext | None] = ContextVar(
    "active_artifact_access",
    default=None,
)


def activate_artifact_access(access: AccessContext) -> None:
    """Bind the trusted graph scope for the concrete Deep Agents backend adapter."""
    if access.thread_id is None:
        raise ArtifactScopeError
    _ACTIVE_ARTIFACT_ACCESS.set(access)


class ScopedArtifactStore:
    """Use the Deep Agents filesystem backend inside one authorized scope."""

    def __init__(self, artifacts_root: Path) -> None:
        if not artifacts_root.is_absolute():
            raise ArtifactScopeError
        self._artifacts_root = artifacts_root
        self._scope_lock = RLock()
        self._graph_scopes: dict[str, AccessContext] = {}

    @property
    def artifacts_root(self) -> Path:
        """Return the already-resolved root used by trusted backend construction."""
        return self._artifacts_root

    def backend_root(self, access: AccessContext) -> Path:
        """Return one lexical backend root without filesystem I/O on the event loop."""
        if access.thread_id is None:
            raise ArtifactScopeError
        candidate = self._artifacts_root / access.engagement_id / access.thread_id
        self._require_beneath(candidate, self._artifacts_root)
        return candidate

    def backend(self, access: AccessContext) -> FilesystemBackend:
        """Build the same virtual filesystem backend used by the Deep Agent harness."""
        return FilesystemBackend(
            root_dir=self.backend_root(access),
            virtual_mode=True,
        )

    def register_graph_scope(self, access: AccessContext) -> None:
        """Register an immutable runtime thread mapping before graph filesystem work."""
        thread_id = access.thread_id
        if thread_id is None:
            raise ArtifactScopeError
        with self._scope_lock:
            existing = self._graph_scopes.get(thread_id)
            if existing is not None and existing != access:
                raise ArtifactScopeError
            self._graph_scopes[thread_id] = access

    def unregister_graph_scope(self, access: AccessContext) -> None:
        """Release the exact runtime mapping after graph streaming terminates."""
        thread_id = access.thread_id
        if thread_id is None:
            return
        with self._scope_lock:
            if self._graph_scopes.get(thread_id) == access:
                self._graph_scopes.pop(thread_id, None)

    def graph_access(self, thread_id: str) -> AccessContext:
        """Resolve only a previously registered server-owned graph scope."""
        with self._scope_lock:
            access = self._graph_scopes.get(thread_id)
        if access is None:
            raise ArtifactScopeError
        return access

    def scope_root(self, access: AccessContext, *, create: bool = False) -> Path:
        """Return the physical engagement/thread root without trusting a model path."""
        if access.thread_id is None:
            raise ArtifactScopeError
        candidate = (self._artifacts_root / access.engagement_id / access.thread_id).resolve()
        self._require_beneath(candidate, self._artifacts_root)
        if create:
            candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def resolve(
        self, access: AccessContext, virtual_path: str, *, create_parent: bool = False
    ) -> Path:
        """Resolve a normalized virtual path and reject traversal or host paths."""
        relative = self._validate_virtual_path(virtual_path)
        root = self.scope_root(access, create=create_parent)
        candidate = (root / Path(*relative.parts)).resolve()
        self._require_beneath(candidate, root)
        if create_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            self._require_beneath(candidate.parent.resolve(), root)
        return candidate

    def exists(self, access: AccessContext, virtual_path: str) -> bool:
        """Return whether an in-scope regular artifact exists."""
        normalized = self._normalized_virtual_path(virtual_path)
        result = self.backend(access).read(normalized, limit=1)
        return result.error is None and result.file_data is not None

    async def aexists(self, access: AccessContext, virtual_path: str) -> bool:
        """Check an artifact without blocking the async Agent Server event loop."""
        return await asyncio.to_thread(self.exists, access, virtual_path)

    def read_text(self, access: AccessContext, virtual_path: str) -> str:
        """Read an in-scope UTF-8 artifact."""
        normalized = self._normalized_virtual_path(virtual_path)
        result = self.backend(access).read(normalized, limit=1_000_000)
        if result.error is not None or result.file_data is None:
            raise ArtifactNotFoundError
        if result.file_data.get("encoding") != "utf-8":
            raise ArtifactStateError
        content = result.file_data.get("content")
        if not isinstance(content, str):
            raise ArtifactStateError
        return content

    async def aread_text(self, access: AccessContext, virtual_path: str) -> str:
        """Read text without blocking the async Agent Server event loop."""
        return await asyncio.to_thread(self.read_text, access, virtual_path)

    def read_json(self, access: AccessContext, virtual_path: str) -> Any:
        """Read and parse an in-scope JSON artifact."""
        return json.loads(self.read_text(access, virtual_path))

    async def aread_json(self, access: AccessContext, virtual_path: str) -> Any:
        """Read JSON without blocking the async Agent Server event loop."""
        return await asyncio.to_thread(self.read_json, access, virtual_path)

    def write_text(self, access: AccessContext, virtual_path: str, content: str) -> Path:
        """Create or replace one artifact through the scoped Deep Agents backend."""
        normalized = self._normalized_virtual_path(virtual_path)
        backend = self.backend(access)
        existing = backend.read(normalized, limit=1_000_000)
        if existing.error is None and existing.file_data is not None:
            if existing.file_data.get("encoding") != "utf-8":
                raise ArtifactStateError
            previous = existing.file_data.get("content")
            if not isinstance(previous, str):
                raise ArtifactStateError
            if previous != content:
                edited = backend.edit(normalized, previous, content)
                if edited.error is not None:
                    raise ArtifactStateError
        else:
            written = backend.write(normalized, content)
            if written.error is not None:
                raise ArtifactStateError
        return self.resolve(access, normalized)

    async def awrite_text(
        self,
        access: AccessContext,
        virtual_path: str,
        content: str,
    ) -> Path:
        """Write text without blocking the async Agent Server event loop."""
        return await asyncio.to_thread(self.write_text, access, virtual_path, content)

    def write_json(self, access: AccessContext, virtual_path: str, payload: Any) -> Path:
        """Atomically write deterministic UTF-8 JSON inside the authorized scope."""
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        return self.write_text(access, virtual_path, content)

    async def awrite_json(
        self,
        access: AccessContext,
        virtual_path: str,
        payload: Any,
    ) -> Path:
        """Write JSON without blocking the async Agent Server event loop."""
        return await asyncio.to_thread(self.write_json, access, virtual_path, payload)

    @staticmethod
    def _validate_virtual_path(virtual_path: str) -> PurePosixPath:
        if not virtual_path or "\\" in virtual_path or "\x00" in virtual_path:
            raise ArtifactPathError
        path = PurePosixPath(virtual_path)
        parts = tuple(part for part in path.parts if part != "/")
        if not parts or any(part in {"", ".", "..", "~"} for part in parts):
            raise ArtifactPathError
        if any(":" in part for part in parts):
            raise ArtifactPathError
        return PurePosixPath(*parts)

    @classmethod
    def _normalized_virtual_path(cls, virtual_path: str) -> str:
        """Return one canonical backend path after project-level validation."""
        return f"/{cls._validate_virtual_path(virtual_path).as_posix()}"

    @staticmethod
    def _require_beneath(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ArtifactScopeError from error


class ScopedFilesystemBackend(BackendProtocol):
    """Concrete protocol adapter delegating to the active scoped FilesystemBackend."""

    def __init__(self, artifacts: ScopedArtifactStore) -> None:
        self._artifacts = artifacts

    def _backend(self) -> FilesystemBackend:
        access: AccessContext | None = None
        try:
            thread_id = get_config().get("configurable", {}).get("thread_id")
        except RuntimeError:
            thread_id = None
        if isinstance(thread_id, str):
            access = self._artifacts.graph_access(thread_id)
        if access is None:
            access = _ACTIVE_ARTIFACT_ACCESS.get()
        if access is None:
            raise ArtifactScopeError
        return self._artifacts.backend(access)

    def ls(self, path: str) -> LsResult:
        return self._backend().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend().read(file_path, offset, limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        return self._backend().grep(pattern, path, glob)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._backend().glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._backend().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self._backend().edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._backend().upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend().download_files(paths)
