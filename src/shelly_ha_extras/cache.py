from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

from .models import IntegrityError, UpdateInfo


def _zip_signature(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


def validate_zip(
    path: Path, *, expected_size: int | None = None, expected_sha256: str | None = None
) -> str:
    if expected_size is not None and path.stat().st_size != expected_size:
        raise IntegrityError("downloaded firmware size does not match metadata")
    if not _zip_signature(path):
        raise IntegrityError("firmware does not have a ZIP signature")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected_sha256 and actual.lower() != expected_sha256.lower():
        raise IntegrityError("firmware SHA-256 does not match metadata")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise IntegrityError("firmware ZIP contains a corrupt member")
    except zipfile.BadZipFile as exc:
        raise IntegrityError("firmware is not a valid ZIP archive") from exc
    return actual


def cache_path(directory: Path, info: UpdateInfo) -> Path:
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in f"{info.app}-{info.version}")
    return directory / f"{safe}.zip"


def cache_firmware(directory: Path, info: UpdateInfo, response: object) -> Path:
    """Write a streamed response atomically and validate all available metadata."""
    directory.mkdir(parents=True, exist_ok=True)
    target = cache_path(directory, info)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=directory)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            total = 0
            for chunk in response.iter_bytes():  # type: ignore[attr-defined]
                total += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        content_length = response.headers.get("content-length")  # type: ignore[attr-defined]
        expected_size = info.size
        if content_length:
            try:
                expected_size = int(content_length)
            except ValueError as exc:
                raise IntegrityError("firmware content length is not an integer") from exc
        if expected_size is not None and total != expected_size:
            raise IntegrityError("firmware content length does not match downloaded bytes")
        validate_zip(temp_path, expected_size=info.size, expected_sha256=info.sha256)
        os.replace(temp_path, target)
        return target
    finally:
        temp_path.unlink(missing_ok=True)
