"""
Upload service — orchestrates the whole upload flow.

    API (thin)  ->  Service (this file)  ->  Processing (parsers/validation)
                                          ->  Data (ORM)

The route does nothing except parse the request, call `upload_file()`,
and translate `UploadValidationError` into an HTTP response. All of the
real work lives here so it can be reused (e.g. by a CLI or a test)
without going through HTTP.

Flow per extension:
    .zip  validate -> save -> UploadedFile row -> extract to a temporary
          directory -> locate the Shapefile -> parse -> Feature rows
          (the stored ZIP stays in uploads/; the extracted components
          are deleted with the temporary directory.)
    .kml  validate -> save -> UploadedFile row -> parse   -> Feature rows

Failure policy: everything from saving the file to committing the
Feature rows happens in ONE transaction. If any step fails we roll the
database back and delete the stored file, so a rejected upload leaves no
UploadedFile row, no Feature rows, and no file on disk.
"""

import tempfile
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Feature, UploadedFile
from app.processing.kml_parser import parse_kml
from app.processing.parsed_feature import ParsedFeature
from app.processing.shapefile_parser import locate_shapefile, parse_shapefile
from app.processing.validation import (
    UploadValidationError,
    sanitize_filename,
    validate_extension,
    validate_file_size,
    validate_zip,
)
from app.processing.zip_extract import safe_extract

# Read/write the upload 1 MiB at a time. Large enough to keep the number
# of syscalls low, small enough that we never hold a whole file in RAM.
CHUNK_SIZE = 1024 * 1024


async def upload_file(upload: UploadFile, db: Session) -> UploadedFile:
    """Validate, store, and record an uploaded file.

    Args:
        upload: The multipart file sent by the client.
        db:    An open SQLAlchemy session (supplied by FastAPI's
               `Depends(get_db)`).

    Returns:
        The persisted UploadedFile row. Its transient `feature_count`
        attribute holds the number of Feature rows created (KML
        placemarks, or Shapefile records for .zip uploads).

    Raises:
        UploadValidationError: on any validation, KML parsing, or
            Shapefile processing failure. Nothing is persisted in that
            case — the database is rolled back and the stored file is
            deleted.
    """
    # 1. Make the client filename safe for storage/display.
    original_filename = sanitize_filename(upload.filename or "")

    # 2. Reject unsupported types before touching the filesystem.
    extension = validate_extension(original_filename)

    # 3. Prepare the destination directory.
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 4. Write the bytes under a *generated* name — the client name is
    #    never used as a path, so traversal is impossible by construction.
    stored_name = f"{uuid.uuid4().hex}{extension}"
    destination = upload_dir / stored_name

    feature_count = 0
    try:
        size_bytes = await _save_stream(upload, destination)

        # 5. ZIP uploads must actually be readable archives.
        if extension == ".zip":
            validate_zip(destination)

        # 6. Persist the metadata. One row first, so features can point
        #    at a real primary key.
        record = UploadedFile(
            filename=original_filename,       # original name kept for the user
            file_type=extension.lstrip("."),  # ".kml" -> "kml", ".zip" -> "zip"
            file_path=str(destination),
            upload_crs=None,                  # file-level CRS comes in Phase 6
            file_size_bytes=size_bytes,
        )
        db.add(record)
        db.flush()  # assigns record.id without committing yet

        # 7. Geometry extraction. KML is parsed in place; a ZIP is
        #    unpacked into a temporary directory first (never into
        #    uploads/) and must contain exactly one Shapefile.
        if extension == ".kml":
            feature_count = _persist_features(record, parse_kml(destination), db)
        elif extension == ".zip":
            feature_count = _process_shapefile_zip(record, destination, db)

        # 8. Single commit covering the file record AND its features.
        db.commit()
    except Exception:
        # All-or-nothing: a rejected upload leaves no rows and no file.
        db.rollback()
        destination.unlink(missing_ok=True)
        raise

    db.refresh(record)
    # Not a column — just lets the response report how many features
    # were extracted without issuing a second query.
    record.feature_count = feature_count
    return record


def _process_shapefile_zip(
    record: UploadedFile,
    zip_path: Path,
    db: Session,
) -> int:
    """Extract a Shapefile ZIP, parse it, and create its Feature rows.

    The archive is unpacked into a fresh directory under the system
    temp dir — never into the permanent uploads dir — and that
    directory is removed on the way out, success or failure. Only the
    ZIP itself remains stored.

    Runs inside `upload_file()`'s single try block, so a rejection here
    rolls the database back, deletes the stored ZIP, and surfaces as a
    clean HTTP 400 (no stack traces, no orphan Feature rows).
    """
    with tempfile.TemporaryDirectory(prefix="aereo-shapefile-") as tmp:
        extraction_dir = Path(tmp)
        safe_extract(zip_path, extraction_dir)   # rejects escaping paths
        shp_path = locate_shapefile(extraction_dir)  # one complete bundle
        features = parse_shapefile(shp_path)         # WKT + attributes + CRS
    # Every file handle is closed and the temp dir is gone by here;
    # the parsed features are plain strings, safe to persist afterwards.
    return _persist_features(record, features, db)


def _persist_features(
    record: UploadedFile,
    features: list[ParsedFeature],
    db: Session,
) -> int:
    """Turn parsed features (KML or Shapefile) into Feature rows.

    They land in the caller's transaction, so a later failure rolls
    them back with everything else. Measurement columns stay NULL:
    measuring is a later phase.
    """
    for parsed in features:
        db.add(
            Feature(
                file=record,  # sets file_id from record.id
                feature_index=parsed.feature_index,
                geometry_type=parsed.geometry_type,
                geometry=parsed.geometry,  # WKT text
                crs=parsed.crs,            # KML: "EPSG:4326"; Shapefile: from .prj or None
                properties=parsed.properties,
                measurement_type=None,
                measurement_value=None,
                measurement_unit=None,
            )
        )
    return len(features)


async def _save_stream(upload: UploadFile, destination: Path) -> int:
    """Copy the upload to disk chunk by chunk, enforcing the size limit.

    The file is checked *while* it is written rather than after, so an
    oversized upload is aborted instead of being fully written and then
    deleted. Returns the total number of bytes written.
    """
    total = 0
    with open(destination, "wb") as out:
        while chunk := await upload.read(CHUNK_SIZE):
            total += len(chunk)
            validate_file_size(total)  # raises once the limit is passed
            out.write(chunk)
    return total
