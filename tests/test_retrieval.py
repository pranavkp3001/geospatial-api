"""
Phase 7 tests: retrieval endpoints.

    GET /api/files/{id}/              -> file metadata
    GET /api/files/{id}/measurements/ -> stored measurement rows

Both GET endpoints are read-only: responses come from the database, so
no KML/Shapefile file on disk is parsed and no measurement is
recalculated. The upload helpers (KML/Shapefile fixtures) are only used
to *create* the rows that the GET endpoints then serve.
"""

import pytest

from tests.conftest import UTM43N_PRJ, env  # noqa: F401  (shared fixture)
from tests.test_kml_processing import (
    MULTI_KML,
    POINT_KML,
    POLYGON_KML,
    upload_kml,
)
from tests.test_shapefile_processing import (
    bundle_entries,
    upload_zip,
    write_shapefile,
)

MISSING_FILE_ID = 999999


# --------------------------------------------------------------------------- #
# A. GET existing file — metadata, and no internal details leak
# --------------------------------------------------------------------------- #
def test_get_existing_file_returns_metadata(env):
    client, _, _ = env
    upload = upload_kml(client, POINT_KML).json()

    response = client.get(f"/api/files/{upload['id']}/")
    assert response.status_code == 200

    body = response.json()
    assert body["id"] == upload["id"]
    assert body["filename"] == "sample.kml"
    assert body["file_type"] == "kml"
    assert body["file_size_bytes"] > 0
    assert body["feature_count"] == 1
    assert body["upload_crs"] is None
    assert "uploaded_at" in body and body["uploaded_at"]
    # Internal storage details must not be exposed.
    assert "file_path" not in body
    assert list(body) == [
        "id",
        "filename",
        "file_type",
        "upload_crs",
        "uploaded_at",
        "file_size_bytes",
        "feature_count",
    ]


def test_get_existing_zip_file_reports_zip_type(env, tmp_path):
    client, _, _ = env
    shp = write_shapefile(tmp_path, "parcel", [])
    upload = upload_zip(client, bundle_entries(shp)).json()

    body = client.get(f"/api/files/{upload['id']}/").json()
    assert body["file_type"] == "zip"
    assert body["feature_count"] == 0
    assert "file_path" not in body


# --------------------------------------------------------------------------- #
# B. GET non-existent file -> 404 (for both endpoints)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("suffix", ["/", "/measurements/"])
def test_get_missing_file_returns_404(env, suffix):
    client, _, _ = env
    response = client.get(f"/api/files/{MISSING_FILE_ID}{suffix}")
    assert response.status_code == 404


def test_get_measurements_for_missing_file_returns_404(env):
    client, _, _ = env
    response = client.get(f"/api/files/{MISSING_FILE_ID}/measurements/")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# C. Measurements for a measurable KML Polygon
# --------------------------------------------------------------------------- #
def test_measurements_for_kml_polygon(env):
    client, _, _ = env
    upload = upload_kml(client, POLYGON_KML).json()

    response = client.get(f"/api/files/{upload['id']}/measurements/")
    assert response.status_code == 200

    body = response.json()
    assert body["file_id"] == upload["id"]
    assert len(body["features"]) == 1

    feature = body["features"][0]
    assert feature["feature_index"] == 0
    assert feature["geometry_type"] == "Polygon"
    assert feature["measurement_type"] == "area"
    assert feature["measurement_unit"] == "m\u00b2"
    # ~10°x10° square minus a 2°x2° hole ≈ 1.18e12 m² — meter-scale
    # value proves the stored measurement (not degree² ≈ 10²) is served.
    assert feature["measurement_value"] is not None
    assert 1.0e12 < feature["measurement_value"] < 1.5e12


# --------------------------------------------------------------------------- #
# D. Point measurement fields are stored/returned as NULL
# --------------------------------------------------------------------------- #
def test_measurements_for_kml_point_are_null(env):
    client, _, _ = env
    upload = upload_kml(client, POINT_KML).json()

    feature = client.get(
        f"/api/files/{upload['id']}/measurements/"
    ).json()["features"][0]
    assert feature["geometry_type"] == "Point"
    assert feature["measurement_type"] is None
    assert feature["measurement_value"] is None
    assert feature["measurement_unit"] is None


# --------------------------------------------------------------------------- #
# E. Shapefile measurements — stored values from a valid .prj upload
# --------------------------------------------------------------------------- #
def test_measurements_for_shapefile_with_prj(env, tmp_path):
    client, _, _ = env
    shp = write_shapefile(
        tmp_path,
        "parcel",
        [
            (
                ("polygon", [[(500000, 0), (500000, 1000), (501000, 1000), (501000, 0), (500000, 0)]]),
                {"name": "Parcel 1"},
            )
        ],
        prj=UTM43N_PRJ,
    )
    upload = upload_zip(client, bundle_entries(shp)).json()

    feature = client.get(
        f"/api/files/{upload['id']}/measurements/"
    ).json()["features"][0]
    assert feature["geometry_type"] == "Polygon"
    assert feature["measurement_type"] == "area"
    assert feature["measurement_value"] == pytest.approx(1_000_000.0, rel=1e-6)
    assert feature["measurement_unit"] == "m\u00b2"


# --------------------------------------------------------------------------- #
# F. Shapefile without CRS — measurements served back as NULL
# --------------------------------------------------------------------------- #
def test_measurements_for_shapefile_without_prj_are_null(env, tmp_path):
    client, _, _ = env
    shp = write_shapefile(
        tmp_path,
        "parcel",
        [
            (
                ("polygon", [[(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]]),
                {"name": "Parcel"},
            )
        ],
        prj=None,
    )
    upload = upload_zip(client, bundle_entries(shp)).json()

    feature = client.get(
        f"/api/files/{upload['id']}/measurements/"
    ).json()["features"][0]
    assert feature["geometry_type"] == "Polygon"
    assert feature["measurement_type"] is None
    assert feature["measurement_value"] is None
    assert feature["measurement_unit"] is None


# --------------------------------------------------------------------------- #
# G. Multiple features — all returned, ordered by feature_index
# --------------------------------------------------------------------------- #
def test_multiple_features_returned_in_feature_index_order(env):
    client, _, _ = env
    upload = upload_kml(client, MULTI_KML).json()  # Point, LineString, Polygon
    assert upload["feature_count"] == 3

    body = client.get(f"/api/files/{upload['id']}/measurements/").json()
    assert body["file_id"] == upload["id"]
    assert len(body["features"]) == 3
    assert [f["feature_index"] for f in body["features"]] == [0, 1, 2]
    assert [f["geometry_type"] for f in body["features"]] == [
        "Point",
        "LineString",
        "Polygon",
    ]
    # The measured Polygon keeps its area; the Point stays NULL.
    assert body["features"][2]["measurement_type"] == "area"
    assert body["features"][2]["measurement_value"] is not None
    assert body["features"][0]["measurement_value"] is None


# --------------------------------------------------------------------------- #
# H. Zero features -> empty feature list (valid empty Shapefile upload)
# --------------------------------------------------------------------------- #
def test_empty_feature_set_returns_empty_list(env, tmp_path):
    client, _, _ = env
    # An empty but valid Shapefile bundle is accepted by existing rules
    # (Phase 5) and produces a file record with zero Feature rows.
    shp = write_shapefile(tmp_path, "empty", [])
    upload = upload_zip(client, bundle_entries(shp)).json()
    assert upload["feature_count"] == 0

    body = client.get(f"/api/files/{upload['id']}/measurements/").json()
    assert body == {"file_id": upload["id"], "features": []}


# --------------------------------------------------------------------------- #
# I. Route separation — the two GET paths resolve independently
# --------------------------------------------------------------------------- #
def test_file_and_measurements_routes_resolve_independently(env):
    client, _, _ = env
    upload = upload_kml(client, POLYGON_KML).json()
    file_id = upload["id"]

    detail = client.get(f"/api/files/{file_id}/")
    measurements = client.get(f"/api/files/{file_id}/measurements/")

    assert detail.status_code == 200
    assert measurements.status_code == 200

    # The detail route serves the FILE, not the measurements list.
    assert detail.json()["id"] == file_id
    assert "features" not in detail.json()
    assert "file_id" not in detail.json()

    # The measurements route serves the FILE'S measurements, not metadata.
    assert measurements.json()["file_id"] == file_id
    assert "filename" not in measurements.json()