"""
Phase 3 tests: POST /api/files/

Each test gets an isolated upload directory and an isolated SQLite
database (both under pytest's tmp_path), so nothing here touches the
real ./uploads folder or ./aereo.db.
"""

import zipfile
from pathlib import Path

from app.config import settings
from app.models import UploadedFile

# The `env` fixture, `post_file`, and the ZIP builder live in
# tests/conftest.py so every phase's tests share them. `build_zip()`
# defaults to a complete Shapefile bundle: Phase 5 processes .zip
# contents, so a fixture of random bytes would be rejected as an
# incomplete Shapefile.
from tests.conftest import build_zip, env, post_file  # noqa: F401

# A minimal but well-formed KML document.
KML_CONTENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark><name>Test Parcel</name></Placemark>
  </Document>
</kml>
"""


# --------------------------------------------------------------------------- #
# 1 & 2. Successful uploads
# --------------------------------------------------------------------------- #
def test_successful_kml_upload(env):
    client, _, _ = env
    response = post_file(client, "parcels.kml", KML_CONTENT)

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "parcels.kml"
    assert body["file_type"] == "kml"
    assert body["file_size_bytes"] == len(KML_CONTENT)
    assert body["id"] > 0
    assert body["upload_crs"] is None
    assert body["uploaded_at"] is not None


def test_successful_zip_upload(env):
    client, _, _ = env
    payload = build_zip()
    response = post_file(client, "bundle.zip", payload)

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "bundle.zip"
    assert body["file_type"] == "zip"
    assert body["file_size_bytes"] == len(payload)


# --------------------------------------------------------------------------- #
# 3. Unsupported extension
# --------------------------------------------------------------------------- #
def test_unsupported_extension_rejected(env):
    client, _, upload_dir = env
    response = post_file(client, "notes.txt", b"hello world")

    assert response.status_code == 400
    assert ".txt" in response.json()["detail"]
    # Nothing written, nothing recorded.
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_missing_extension_rejected(env):
    client, _, _ = env
    response = post_file(client, "README", b"no extension")
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 4. Oversized file
# --------------------------------------------------------------------------- #
def test_oversized_file_rejected(env):
    client, _, upload_dir = env
    # One byte over the configured limit.
    limit_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    response = post_file(client, "huge.kml", b"a" * (limit_bytes + 1))

    assert response.status_code == 400
    assert "too large" in response.json()["detail"]

    # The partially written file must have been cleaned up.
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_file_at_exact_limit_accepted(env):
    client, _, _ = env
    limit_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    # This test is about the size boundary, so the payload must now also
    # be valid KML (Phase 4 parses .kml content). Pad the well-formed
    # fixture with trailing whitespace — legal XML after the root element.
    assert len(KML_CONTENT) < limit_bytes
    payload = KML_CONTENT + b" " * (limit_bytes - len(KML_CONTENT))
    response = post_file(client, "exactly.kml", payload)
    assert response.status_code == 201


# --------------------------------------------------------------------------- #
# 5. Invalid ZIP
# --------------------------------------------------------------------------- #
def test_invalid_zip_rejected(env):
    client, _, upload_dir = env
    response = post_file(client, "broken.zip", b"this is definitely not a zip file")

    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_corrupt_zip_rejected(env):
    """A real ZIP with its bytes truncated must still be rejected."""
    client, _, _ = env
    payload = build_zip()
    truncated = payload[: len(payload) // 2]
    response = post_file(client, "truncated.zip", truncated)

    assert response.status_code == 400


def test_zip_content_not_extracted(env):
    """Phase 3 stores the archive; it must not be unpacked."""
    client, _, upload_dir = env
    response = post_file(client, "bundle.zip", build_zip())
    assert response.status_code == 201

    stored = list(upload_dir.iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".zip"          # still an archive ...
    assert zipfile.is_zipfile(stored[0])       # ... and still a valid one


# --------------------------------------------------------------------------- #
# 6 & 7. Database record
# --------------------------------------------------------------------------- #
def test_database_record_created(env):
    client, TestingSession, _ = env
    response = post_file(client, "parcels.kml", KML_CONTENT)
    record_id = response.json()["id"]

    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)

    assert record is not None
    assert record.filename == "parcels.kml"
    assert record.file_type == "kml"
    assert record.file_path is not None
    assert record.upload_crs is None      # CRS comes later
    assert record.uploaded_at is not None
    assert record.file_size_bytes == len(KML_CONTENT)


def test_file_size_stored_correctly(env):
    client, TestingSession, _ = env
    payload = build_zip()
    record_id = post_file(client, "sizes.zip", payload).json()["id"]

    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)

    assert record.file_size_bytes == len(payload)


# --------------------------------------------------------------------------- #
# 8. The file really lands in the upload directory
# --------------------------------------------------------------------------- #
def test_uploaded_file_exists_on_disk(env):
    client, _, upload_dir = env
    response = post_file(client, "parcels.kml", KML_CONTENT)
    assert response.status_code == 201

    files = list(upload_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == KML_CONTENT
    assert files[0].parent == upload_dir


def test_file_path_in_db_matches_disk(env):
    client, TestingSession, _ = env
    record_id = post_file(client, "parcels.kml", KML_CONTENT).json()["id"]

    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)

    on_disk = Path(record.file_path)
    assert on_disk.is_file()
    assert on_disk.parent.name == "uploads"


def test_internal_path_not_exposed_in_response(env):
    client, _, _ = env
    body = post_file(client, "parcels.kml", KML_CONTENT).json()
    assert "file_path" not in body


# --------------------------------------------------------------------------- #
# 9. Unsafe filenames / path traversal
# --------------------------------------------------------------------------- #
def test_filename_is_sanitized_in_database(env):
    client, TestingSession, _ = env
    record_id = post_file(client, "../../../etc/passwd.kml", KML_CONTENT).json()["id"]

    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)

    # Traversal components stripped, original name otherwise preserved.
    assert record.filename == "passwd.kml"
    assert ".." not in record.filename


def test_windows_style_traversal_is_sanitized(env):
    client, TestingSession, _ = env
    record_id = post_file(client, "..\\..\\secrets.kml", KML_CONTENT).json()["id"]

    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)
    assert record.filename == "secrets.kml"


def test_traversal_does_not_escape_upload_dir(env, tmp_path):
    """The stored file must always be directly inside UPLOAD_DIR."""
    client, _, upload_dir = env
    post_file(client, "../../escaped.kml", KML_CONTENT)

    stored = list(upload_dir.iterdir())
    assert len(stored) == 1
    # Stored name is generated, not derived from the client's string.
    assert stored[0].parent == upload_dir
    assert ".." not in stored[0].name
    # And nothing appeared in the parent of the upload dir.
    assert not (upload_dir.parent / "escaped.kml").exists()
    assert not (upload_dir.parent / "passwd.kml").exists()


def test_path_only_filename_rejected(env):
    client, _, _ = env
    response = post_file(client, "../..", b"data")
    assert response.status_code == 400
