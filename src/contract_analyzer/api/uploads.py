"""Getting a client's bytes onto disk without trusting anything about them.

Two things arrive from outside and neither can be used as it is.

**The filename.** `UploadFile.filename` is whatever the client put in the
multipart header. `../../../.env` is a path, and a uuid prefix does not fix it:
`<uuid>-../../x.pdf` still resolves upward. So the name is reduced to a
basename, stripped to a safe alphabet, truncated, and the assembled path is
checked to be inside `RAW_DIR` before anything is opened for writing. The
*original* name is kept -- in the database, where it is data rather than a path,
and where it is what `GET /documents` shows the user.

**The size.** The cap is enforced chunk by chunk as the body is written, not
after it is in memory: `await file.read()` on an endpoint that is open by
default is a one-line way to run the container out of memory. Over the cap, the
partial file is deleted before the error is raised -- a rejected upload must not
leave bytes behind.

A uuid goes in front of every stored name so that two sessions uploading the
same contract get two documents. `ingest_file` keys uniqueness on path, so a
unique path is exactly how a unique `document_id` is obtained.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path
from typing import Protocol

from fastapi import status

from ..logger import get_logger
from .errors import ApiError

log = get_logger(__name__)

#: What survives sanitisation. Everything else becomes an underscore, so a name
#: cannot carry a separator, a null, a control character or a shell surprise.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
#: Leading dots would make a hidden file, and `..` is the whole problem.
_LEADING_JUNK = re.compile(r"^[.\s_-]+")
_MAX_NAME = 100
#: Read the body in 64 KiB pieces: small enough that the cap is enforced long
#: before memory is a question, large enough not to syscall per kilobyte.
_CHUNK = 64 * 1024
FALLBACK_NAME = "upload.pdf"


class Upload(Protocol):
    """What this module needs of a `fastapi.UploadFile`. A protocol, so the
    tests can hand it a few bytes without building a multipart request."""

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


def sanitize_filename(name: str | None, *, suffix: str = ".pdf") -> str:
    """A client's filename reduced to something that is only ever a filename.

    `Path(name).name` drops any directory part -- including the whole of
    `../../../etc/passwd` -- and what remains is stripped to
    `[A-Za-z0-9._-]`, unslashed by Unicode normalisation first so that a
    look-alike character cannot smuggle a separator through. The result always
    ends in `suffix`, and is never empty.
    """
    raw = (name or "").strip()
    # NFKC first: a fullwidth solidus normalises to '/', and it is better for
    # `Path(...).name` to see it as a separator than for the alphabet filter to
    # quietly turn it into an underscore inside what looks like one name.
    raw = unicodedata.normalize("NFKC", raw)
    raw = raw.replace("\\", "/")
    base = Path(raw).name
    base = _SAFE.sub("_", base)
    base = _LEADING_JUNK.sub("", base)
    if base.lower().endswith(suffix.lower()):
        base = base[: -len(suffix)]
    base = base[:_MAX_NAME].rstrip("._-")
    return f"{base}{suffix}" if base else FALLBACK_NAME


def stored_path(raw_dir: Path, filename: str | None, *, suffix: str = ".pdf") -> Path:
    """Where an upload is written: `RAW_DIR/<uuid>-<sanitized name>`.

    The uuid is what makes two uploads of the same bytes two documents;
    `ingest_file` keys uniqueness on path, so a fresh path is a fresh id. The
    containment check is belt and braces over `sanitize_filename` -- cheap, and
    the one assertion that must hold whatever the sanitiser missed.
    """
    raw_dir = raw_dir.resolve()
    path = raw_dir / f"{uuid.uuid4().hex[:12]}-{sanitize_filename(filename, suffix=suffix)}"
    if not path.resolve().is_relative_to(raw_dir):  # pragma: no cover - unreachable by design
        raise ApiError(
            status.HTTP_400_BAD_REQUEST, "invalid_filename",
            "The uploaded filename does not resolve to a path inside the upload directory.",
            "Send a plain filename, without directory separators.",
        )
    return path


async def save_upload(upload: Upload, path: Path, *, max_bytes: int) -> int:
    """Stream the body to `path`, refusing to write more than `max_bytes`.

    Returns the number of bytes written. Raises `ApiError` 413 -- after
    deleting the partial file -- when the body is larger, and 422 when it is
    empty. Nothing is ever held whole in memory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with path.open("wb") as out:
            while chunk := await upload.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError(
                        status.HTTP_413_CONTENT_TOO_LARGE, "payload_too_large",
                        f"The upload is larger than the {max_bytes // (1024 * 1024)} MB limit.",
                        "Raise api_max_upload_mb in settings.json, or send a smaller file.",
                    )
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise

    if written == 0:
        path.unlink(missing_ok=True)
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation", "The uploaded file is empty."
        )
    log.info("api.upload", extra={"bytes": written, "path": path.name})
    return written


def require_pdf(filename: str | None, content_type: str | None) -> None:
    """The parser is PDF-only, so anything else is refused before it is stored.

    Judged on the extension, not the declared content type: browsers send
    `application/octet-stream` for a PDF often enough that trusting the header
    would reject real uploads, while a client that lies about the extension
    fails in the parser a second later with a worse message.
    """
    name = (filename or "").lower()
    if not name.endswith(".pdf"):
        raise ApiError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type",
            f"Only PDF files are supported; got {filename or 'a file with no name'}.",
            "Convert the contract to PDF and upload it again.",
        )


__all__ = ["FALLBACK_NAME", "Upload", "require_pdf", "sanitize_filename", "save_upload",
           "stored_path"]
