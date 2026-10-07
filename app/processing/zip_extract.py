"""
Safe extraction of untrusted ZIP archives.

An uploaded ZIP is attacker-controlled input, so `extractall()` is off
the table: a member named "../../evil.txt", "/etc/passwd" or
"C:\\\\Windows\\\\evil.dll" would land outside the extraction directory.

`safe_extract()` guarantees that every byte written ends up inside one
dedicated temporary directory:

    1. Member names are normalized ("/" and "\\" treated the same).
    2. Absolute paths, drive letters and any ".." component are
       rejected outright.
    3. As defense in depth, every resolved target must still be inside
       the destination directory (catches anything step 2 missed).
    4. ALL names are validated before the FIRST byte is written, so a
       malicious archive is rejected without extracting half of it.

Nothing here knows about HTTP or SQLAlchemy: failures raise
`UploadValidationError`, which the API layer turns into HTTP 400.

Deliberately NOT done here:
    * No `extractall()` — members are copied one by one.
    * Members are always written as regular files, so a ZIP symlink
      entry cannot become a symlink on disk.
    * The destination is a temporary directory supplied by the caller,
      never the permanent uploads directory.
"""

import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from app.processing.validation import UploadValidationError

# Windows drive-letter prefixes: "C:", "d:/...", "Z:\\...".
_DRIVE = re.compile(r"^[A-Za-z]:")


def safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract every member of `zip_path` into `dest_dir`, safely.

    Raises:
        UploadValidationError: the archive contains a member whose path
            escapes `dest_dir` (or is otherwise unsafe). In that case
            nothing is written — all member names are checked first.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = dest_dir.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        # Validate every name first, extract second: a malicious entry
        # anywhere in the archive rejects the whole upload with zero
        # bytes written.
        planned = [(member, _safe_member_path(base, member)) for member in archive.infolist()]

        for member, target in planned:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Read/write in chunks: a shapefile bundle should not be
            # held in memory as one blob.
            with archive.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _safe_member_path(base: Path, member: zipfile.ZipInfo) -> Path:
    """Return the on-disk path for `member`, or raise if it is unsafe.

    `base` is the already-resolved extraction directory; the returned
    path is guaranteed to resolve inside it.
    """
    name = member.filename
    if not name.strip():
        raise UploadValidationError("ZIP contains an entry with an empty name.")

    # ZIP stores names with "/", but malicious archives use "\\" too;
    # on Windows both are path separators, so normalize before judging.
    normalized = name.replace("\\", "/")
    posix = PurePosixPath(normalized)

    if posix.is_absolute() or _DRIVE.match(normalized):
        raise UploadValidationError(f"ZIP contains an unsafe absolute path: {name!r}.")
    if any(part == ".." for part in posix.parts):
        raise UploadValidationError(f"ZIP contains a path that escapes the archive: {name!r}.")

    # `posix.parts` never contains "." (PurePosixPath collapses it).
    target = base.joinpath(*posix.parts)

    # Defense in depth: even with the checks above, only write a file
    # whose final, fully resolved location is still inside `base`.
    if not target.resolve().is_relative_to(base):
        raise UploadValidationError(f"ZIP contains a path that escapes the archive: {name!r}.")
    return target
