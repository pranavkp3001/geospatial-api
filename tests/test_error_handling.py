"""
Phase 8 tests: error handling and edge cases.

The behaviour pinned by this suite:

    EXPECTED CLIENT/FILE ERROR   -> HTTP 400, safe human-readable detail
    MISSING RESOURCE             -> HTTP 404
    UNEXPECTED INTERNAL FAILURE  -> HTTP 500, generic detail, no traceback

and the safety rules:

    * A failed upload never leaves an UploadedFile row, any Feature rows,
      or a stored file behind (single transaction + delete-on-failure).
    * A successful upload keeps its stored file.
    * The temporary extraction directory is always removed — success or
      failure — and malicious ZIP members are rejected before a single
      byte is written.
    * Error responses never contain stack traces, absolute filesystem
      paths, temp directory names, or drive letters.
    * Unsupported geometry and missing/invalid CRS degrade to NULLs: the
      upload succeeds, the stored WKT/CRS stay untouched, and
      measurements are never fabricated.
"""

import re

import pytest

from app.config import settings
from app.models import Feature, UploadedFile
from app.processing.geometry import calculate_measurement as _real_measure
from app.processing.validation import UploadValidationError
from app.processing.zip_extract import safe_extract
from app.services import upload as upload_service

from tests.conftest import (  # noqa: F401  (env is a fixture)
    build_zip,
    default_shapefile_entries,
    env,
    post_file,
)
from tests.test_kml_processing import (
    INVALID_KML,
    MULTI_KML,
    POINT_KML,
    upload_kml,
)
from tests.test_shapefile_processing import (
    assert_nothing_persisted,
    bundle_entries,
    features_for,
    temp_processing_dirs,
    upload_zip,
    without,
    write_shapefile,
)

# The one and only 500 body the API is allowed to send.
SAFE_500_DETAIL = {"detail": "Internal server error while processing the file."}

# A <MultiGeometry> whose children mix Point and LineString parses to an
# unsupported GeometryCollection — the KML parser must skip it, not crash
# and not invent a measurement.
UNSUPPORTED_ONLY_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Mixed bag</name><MultiGeometry>
    <Point><coordinates>1,2,0</coordinates></Point>
    <LineString><coordinates>0,0,0 1,1,0</coordinates></LineString>
  </MultiGeometry></Placemark>
</Document></kml>
"""


# --------------------------------------------------------------------------- #
# A/B/C — extension, size, ZIP validity
# --------------------------------------------------------------------------- #
def test_unsupported_extension_is_400(env):
    client, _, _ = env
    response = post_file(client, "notes.txt", b"plain text")

    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_oversized_upload_is_400(env, monkeypatch):
    client, TestingSession, upload_dir = env
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 1)  # 1 MiB for this test
    response = post_file(client, "huge.kml", b"a" * (1024 * 1024 + 1))

    assert response.status_code == 400
    assert "too large" in response.json()["detail"]
    # The partially written file must be gone, and no rows persisted.
    assert_nothing_persisted(TestingSession, upload_dir)


def test_invalid_zip_is_400(env):
    client, _, _ = env
    response = post_file(client, "broken.zip", b"definitely not a zip file")

    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# D–G — Shapefile discovery policy
# --------------------------------------------------------------------------- #
def test_zip_with_no_shapefile_is_400(env):
    client, _, _ = env
    response = upload_zip(client, {"readme.txt": b"hello"})

    assert response.status_code == 400
    assert "Shapefile" in response.json()["detail"]


@pytest.mark.parametrize("missing", [".shx", ".dbf"])
def test_zip_missing_shapefile_sidecar_is_400(env, missing):
    client, _, _ = env
    entries = without(default_shapefile_entries(), missing)

    response = upload_zip(client, entries)
    assert response.status_code == 400
    assert missing in response.json()["detail"]


def test_zip_with_multiple_shapefiles_is_400(env, tmp_path):
    client, _, _ = env
    alpha = write_shapefile(tmp_path, "alpha", [(("point", (-95.8, 40.9)), {"n": 1})])
    beta = write_shapefile(tmp_path, "beta", [(("point", (-95.7, 40.8)), {"n": 2})])
    entries = {**bundle_entries(alpha), **bundle_entries(beta)}

    response = upload_zip(client, entries)
    assert response.status_code == 400
    assert "multiple Shapefiles" in response.json()["detail"]


def test_corrupt_shapefile_data_is_400(env):
    """Spec 4F: corrupt data inside a Shapefile bundle -> controlled 400."""
    client, _, _ = env
    entries = default_shapefile_entries()
    # A valid, complete bundle whose attribute table is garbage. pyshp
    # fails to read it, the parser turns that into a clean 400, and the
    # uploaded ZIP is removed again.
    entries["roads.dbf"] = b"not a dbf at all!!" + b"\x00"

    response = upload_zip(client, entries)
    assert response.status_code == 400
    assert "Shapefile" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# H — ZIP path-traversal security regression
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "malicious",
    [
        "../evil.txt",
        "../../evil.txt",
        "/absolute.txt",
        "C:/absolute.txt",
        "Z:\\absolute.txt",
    ],
    ids=["parent", "grandparent", "root-absolute", "drive-c", "drive-z-backslash"],
)
def test_malicious_zip_members_reject_the_whole_archive(env, malicious):
    client, TestingSession, upload_dir = env
    before = temp_processing_dirs()
    entries = {"ok/readme.txt": b"fine", malicious: b"evil"}

    response = upload_zip(client, entries)
    assert response.status_code == 400
    # Nothing may land anywhere: no rows, no stored ZIP, no extracted
    # files, no leftover temporary extraction directory.
    assert_nothing_persisted(TestingSession, upload_dir)
    assert temp_processing_dirs() == before


def test_safe_extract_rejects_before_writing_any_bytes(tmp_path):
    """Unit-level guarantee behind the upload tests: when a member name
    is unsafe the whole archive is rejected up front — not even the
    benign members of the same ZIP are written."""
    archive = tmp_path / "evil.zip"
    archive.write_bytes(build_zip({"ok.txt": b"fine", "../escape.txt": b"evil"}))
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(UploadValidationError):
        safe_extract(archive, out)

    assert list(out.iterdir()) == []


# --------------------------------------------------------------------------- #
# I/J/K — KML edge cases
# --------------------------------------------------------------------------- #
def test_malformed_kml_is_400(env):
    client, TestingSession, upload_dir = env
    response = upload_kml(client, INVALID_KML)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "KML" in detail or "XML" in detail
    assert_nothing_persisted(TestingSession, upload_dir)


def test_xml_that_is_not_kml_is_400(env):
    client, _, _ = env
    response = upload_kml(client, "<html><body>not kml</body></html>", name="fake.kml")
    assert response.status_code == 400


def test_unsupported_geometry_is_skipped_controlled(env):
    client, TestingSession, _ = env
    response = upload_kml(client, UNSUPPORTED_ONLY_KML)

    # Controlled behaviour: skipped (not crashed), no fake measurement.
    assert response.status_code == 201
    body = response.json()
    assert body["feature_count"] == 0

    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 1
        assert db.query(Feature).count() == 0
    assert client.get(f"/api/files/{body['id']}/measurements/").json()["features"] == []


# --------------------------------------------------------------------------- #
# L/M — CRS degradation (no invented CRS, no fabricated measurement)
# --------------------------------------------------------------------------- #
def test_missing_crs_uploads_but_measurement_stays_null(env, tmp_path):
    client, _, _ = env
    outer = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
    # Polygon deliberately (not a Point) — it makes the null measurement
    # prove the "no CRS -> no measurement" rule, not the "Point" rule.
    shp = write_shapefile(tmp_path, "lots", [(("polygon", [outer]), {"name": "lot"})])
    body = upload_zip(client, bundle_entries(shp)).json()

    assert body["feature_count"] == 1
    item = client.get(f"/api/files/{body['id']}/measurements/").json()["features"][0]
    assert item["measurement_type"] is None
    assert item["measurement_value"] is None
    assert item["measurement_unit"] is None


def test_malformed_prj_leaves_crs_and_measurement_null(env, tmp_path):
    """Spec 4G: an unreadable .prj means NULL CRS — never a guess."""
    client, TestingSession, _ = env
    outer = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
    shp = write_shapefile(
        tmp_path,
        "lots",
        [(("polygon", [outer]), {"name": "lot"})],
        prj="THIS IS NOT VALID WKT FOR A CRS",
    )
    body = upload_zip(client, bundle_entries(shp)).json()

    assert body["feature_count"] == 1
    feature = features_for(TestingSession, body["id"])[0]
    assert feature.crs is None
    assert feature.measurement_type is None
    assert feature.measurement_value is None
    assert feature.measurement_unit is None


# --------------------------------------------------------------------------- #
# N/O — retrieval 404s and path-parameter behaviour
# --------------------------------------------------------------------------- #
def test_missing_file_detail_is_404(env):
    client, _, _ = env
    response = client.get("/api/files/999999/")
    assert response.status_code == 404
    assert "cannot be found" in response.json()["detail"].lower() or "not found" in response.json()["detail"].lower()


def test_missing_file_measurements_is_404(env):
    client, _, _ = env
    assert client.get("/api/files/999999/measurements/").status_code == 404


@pytest.mark.parametrize("file_id", [-1, 0, 10**15])
def test_retrieval_edge_integer_ids_are_404(env, file_id):
    client, _, _ = env
    assert client.get(f"/api/files/{file_id}/").status_code == 404
    assert client.get(f"/api/files/{file_id}/measurements/").status_code == 404
    body = client.get(f"/api/files/{file_id}/").json()
    # A plain "not found" — never a database error.
    assert "Traceback" not in str(body)
    assert "Internal server error" not in str(body)


def test_retrieval_non_integer_id_is_422(env):
    client, _, _ = env
    assert client.get("/api/files/not-an-integer/").status_code == 422


# --------------------------------------------------------------------------- #
# P/Q — rollback and cleanup when processing fails
# --------------------------------------------------------------------------- #
def test_internal_failure_after_row_creation_rolls_back_and_cleans(env, monkeypatch):
    """The UploadedFile row exists before parsing starts — an internal
    failure there must still remove the row, its Feature rows, AND the
    stored file (spec sections 7, 8, 13)."""
    client, TestingSession, upload_dir = env

    def boom(*args, **kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(upload_service, "parse_kml", boom)
    response = upload_kml(client, POINT_KML)

    assert response.status_code == 500
    assert response.json() == SAFE_500_DETAIL
    assert_nothing_persisted(TestingSession, upload_dir)


def test_failure_halfway_through_features_rolls_back_all_rows(env, monkeypatch):
    """An error while measuring feature 2 of 3 leaves no stray Feature
    rows committed (spec section 7)."""
    client, TestingSession, upload_dir = env
    calls = {"n": 0}

    def flaky(geometry_wkt, geometry_type, crs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom measuring the second feature")
        return _real_measure(geometry_wkt, geometry_type, crs)

    monkeypatch.setattr(upload_service, "calculate_measurement", flaky)
    response = upload_kml(client, MULTI_KML)  # 3 features; the 2nd one fails

    assert response.status_code == 500
    assert response.json() == SAFE_500_DETAIL
    assert_nothing_persisted(TestingSession, upload_dir)


def test_validation_failures_delete_the_stored_file(env):
    client, TestingSession, upload_dir = env
    attempts = [
        ("note.txt", b"x"),                 # rejected before anything is written
        ("broken.zip", b"not a zip"),       # written, then found invalid
        ("bad.kml", INVALID_KML),           # written, then unparseable
    ]
    for name, payload in attempts:
        response = post_file(client, name, payload)
        assert response.status_code == 400, name
        assert_nothing_persisted(TestingSession, upload_dir)


# --------------------------------------------------------------------------- #
# R/S — successful uploads keep their file; temp dirs always cleaned
# --------------------------------------------------------------------------- #
def test_successful_uploads_keep_the_stored_file(env, tmp_path):
    client, _, upload_dir = env
    upload_kml(client, POINT_KML)
    shp = write_shapefile(tmp_path, "points", [(("point", (-95.8, 40.9)), {"name": "Depot"})])
    upload_zip(client, bundle_entries(shp))

    stored = sorted(p.suffix for p in upload_dir.iterdir())
    assert stored == [".kml", ".zip"]


def test_temp_extraction_dirs_are_always_cleaned(env, tmp_path):
    client, _, _ = env
    before = temp_processing_dirs()

    shp = write_shapefile(tmp_path, "points", [(("point", (-95.8, 40.9)), {"name": "Depot"})])
    assert upload_zip(client, bundle_entries(shp)).status_code == 201     # success path
    assert upload_zip(client, {"../evil.txt": b"x"}).status_code == 400   # failure path

    assert temp_processing_dirs() == before


# --------------------------------------------------------------------------- #
# T/U — error bodies stay safe and generic
# --------------------------------------------------------------------------- #
def test_error_details_never_expose_filesystem_paths(env):
    client, _, upload_dir = env
    failing_uploads = [
        ("note.txt", b"hello"),
        ("broken.zip", b"not a zip"),
        ("bad.kml", b"this is not XML at all <<<"),
        ("fake.kml", b"<html><body>not kml</body></html>"),
        ("noshp.zip", build_zip({"readme.txt": b"hi"})),
    ]

    for name, payload in failing_uploads:
        response = post_file(client, name, payload)
        assert response.status_code == 400, name
        detail = response.json()["detail"]
        assert str(upload_dir) not in detail, f"{name}: {detail!r}"
        assert "\\" not in detail, f"{name}: {detail!r}"
        # A drive letter ("C:") must never appear; the separator after
        # ordinary words like "extensions:" is fine.
        assert not re.search(r"\b[A-Za-z]:", detail), f"{name}: {detail!r}"


def test_internal_failure_body_has_no_traceback_text(env, monkeypatch):
    client, _, _ = env

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(upload_service, "parse_kml", boom)
    response = upload_kml(client, POINT_KML)

    assert response.status_code == 500
    body = response.json()
    assert body == SAFE_500_DETAIL
    assert "secret internal detail" not in str(body)
    assert "Traceback" not in str(body)
    assert "RuntimeError" not in str(body)
    assert 'File "' not in str(body)


# --------------------------------------------------------------------------- #
# V/W — successful flows unchanged
# --------------------------------------------------------------------------- #
def test_successful_kml_upload_still_works(env):
    client, _, _ = env
    response = upload_kml(client, POINT_KML)
    assert response.status_code == 201
    assert response.json()["feature_count"] == 1


def test_successful_shapefile_upload_still_works(env, tmp_path):
    client, _, _ = env
    shp = write_shapefile(tmp_path, "points", [(("point", (-95.8, 40.9)), {"name": "Depot"})])
    response = upload_zip(client, bundle_entries(shp))
    assert response.status_code == 201
    assert response.json()["feature_count"] == 1


# --------------------------------------------------------------------------- #
# Security review — client filenames are data, never filesystem paths
# --------------------------------------------------------------------------- #
def test_traversal_filename_is_sanitized_not_used_as_path(env):
    client, TestingSession, _ = env
    response = post_file(client, "../../../evil.kml", POINT_KML.encode())

    assert response.status_code == 201
    record_id = response.json()["id"]
    assert response.json()["filename"] == "evil.kml"
    with TestingSession() as db:
        assert db.get(UploadedFile, record_id).filename == "evil.kml"


def test_sql_style_filename_is_treated_as_data(env):
    client, TestingSession, _ = env
    sql_name = "x' OR '1'='1' --.kml"
    response = post_file(client, sql_name, POINT_KML.encode())

    assert response.status_code == 201
    with TestingSession() as db:
        assert db.get(UploadedFile, response.json()["id"]).filename == sql_name