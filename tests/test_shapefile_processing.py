"""
Phase 5 tests: Shapefile processing for .zip uploads.

Each test generates a tiny Shapefile with pyshp on the fly (no binary
fixtures are committed to the repository), zips it in memory, and
uploads it through POST /api/files/. Isolated upload dir + database
come from the shared `env` fixture.

Policies pinned by these tests (see app/processing/shapefile_parser.py):
  * exactly one complete .shp bundle (.shp + .shx + .dbf) per ZIP,
    otherwise HTTP 400 — never a random pick;
  * .prj decides the CRS; a missing .prj means NULL, never EPSG:4326;
  * extraction happens in a temporary directory that is always removed
    (success and failure alike) and never inside uploads/;
  * a malicious member path rejects the whole archive before a single
    byte is written.
"""

import tempfile
from pathlib import Path

import shapefile  # pyshp

from app.models import Feature, UploadedFile
from tests.conftest import (  # noqa: F401  (env is a fixture)
    UTM43N_PRJ,
    build_zip,
    default_shapefile_entries,
    env,
    post_file,
)

# An ESRI-style .prj: WKT, but with no AUTHORITY node to unpack — the
# parser must keep the WKT text itself instead of guessing an EPSG.
ESRI_PRJ = (
    'PROJCS["WGS_1984_UTM_Zone_43N",GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",500000],PARAMETER["False_Northing",0],'
    'PARAMETER["Central_Meridian",75],PARAMETER["Scale_Factor",0.9996],'
    'PARAMETER["Latitude_Of_Origin",0],UNIT["Meter",1]]'
)


# --------------------------------------------------------------------------- #
# Shapefile generation helpers
# --------------------------------------------------------------------------- #
def write_shapefile(
    directory: Path,
    stem: str,
    features: list,
    *,
    prj: str | None = None,
    field_types: dict[str, str] | None = None,
) -> Path:
    """Write a complete Shapefile bundle into `directory`.

    Args:
        stem: base name, e.g. "roads" -> roads.shp/.shx/.dbf (+ .prj).
        features: list of (geometry_spec, attributes) pairs, where
            geometry_spec is one of
                ("point", (x, y))
                ("multipoint", [(x, y), ...])
                ("polygon", [ring, ...])      # outer CW, holes CCW
                ("line", [part, ...])         # 1 part -> LineString
                ("null", None)                # empty shape
            and attributes is {field_name: value}.
            All non-null shapes in one bundle must share a single type —
            that is a Shapefile format rule, not a limitation of ours.
        prj: .prj text to write; None means no .prj at all.
        field_types: DBF type overrides, e.g. {"when": "D"} — anything
            not listed is inferred from the sample value
            (str -> C, int -> N, float -> N, bool -> L).

    Returns the path of the .shp file.
    """
    field_types = field_types or {}

    # One field per attribute key, in first-seen order across records.
    keys: list[str] = []
    for _, attrs in features:
        for key in attrs:
            if key not in keys:
                keys.append(key)

    writer = shapefile.Writer(str(directory / stem))
    for key in keys:
        if key in field_types:
            writer.field(key, field_types[key])
        else:
            sample = next(attrs[key] for _, attrs in features if key in attrs)
            if isinstance(sample, bool):
                writer.field(key, "L")
            elif isinstance(sample, int):
                writer.field(key, "N", size=18, decimal=0)
            elif isinstance(sample, float):
                writer.field(key, "N", size=18, decimal=6)
            else:
                writer.field(key, "C", size=80)
    if not keys:
        writer.field("Id", "N")  # a DBF needs at least one field

    for geometry, attrs in features:
        _write_geometry(writer, geometry)
        writer.record(**attrs)

    writer.close()

    if prj is not None:
        (directory / f"{stem}.prj").write_text(prj, encoding="utf-8")
    return directory / f"{stem}.shp"


def _write_geometry(writer: "shapefile.Writer", geometry) -> None:
    kind, payload = geometry
    if kind == "point":
        writer.point(*payload)
    elif kind == "multipoint":
        writer.multipoint(payload)
    elif kind == "polygon":
        writer.poly(payload)
    elif kind == "line":
        writer.line(payload)
    elif kind == "null":
        writer.null()
    else:  # guards future typos in this file, not user input
        raise ValueError(f"unknown geometry kind {kind!r}")


def bundle_entries(shp: Path, prefix: str = "") -> dict[str, bytes]:
    """Bytes of every component of `shp`'s bundle (all files sharing
    its stem), optionally placed under a folder prefix inside the ZIP."""
    return {
        prefix + path.name: path.read_bytes()
        for path in shp.parent.iterdir()
        if path.is_file() and path.stem.lower() == shp.stem.lower()
    }


def without(entries: dict[str, bytes], suffix: str) -> dict[str, bytes]:
    """Copy of `entries` minus files ending in `suffix` (case-insensitive)."""
    return {k: v for k, v in entries.items() if not k.lower().endswith(suffix)}


def features_for(TestingSession, record_id: int) -> list[Feature]:
    with TestingSession() as db:
        return (
            db.query(Feature)
            .filter(Feature.file_id == record_id)
            .order_by(Feature.feature_index)
            .all()
        )


def upload_zip(client, entries: dict[str, bytes], name: str = "bundle.zip"):
    return post_file(client, name, build_zip(entries))


def assert_nothing_persisted(TestingSession, upload_dir: Path) -> None:
    """A rejected upload must leave no rows and no stored file."""
    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 0
        assert db.query(Feature).count() == 0
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def temp_processing_dirs() -> set[Path]:
    """The temp dirs our extractor creates (see _process_shapefile_zip)."""
    return set(Path(tempfile.gettempdir()).glob("aereo-shapefile-*"))


# --------------------------------------------------------------------------- #
# 1-4. Successful uploads and geometry conversion
# --------------------------------------------------------------------------- #
def test_point_shapefile_upload_succeeds(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path, "points", [(("point", (-95.8, 40.9)), {"name": "Depot"})]
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    assert body["file_type"] == "zip"
    assert body["feature_count"] == 1

    feature = features_for(TestingSession, body["id"])[0]
    assert feature.geometry_type == "Point"
    assert feature.geometry == "POINT (-95.8 40.9)"
    assert feature.properties == {"name": "Depot"}
    assert feature.crs is None  # no .prj in this bundle


def test_polygon_shapefile_stores_wkt_with_hole(env, tmp_path):
    client, TestingSession, _ = env
    outer = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]   # clockwise
    hole = [(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)]         # counter-clockwise
    shp = write_shapefile(
        tmp_path, "blocks", [(("polygon", [outer, hole]), {"name": "block"})]
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    feature = features_for(TestingSession, body["id"])[0]

    assert feature.geometry_type == "Polygon"
    assert feature.geometry == (
        "POLYGON ((0.0 0.0, 0.0 10.0, 10.0 10.0, 10.0 0.0, 0.0 0.0), "
        "(2.0 2.0, 4.0 2.0, 4.0 4.0, 2.0 4.0, 2.0 2.0))"
    )


def test_polyline_shapefile_stores_linestring_variants(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "roads",
        [
            (("line", [[(-95.8, 40.9), (-96.0, 41.0), (-96.1, 41.1)]]),
             {"name": "simple"}),
            (("line", [[(-97.0, 42.0), (-97.1, 42.1)],
                       [(-98.0, 43.0), (-98.1, 43.1)]]),
             {"name": "split"}),
        ],
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    assert body["feature_count"] == 2
    features = features_for(TestingSession, body["id"])

    assert [f.geometry_type for f in features] == ["LineString", "MultiLineString"]
    assert features[0].geometry == "LINESTRING (-95.8 40.9, -96.0 41.0, -96.1 41.1)"
    assert features[1].geometry == (
        "MULTILINESTRING ((-97.0 42.0, -97.1 42.1), (-98.0 43.0, -98.1 43.1))"
    )


def test_multipoint_and_multipolygon_geometry_types(env, tmp_path):
    client, TestingSession, _ = env
    ring1 = [(0, 0), (0, 5), (5, 5), (5, 0), (0, 0)]
    ring2 = [(20, 20), (20, 25), (25, 25), (25, 20), (20, 20)]

    # A Shapefile file may hold only ONE geometry type (format rule),
    # so the two kinds go in two separate bundles and uploads.
    clusters = write_shapefile(
        tmp_path, "clusters",
        [(("multipoint", [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]), {"kind": "mpt"})],
    )
    areas = write_shapefile(
        tmp_path, "areas",
        [(("polygon", [ring1, ring2]), {"kind": "mpoly"})],  # two disjoint outers
    )

    mpt = features_for(TestingSession, upload_zip(client, bundle_entries(clusters)).json()["id"])[0]
    mpoly = features_for(TestingSession, upload_zip(client, bundle_entries(areas)).json()["id"])[0]

    assert mpt.geometry_type == "MultiPoint"
    assert mpt.geometry == "MULTIPOINT ((1.0 2.0), (3.0 4.0), (5.0 6.0))"
    assert mpoly.geometry_type == "MultiPolygon"
    assert mpoly.geometry == (
        "MULTIPOLYGON (((0.0 0.0, 0.0 5.0, 5.0 5.0, 5.0 0.0, 0.0 0.0)), "
        "((20.0 20.0, 20.0 25.0, 25.0 25.0, 25.0 20.0, 20.0 20.0)))"
    )


def test_multiple_features_are_sequential_and_linked(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "parcels",
        [
            (("point", (1.0, 2.0)), {"name": "A"}),
            (("point", (3.0, 4.0)), {"name": "B"}),
            (("point", (5.0, 6.0)), {"name": "C"}),
        ],
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    assert body["feature_count"] == 3

    features = features_for(TestingSession, body["id"])
    assert [f.feature_index for f in features] == [0, 1, 2]
    assert {f.file_id for f in features} == {body["id"]}


def test_null_shape_is_skipped_not_fatal(env, tmp_path):
    """A record with no geometry is skipped; the rest are kept."""
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "holes",
        [
            (("null", None), {"name": "empty"}),
            (("point", (7.0, 8.0)), {"name": "real"}),
        ],
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    assert body["feature_count"] == 1
    feature = features_for(TestingSession, body["id"])[0]
    assert feature.geometry_type == "Point"


# --------------------------------------------------------------------------- #
# 5. DBF properties
# --------------------------------------------------------------------------- #
def test_dbf_attributes_are_preserved(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "assets",
        [
            (("point", (10.0, 20.0)), {
                "name": "Depot",
                "count": 42,
                "score": 3.5,
                "when": "20240115",
                "flag": True,
            }),
        ],
        field_types={"when": "D"},  # DBF date field, not text
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    feature = features_for(TestingSession, body["id"])[0]

    # datetime.date arrives as an ISO string — JSON cannot hold dates.
    assert feature.properties == {
        "name": "Depot",
        "count": 42,
        "score": 3.5,
        "when": "2024-01-15",
        "flag": True,
    }


# --------------------------------------------------------------------------- #
# 6-7. CRS from .prj
# --------------------------------------------------------------------------- #
def test_prj_with_authority_stores_epsg_code(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path, "utm", [(("point", (500000.0, 4500000.0)), {"name": "p"})],
        prj=UTM43N_PRJ,
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    feature = features_for(TestingSession, body["id"])[0]
    assert feature.crs == "EPSG:32643"


def test_esri_prj_without_authority_stores_wkt_string(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path, "esri", [(("point", (500000.0, 4500000.0)), {"name": "p"})],
        prj=ESRI_PRJ,
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    feature = features_for(TestingSession, body["id"])[0]
    assert feature.crs == ESRI_PRJ  # kept verbatim, not guessed away


def test_missing_prj_stores_null_crs(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(tmp_path, "noprj", [(("point", (1.0, 2.0)), {"name": "p"})])

    body = upload_zip(client, bundle_entries(shp)).json()
    feature = features_for(TestingSession, body["id"])[0]
    assert feature.crs is None
    assert feature.crs != "EPSG:4326"  # never assume a CRS silently


# --------------------------------------------------------------------------- #
# 8-11. Discovery policy rejections
# --------------------------------------------------------------------------- #
def test_missing_dbf_rejected(env, tmp_path):
    client, TestingSession, upload_dir = env
    shp = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"name": "p"})])

    response = upload_zip(client, without(bundle_entries(shp), ".dbf"))

    assert response.status_code == 400
    assert ".dbf" in response.json()["detail"]
    assert_nothing_persisted(TestingSession, upload_dir)


def test_missing_shx_rejected(env, tmp_path):
    client, TestingSession, upload_dir = env
    shp = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"name": "p"})])

    response = upload_zip(client, without(bundle_entries(shp), ".shx"))

    assert response.status_code == 400
    assert ".shx" in response.json()["detail"]
    assert_nothing_persisted(TestingSession, upload_dir)


def test_zip_without_shapefile_rejected(env):
    client, TestingSession, upload_dir = env

    response = upload_zip(client, {"readme.txt": b"no geometry here"})

    assert response.status_code == 400
    assert "Shapefile" in response.json()["detail"]
    assert_nothing_persisted(TestingSession, upload_dir)


def test_multiple_shapefiles_rejected(env, tmp_path):
    client, TestingSession, upload_dir = env
    roads = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"n": "r"})])
    water = write_shapefile(tmp_path, "water", [(("point", (3.0, 4.0)), {"n": "w"})])

    response = upload_zip(
        client, {**bundle_entries(roads), **bundle_entries(water)}
    )

    assert response.status_code == 400
    assert "multiple Shapefiles" in response.json()["detail"]
    assert_nothing_persisted(TestingSession, upload_dir)


def test_shapefile_in_subfolder_is_found(env, tmp_path):
    """Discovery is recursive: bundles may sit inside ZIP folders."""
    client, TestingSession, _ = env
    shp = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"name": "p"})])

    body = upload_zip(client, bundle_entries(shp, prefix="data/gis/")).json()
    assert body["feature_count"] == 1


def test_empty_shapefile_creates_record_without_features(env, tmp_path):
    """A valid but empty bundle succeeds with zero features — same as
    an empty KML file in Phase 4."""
    client, TestingSession, _ = env
    shp = write_shapefile(tmp_path, "empty", [])

    body = upload_zip(client, bundle_entries(shp)).json()
    assert body["feature_count"] == 0
    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 1
        assert db.query(Feature).count() == 0


# --------------------------------------------------------------------------- #
# 12-13. Extraction security and cleanup
# --------------------------------------------------------------------------- #
def test_malicious_relative_path_rejected(env, tmp_path):
    client, TestingSession, upload_dir = env
    entries = default_shapefile_entries()
    entries["../evil.txt"] = b"pwned"  # would land in the system temp dir

    response = upload_zip(client, entries)

    assert response.status_code == 400
    assert "escapes" in response.json()["detail"]
    # Nothing was written: names are all validated before extraction.
    assert not (Path(tempfile.gettempdir()) / "evil.txt").exists()
    assert_nothing_persisted(TestingSession, upload_dir)


def test_malicious_absolute_path_rejected(env, tmp_path):
    client, TestingSession, upload_dir = env
    entries = default_shapefile_entries()
    entries["C:\\Windows\\evil.dll"] = b"pwned"

    response = upload_zip(client, entries)

    assert response.status_code == 400
    assert "unsafe" in response.json()["detail"]
    assert_nothing_persisted(TestingSession, upload_dir)


def test_temp_extraction_dir_removed_after_success(env, tmp_path):
    client, _, _ = env
    shp = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"n": "p"})])
    before = temp_processing_dirs()

    response = upload_zip(client, bundle_entries(shp))

    assert response.status_code == 201
    assert temp_processing_dirs() == before


def test_temp_extraction_dir_removed_after_failure(env, tmp_path):
    client, TestingSession, upload_dir = env
    shp = write_shapefile(tmp_path, "roads", [(("point", (1.0, 2.0)), {"n": "p"})])
    entries = without(bundle_entries(shp), ".dbf")
    before = temp_processing_dirs()

    response = upload_zip(client, entries)

    assert response.status_code == 400
    assert temp_processing_dirs() == before
    assert_nothing_persisted(TestingSession, upload_dir)


# --------------------------------------------------------------------------- #
# 14. Measurements stay untouched (Phase 6 fills them in)
# --------------------------------------------------------------------------- #
def test_measurements_are_null_for_shapefile_features(env, tmp_path):
    client, TestingSession, _ = env
    shp = write_shapefile(
        tmp_path,
        "roads",
        [
            # One geometry type per file (format rule) -> two lines.
            (("line", [[(1.0, 2.0), (3.0, 4.0)]]), {"n": "a"}),
            (("line", [[(5.0, 6.0), (7.0, 8.0)]]), {"n": "b"}),
        ],
    )

    body = upload_zip(client, bundle_entries(shp)).json()
    for feature in features_for(TestingSession, body["id"]):
        assert feature.measurement_type is None
        assert feature.measurement_value is None
        assert feature.measurement_unit is None
