"""Six-format upload validation with independent extension, MIME, and signature checks."""

from __future__ import annotations

import io
import unicodedata
import warnings
import zipfile
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from PIL import Image
from pydantic import TypeAdapter, ValidationError

from stakeholder_intelligence_agent.contracts.common import ExternalFilename
from stakeholder_intelligence_agent.errors import (
    CorruptSourceError,
    MediaTypeMismatchError,
    UnsupportedDocumentTypeError,
    UploadSizeError,
)
from stakeholder_intelligence_agent.ingestion.types import ValidatedUpload

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.source import DocumentType

_FILENAME_ADAPTER = TypeAdapter(ExternalFilename)
_EXTENSIONS: Final[dict[str, DocumentType]] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}
_MEDIA_TYPES: Final[dict[DocumentType, str]] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpeg": "image/jpeg",
}
_OOXML_MARKERS: Final[dict[DocumentType, str]] = {
    "docx": "word/document.xml",
    "pptx": "ppt/presentation.xml",
    "xlsx": "xl/workbook.xml",
}


class UploadValidator:
    """Validate the untrusted upload envelope and format-specific structure."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate_envelope(
        self,
        *,
        filename: str,
        declared_media_type: str,
        content: bytes,
    ) -> ValidatedUpload:
        """Validate size, filename, allowlist, MIME, and detected byte signature."""
        if not content or len(content) > self._settings.max_upload_bytes:
            raise UploadSizeError
        try:
            external_filename = _FILENAME_ADAPTER.validate_python(filename)
        except ValidationError as error:
            raise UnsupportedDocumentTypeError from error
        suffix = self._suffix(external_filename)
        document_type = _EXTENSIONS.get(suffix)
        if document_type is None:
            raise UnsupportedDocumentTypeError

        normalized_media_type = declared_media_type.strip().lower()
        if normalized_media_type != _MEDIA_TYPES[document_type]:
            raise MediaTypeMismatchError
        detected = self._detect_type(content)
        if detected != document_type:
            raise MediaTypeMismatchError

        normalized_filename = unicodedata.normalize("NFKC", external_filename).casefold()
        return ValidatedUpload(
            filename=external_filename,
            normalized_filename=normalized_filename,
            document_type=document_type,
            media_type=normalized_media_type,
            content=content,
            content_hash=sha256(content).hexdigest(),
        )

    def validate_structure(self, upload: ValidatedUpload) -> None:
        """Perform bounded structural checks after the stable version is recorded."""
        if upload.document_type == "pdf":
            if b"%%EOF" not in upload.content[-4_096:]:
                raise CorruptSourceError
            return
        if upload.document_type in {"docx", "pptx", "xlsx"}:
            self._validate_ooxml(upload)
            return
        self._validate_image(upload)

    @staticmethod
    def _suffix(filename: str) -> str:
        dot = filename.rfind(".")
        return filename[dot:].lower() if dot >= 0 else ""

    @staticmethod
    def _detect_type(content: bytes) -> DocumentType | None:
        if content.startswith(b"%PDF-"):
            return "pdf"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if content.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    names = set(archive.namelist())
            except (OSError, zipfile.BadZipFile):
                return None
            for document_type, marker in _OOXML_MARKERS.items():
                if marker in names:
                    return document_type
        return None

    def _validate_ooxml(self, upload: ValidatedUpload) -> None:
        marker = _OOXML_MARKERS[upload.document_type]
        try:
            with zipfile.ZipFile(io.BytesIO(upload.content)) as archive:
                infos = archive.infolist()
                if len(infos) > self._settings.max_archive_entries:
                    raise CorruptSourceError
                total_size = 0
                names: set[str] = set()
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if (
                        not path.parts
                        or path.is_absolute()
                        or any(part in {"", ".", "..", "~"} for part in path.parts)
                    ):
                        raise CorruptSourceError
                    names.add(info.filename)
                    total_size += info.file_size
                    if total_size > self._settings.max_archive_uncompressed_bytes:
                        raise CorruptSourceError
                if "[Content_Types].xml" not in names or marker not in names:
                    raise MediaTypeMismatchError
                if archive.testzip() is not None:
                    raise CorruptSourceError
        except MediaTypeMismatchError:
            raise
        except CorruptSourceError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise CorruptSourceError from error

    def _validate_image(self, upload: ValidatedUpload) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(upload.content)) as image:
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise CorruptSourceError
                    if width * height > self._settings.max_image_pixels:
                        raise CorruptSourceError
                    expected = "JPEG" if upload.document_type == "jpeg" else "PNG"
                    if image.format != expected:
                        raise MediaTypeMismatchError
                    image.verify()
        except (MediaTypeMismatchError, CorruptSourceError):
            raise
        except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as error:
            raise CorruptSourceError from error
