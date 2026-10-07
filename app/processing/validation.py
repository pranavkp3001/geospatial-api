"""
Validation helpers for uploaded files.

These are deliberately *pure* functions: they take plain values (a
filename, a byte count, a path on disk) and raise `UploadValidationError`
if a rule is violated. They know nothing about FastAPI, HTTP, or the
database, which keeps them trivially testable.

Rules enforced here:
    1. Only allowed file extensions are accepted.
    2. Files must not exceed `settings.MAX_FILE_SIZE_MB`.
    3. A `.zip` upload must really be a readable ZIP archive.
    4. Client-supplied filenames are never trusted as filesystem paths.
"""

import zipfile
import zlib
from pathlib import Path

from app.config import settings

# Extensions this API accepts today. Their *contents* are interpreted
# per type: .kml is parsed directly, .zip must hold a Shapefile (see
# the service layer) — this module only checks the extension itself.
ALLOWED_EXTENSIONS = {".kml", ".zip"}


class UploadValidationError(ValueError):
    """Raised when an upload breaks a validation rule.

    The API layer catches this and turns it into HTTP 400.
    """


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied filename to a safe, display-only name.

    The client controls this string completely, so it may contain path
    separators, parent-directory references (`../../`), or characters
    that are illegal on some filesystems. We keep only the final path
    component and strip anything unsafe.

    Note this value is for *display and database storage only* — the file
    itself is always written under a generated name (see the service).
    """
    # Treat both separators the same so "..\\..\\evil.kml" is caught too.
    name = filename.replace("\\", "/").split("/")[-1]

    # Drop control characters and characters illegal on common filesystems.
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')

    # Leading/trailing dots hide files ("." / "..") or produce odd names.
    name = name.strip().strip(".")
    name = name[:255]

    if not name:
        raise UploadValidationError("Filename is missing or invalid.")
    return name


def validate_extension(filename: str) -> str:
    """Return the lower-cased extension, or raise if it is not allowed."""
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        shown = extension or "(none)"
        raise UploadValidationError(
            f"Unsupported file type '{shown}'. Allowed extensions: {allowed}."
        )
    return extension


def max_upload_bytes() -> int:
    """Maximum upload size in bytes, derived from settings.

    The limit is configured in megabytes (via .env) but compared in
    bytes, which is what we actually count while streaming.
    """
    return settings.MAX_FILE_SIZE_MB * 1024 * 1024


def validate_file_size(size_bytes: int) -> None:
    """Raise if `size_bytes` exceeds the configured maximum."""
    limit = max_upload_bytes()
    if size_bytes > limit:
        raise UploadValidationError(
            f"File is too large: {size_bytes} bytes exceeds the "
            f"{limit} byte limit ({settings.MAX_FILE_SIZE_MB} MB)."
        )


def validate_zip(path: Path) -> None:
    """Raise if `path` is not a valid, uncorrupted ZIP archive.

    Only the archive structure is checked here. Extracting it safely
    and validating what is inside (the Shapefile bundle) happens
    afterwards in the service layer — see zip_extract.py and
    shapefile_parser.py.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            # testzip() walks every member and returns the name of the
            # first corrupt one, or None if the whole archive is sound.
            corrupt_member = archive.testzip()
    except zipfile.BadZipFile as exc:
        raise UploadValidationError(
            "File is not a valid ZIP archive."
        ) from exc
    except (RuntimeError, OSError, zlib.error) as exc:
        # RuntimeError: encrypted/unreadable members.
        # zlib.error:    broken compressed data.
        raise UploadValidationError(
            f"ZIP archive could not be read: {exc}"
        ) from exc

    if corrupt_member is not None:
        raise UploadValidationError(
            f"ZIP archive is corrupt (bad entry: {corrupt_member})."
        )
