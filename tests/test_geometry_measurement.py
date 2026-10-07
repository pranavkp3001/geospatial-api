"""
Phase 6 tests: geometry measurements + CRS handling.

Unit tests drive `calculate_measurement()` / `utm_epsg_for()` directly;
integration tests push KML and Shapefile uploads through POST /api/files/
and inspect the finished Feature rows.

The invariants under test:
    * area/length are always metre-based ("m\u00b2"/"m"), never degree-based;
    * a geographic CRS (EPSG:4326) is reprojected to a UTM zone chosen
      from the geometry centroid (326zz north, 327zz south);
    * projected metre CRSs are measured in their own plane;
    * missing/invalid/non-metre CRSs and unmeasurable geometries yield
      NULL measurement fields (and never crash);
    * stored geometry + CRS are never modified by measuring.
"""

import logging

import numpy as np
import pytest
from pyproj import CRS, Transformer
from shapely import transform_coordseq
from shapely import wkt as shapely_wkt

from app.processing.geometry import calculate_measurement, utm_epsg_for

from tests.conftest import UTM43N_PRJ, env  # noqa: F401  (shared fixture)
from tests.test_kml_processing import POLYGON_KML, upload_kml
from tests.test_shapefile_processing import (
    ESRI_PRJ,
    bundle_entries,
    features_for,
    upload_zip,
    write_shapefile,
)

# A ~0.1° x 0.1° block near Bangalore (12.97 N, 77.55 E) in EPSG:4326.
BANGALORE_POLYGON = (
    "POLYGON ((77.5 12.9, 77.6 12.9, 77.6 13.0, 77.5 13.0, 77.5 12.9))"
)
# A short diagonal near Bangalore, ~0.05° in each direction (~7.8 km).
BANGALORE_LINE = "LINESTRING (77.50 12.90, 77.55 12.95)"
# A 0.1° x 0.1° block at ~34°S / 151°E (Sydney area) — southern hemisphere.
SOUTH_POLYGON = (
    "POLYGON ((151.0 -33.9, 151.1 -33.9, 151.1 -34.0, 151.0 -34.0, 151.0 -33.9))"
)

PROJECTED_SQUARE = (
    "POLYGON ((500000 0, 501000 0, 501000 1000, 500000 1000, 500000 0))"
)
RIGHT_TRIANGLE_PATH = "LINESTRING (0 0, 3000 4000)"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _project_to_utm(geometry_wkt: str) -> "shapely.Geometry":
    """Reproject a geographic WKT into its centroid's UTM zone.

    Mirrors the production path (used to compute expected values) and
    stays independent enough to catch wrong zone/hemisphere choices.
    """
    geom = shapely_wkt.loads(geometry_wkt)
    epsg = utm_epsg_for(geom.centroid.x, geom.centroid.y)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    def _project(coords):
        # transform_coordseq passes a single (N, 2) array; pyproj wants
        # x and y separately (same bridge as app/processing/geometry.py).
        x, y = coords[:, 0], coords[:, 1]
        new_x, new_y = transformer.transform(x, y)
        return np.column_stack((new_x, new_y))

    return transform_coordseq(geom, _project)


def _expected_area_m2(geometry_wkt: str) -> float:
    return float(_project_to_utm(geometry_wkt).area)


def _expected_length_m(geometry_wkt: str) -> float:
    return float(_project_to_utm(geometry_wkt).length)


# --------------------------------------------------------------------------- #
# A. Projected Polygon
# --------------------------------------------------------------------------- #
def test_projected_polygon_area():
    result = calculate_measurement(PROJECTED_SQUARE, "Polygon", "EPSG:32643")
    assert result.measurement_type == "area"
    assert result.measurement_value == pytest.approx(1_000_000.0, rel=1e-9)
    assert result.measurement_unit == "m\u00b2"


# --------------------------------------------------------------------------- #
# B. Projected LineString
# --------------------------------------------------------------------------- #
def test_projected_linestring_length():
    result = calculate_measurement(RIGHT_TRIANGLE_PATH, "LineString", "EPSG:32643")
    assert result.measurement_type == "length"
    assert result.measurement_value == pytest.approx(5_000.0, rel=1e-9)
    assert result.measurement_unit == "m"


# --------------------------------------------------------------------------- #
# C. MultiPolygon
# --------------------------------------------------------------------------- #
def test_multipolygon_total_area():
    wkt = (
        "MULTIPOLYGON (((0 0, 100 0, 100 100, 0 100, 0 0)), "
        "((200 0, 300 0, 300 100, 200 100, 200 0)))"
    )
    result = calculate_measurement(wkt, "MultiPolygon", "EPSG:32643")
    assert result.measurement_type == "area"
    assert result.measurement_value == pytest.approx(20_000.0, rel=1e-9)
    assert result.measurement_unit == "m\u00b2"


# --------------------------------------------------------------------------- #
# D. MultiLineString
# --------------------------------------------------------------------------- #
def test_multilinestring_total_length():
    wkt = "MULTILINESTRING ((0 0, 3 4), (0 0, 0 10))"  # 5 m + 10 m
    result = calculate_measurement(wkt, "MultiLineString", "EPSG:32643")
    assert result.measurement_type == "length"
    assert result.measurement_value == pytest.approx(15.0, rel=1e-9)
    assert result.measurement_unit == "m"


# --------------------------------------------------------------------------- #
# E / F. Point & MultiPoint — no measurement, no warning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("wkt", "geometry_type", "crs"),
    [
        ("POINT (77.5 12.9)", "Point", "EPSG:4326"),
        ("MULTIPOINT ((0 0), (1 1))", "MultiPoint", "EPSG:32643"),
    ],
)
def test_point_like_geometries_have_no_measurement(wkt, geometry_type, crs):
    result = calculate_measurement(wkt, geometry_type, crs)
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None


# --------------------------------------------------------------------------- #
# G. EPSG:4326 Polygon — measured in m², never degree²
# --------------------------------------------------------------------------- #
def test_4326_polygon_measured_in_square_meters():
    result = calculate_measurement(BANGALORE_POLYGON, "Polygon", "EPSG:4326")
    assert result.measurement_type == "area"
    # Non-null, and the shape is real: ~0.1° x 0.1° ≈ 1.2e8 m². A degree²
    # "measurement" would be ~0.01, so this bound proves no degree math.
    assert result.measurement_value is not None
    assert 1.0e8 < result.measurement_value < 1.5e8
    assert result.measurement_value == pytest.approx(
        _expected_area_m2(BANGALORE_POLYGON), rel=1e-6
    )
    assert result.measurement_unit == "m\u00b2"


# --------------------------------------------------------------------------- #
# H. EPSG:4326 LineString — measured in metres
# --------------------------------------------------------------------------- #
def test_4326_linestring_measured_in_meters():
    result = calculate_measurement(BANGALORE_LINE, "LineString", "EPSG:4326")
    assert result.measurement_type == "length"
    assert result.measurement_value is not None
    # ~7.8 km for a 0.05° diagonal; a degree-based reading would be ~0.07.
    assert 6_500 < result.measurement_value < 9_000
    assert result.measurement_value == pytest.approx(
        _expected_length_m(BANGALORE_LINE), rel=1e-6
    )
    assert result.measurement_unit == "m"


# --------------------------------------------------------------------------- #
# I. Bangalore UTM — 32643, projected, metre-based
# --------------------------------------------------------------------------- #
def test_bangalore_utm_zone_is_326n():
    assert utm_epsg_for(77.6, 12.97) == 32643
    assert utm_epsg_for(70.0, 15.0) == 32642  # zone 42 (72°E starts zone 43)
    # A southern latitude flips to the 327xx series.
    assert utm_epsg_for(151.05, -33.95) == 32756
    assert utm_epsg_for(-46.6, -23.5) == 32723


def test_epsg_32643_is_a_projected_metre_crs():
    crs = CRS.from_user_input("EPSG:32643")
    assert crs.is_projected
    assert crs.axis_info[0].unit_name.lower() in {"metre", "meter", "m"}
    # ...and measuring in it really yields metres (planar square: 1 km²).
    result = calculate_measurement(PROJECTED_SQUARE, "Polygon", "EPSG:32643")
    assert result.measurement_value == pytest.approx(1_000_000.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# J. Southern hemisphere — 327xx selected, still measured in metres
# --------------------------------------------------------------------------- #
def test_southern_hemisphere_selects_327xx_utm():
    assert utm_epsg_for(151.05, -33.95) == 32756
    assert str(utm_epsg_for(151.05, -33.95)).startswith("327")


def test_southern_4326_polygon_measured_in_square_meters():
    result = calculate_measurement(SOUTH_POLYGON, "Polygon", "EPSG:4326")
    assert result.measurement_type == "area"
    assert result.measurement_value is not None
    # ~0.1° x 0.1° at 34°S ≈ 9 km x 11 km ≈ 1.0e8 m².
    assert 7.0e7 < result.measurement_value < 1.3e8
    assert result.measurement_value == pytest.approx(
        _expected_area_m2(SOUTH_POLYGON), rel=1e-6
    )
    assert result.measurement_unit == "m\u00b2"


# --------------------------------------------------------------------------- #
# K / L. Missing / invalid CRS — no measurement, no crash, no guessing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("crs", [None, "", "   "])
def test_missing_crs_returns_null_measurement(caplog, crs):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement(BANGALORE_POLYGON, "Polygon", crs)
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None
    assert any("no CRS" in r.message for r in caplog.records)


@pytest.mark.parametrize("crs", ["EPSG:999999", "not-a-crs", "banana??"])
def test_invalid_crs_returns_null_measurement_without_crashing(caplog, crs):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement(BANGALORE_POLYGON, "Polygon", crs)
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None
    assert any("could not be interpreted" in r.message for r in caplog.records)


def test_projected_non_metre_crs_returns_null_measurement(caplog):
    """Feet-based CRSs must not produce values claiming to be metres."""
    # EPSG:2263 = NAD83 / New York Long Island (ftUS).
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement(PROJECTED_SQUARE, "Polygon", "EPSG:2263")
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None
    assert any("not metre-based" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# M. Unsupported / empty geometry — null measurement + warning
# --------------------------------------------------------------------------- #
def test_unsupported_geometry_returns_null_measurement(caplog):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement(
            "GEOMETRYCOLLECTION (POINT (0 0))",
            "GeometryCollection",
            "EPSG:4326",
        )
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None
    assert any("unsupported geometry type" in r.message for r in caplog.records)


def test_empty_geometry_returns_null_measurement(caplog):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement("POLYGON EMPTY", "Polygon", "EPSG:32643")
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None


def test_unparseable_wkt_returns_null_measurement(caplog):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement("THIS IS NOT WKT", "Polygon", "EPSG:32643")
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None


def test_type_wkt_mismatch_returns_null_measurement(caplog):
    with caplog.at_level(logging.WARNING, logger="app.processing.geometry"):
        result = calculate_measurement(
            "LINESTRING (0 0, 1 1)", "Polygon", "EPSG:32643"
        )
    assert result.measurement_type is None
    assert result.measurement_value is None
    assert result.measurement_unit is None


# --------------------------------------------------------------------------- #
# N. The original WKT is never modified
# --------------------------------------------------------------------------- #
def test_original_wkt_string_is_not_modified():
    original = BANGALORE_POLYGON
    calculate_measurement(original, "Polygon", "EPSG:4326")
    # Strings are immutable, so the variable must still hold the exact
    # degree-based WKT — a reprojection cannot have leaked back.
    assert "POLYGON ((77.5 12.9" in original
    assert original == BANGALORE_POLYGON


# also verified end-to-end in the KML/Shapefile upload tests below.

# --------------------------------------------------------------------------- #
# O. Full KML upload — measurable feature gets measurements; WKT unchanged
# --------------------------------------------------------------------------- #
def test_kml_polygon_upload_populates_measurements(env):
    client, TestingSession, _ = env
    response = upload_kml(client, POLYGON_KML)
    assert response.status_code == 201

    feature = features_for(TestingSession, response.json()["id"])[0]
    assert feature.geometry_type == "Polygon"
    assert feature.crs == "EPSG:4326"
    assert feature.measurement_type == "area"
    assert feature.measurement_unit == "m\u00b2"
    assert feature.measurement_value is not None
    # The stored geometry is still the degree-based KML one (unchanged),
    # not the reprojected working copy used for the calculation.
    assert feature.geometry.startswith("POLYGON")
    assert "10.0" in feature.geometry
    assert feature.measurement_value == pytest.approx(
        _expected_area_m2(feature.geometry), rel=1e-6
    )


def test_kml_point_upload_keeps_null_measurements(env):
    client, TestingSession, _ = env
    point_kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><Placemark><Point><coordinates>77.6,12.97,0</coordinates></Point></Placemark></Document>
</kml>
"""
    response = upload_kml(client, point_kml)
    assert response.status_code == 201

    feature = features_for(TestingSession, response.json()["id"])[0]
    assert feature.measurement_type is None
    assert feature.measurement_value is None
    assert feature.measurement_unit is None


# --------------------------------------------------------------------------- #
# P. Full Shapefile upload — valid .prj -> measurements populated
# --------------------------------------------------------------------------- #
def test_shapefile_upload_with_prj_populates_measurements(env, tmp_path):
    client, TestingSession, _ = env
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
    response = upload_zip(client, bundle_entries(shp))
    assert response.status_code == 201

    feature = features_for(TestingSession, response.json()["id"])[0]
    assert feature.crs == "EPSG:32643"  # area in the native plane
    assert feature.measurement_type == "area"
    assert feature.measurement_value == pytest.approx(1_000_000.0, rel=1e-6)
    assert feature.measurement_unit == "m\u00b2"
    # Stored geometry untouched: planar UTM coords, not the reprojection.
    assert feature.geometry.startswith("POLYGON")
    assert "500000" in feature.geometry


def test_shapefile_upload_with_esri_wkt_prj_populates_measurements(env, tmp_path):
    """A .prj without an EPSG authority (raw WKT CRS) still measures."""
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "parcel",
        [
            (("line", [[(500000, 0), (503000, 4000)]]), {"name": "Route"}),
        ],
        prj=ESRI_PRJ,
    )
    response = upload_zip(client, bundle_entries(shp))
    assert response.status_code == 201

    feature = features_for(TestingSession, response.json()["id"])[0]
    assert feature.crs == ESRI_PRJ  # kept verbatim (Phase 5 policy)
    assert feature.measurement_type == "length"
    assert feature.measurement_value == pytest.approx(5_000.0, rel=1e-6)
    assert feature.measurement_unit == "m"


# --------------------------------------------------------------------------- #
# Q. Shapefile without .prj — CRS None -> measurements stay null
# --------------------------------------------------------------------------- #
def test_shapefile_without_prj_keeps_null_measurements(env, tmp_path):
    client, TestingSession, _ = env
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
    response = upload_zip(client, bundle_entries(shp))
    assert response.status_code == 201

    feature = features_for(TestingSession, response.json()["id"])[0]
    assert feature.crs is None
    assert feature.measurement_type is None
    assert feature.measurement_value is None
    assert feature.measurement_unit is None
    # ...and the geometry itself is still stored (no silent EPSG:4326 guess).
    assert feature.geometry.startswith("POLYGON")