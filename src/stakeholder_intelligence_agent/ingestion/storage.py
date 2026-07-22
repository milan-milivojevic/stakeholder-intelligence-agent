"""Controlled original and derived storage outside the agent filesystem backend."""

from __future__ import annotations

import os
import re
import secrets
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.errors import ArtifactPathError, ArtifactScopeError
from stakeholder_intelligence_agent.ingestion.types import ArtifactDraft, StoredArtifact

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.ingestion.types import ValidatedUpload

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MIN_VIRTUAL_PARTS = 2


class IngestionArtifactStore:
    """Atomically preserve originals and derivatives in an engagement-owned tree."""

    def __init__(self, originals_root: Path, derived_root: Path) -> None:
        self._originals_root = originals_root.resolve()
        self._derived_root = derived_root.resolve()

    def write_original(
        self,
        access: AccessContext,
        *,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
        upload: ValidatedUpload,
    ) -> tuple[StoredArtifact, Path]:
        """Write immutable source bytes once and verify byte-for-byte idempotency."""
        self._authorize(access)
        extension = ".jpg" if upload.document_type == "jpeg" else f".{upload.document_type}"
        relative = self._relative_path(
            access.engagement_id,
            document_id,
            document_version_id,
            f"original{extension}",
        )
        path = self._resolve(self._originals_root, relative, create_parent=True)
        self._write_immutable(path, upload.content, expected_hash=upload.content_hash)
        return (
            StoredArtifact(
                artifact_id=artifact_id,
                engagement_id=access.engagement_id,
                document_version_id=document_version_id,
                artifact_kind="original",
                virtual_path=f"originals/{relative.as_posix()}",
                media_type=upload.media_type,
                content_hash=upload.content_hash,
            ),
            path,
        )

    def write_derived(
        self,
        access: AccessContext,
        *,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
        draft: ArtifactDraft,
    ) -> StoredArtifact:
        """Write one deterministic derived artifact under its immutable ID."""
        self._authorize(access)
        suffix = draft.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            raise ArtifactPathError
        relative = self._relative_path(
            access.engagement_id,
            document_id,
            document_version_id,
            f"{artifact_id}{suffix}",
        )
        path = self._resolve(self._derived_root, relative, create_parent=True)
        digest = sha256(draft.content).hexdigest()
        self._write_immutable(path, draft.content, expected_hash=digest)
        return StoredArtifact(
            artifact_id=artifact_id,
            engagement_id=access.engagement_id,
            document_version_id=document_version_id,
            artifact_kind=draft.artifact_kind,
            virtual_path=f"derived/{relative.as_posix()}",
            media_type=draft.media_type,
            content_hash=digest,
        )

    def resolve_virtual(self, access: AccessContext, virtual_path: str) -> Path:
        """Resolve an authorized controlled-storage reference for source drill-down."""
        self._authorize(access, permission="source:read")
        path = PurePosixPath(virtual_path)
        if len(path.parts) < _MIN_VIRTUAL_PARTS or path.parts[0] not in {
            "originals",
            "derived",
        }:
            raise ArtifactPathError
        relative = PurePosixPath(*path.parts[1:])
        if not relative.parts or relative.parts[0] != access.engagement_id:
            raise ArtifactScopeError
        root = self._originals_root if path.parts[0] == "originals" else self._derived_root
        return self._resolve(root, relative)

    @staticmethod
    def _authorize(access: AccessContext, *, permission: str = "document:upload") -> None:
        access.require_permission(permission)

    @staticmethod
    def _relative_path(
        engagement_id: str,
        document_id: str,
        document_version_id: str,
        filename: str,
    ) -> PurePosixPath:
        for value in (engagement_id, document_id, document_version_id):
            if not _SAFE_ID.fullmatch(value):
                raise ArtifactPathError
        if not filename or "/" in filename or "\\" in filename or "\x00" in filename:
            raise ArtifactPathError
        return PurePosixPath(engagement_id, document_id, document_version_id, filename)

    @staticmethod
    def _resolve(
        root: Path,
        relative: PurePosixPath,
        *,
        create_parent: bool = False,
    ) -> Path:
        if any(part in {"", ".", "..", "~"} or ":" in part for part in relative.parts):
            raise ArtifactPathError
        candidate = (root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ArtifactScopeError from error
        if create_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            try:
                candidate.parent.resolve().relative_to(root)
            except ValueError as error:
                raise ArtifactScopeError from error
        return candidate

    @staticmethod
    def _write_immutable(path: Path, content: bytes, *, expected_hash: str) -> None:
        if path.exists():
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected_hash:
                raise ArtifactScopeError
            return
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
