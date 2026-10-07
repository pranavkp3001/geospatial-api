"""
Phase 4 tests: KML parsing and Feature persistence.

Every test uploads a small inline KML fixture through POST /api/files/
and inspects the resulting Feature rows. Temporary directories and an
isolated database come from the shared `env` fixture in conftest.py.
"""

from app.models import Feature, UploadedFile

from tests.conftest import env, post_file  # noqa: F401  (shared fixture)


# --------------------------------------------------------------------------- #
# Small inline KML fixtures — no external files needed.
# --------------------------------------------------------------------------- #
POINT_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Sample Point</name>
      <description>Just a point</description>
      <Point><coordinates>-95.8,40.9,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

MULTI_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>First</name>
      <Point><coordinates>-95.8,40.9,0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>Second</name>
      <LineString><coordinates>-95.8,40.9,0 -96.0,41.0,0</coordinates></LineString>
    </Placemark>
    <Placemark>
      <name>Third</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing><coordinates>0,0,0 4,0,0 4,4,0 0,4,0 0,0,0</coordinates></LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

POLYGON_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Block with hole</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing><coordinates>0,0,0 10,0,0 10,10,0 0,10,0 0,0,0</coordinates></LinearRing>
        </outerBoundaryIs>
        <innerBoundaryIs>
          <LinearRing><coordinates>2,2,0 4,2,0 4,4,0 2,4,0 2,2,0</coordinates></LinearRing>
        </innerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""

LINESTRING_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Route</name>
      <LineString>
        <coordinates>-95.8,40.9,0 -96.0,41.0,0 -96.1,41.1,0</coordinates>
      </LineString>
    </Placemark>
  </Document>
</kml>
"""

PROPERTIES_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Schema id="parcels" name="parcels">
      <SimpleField name="zone" type="string"/>
      <SimpleField name="value" type="float"/>
    </Schema>
    <Placemark>
      <name>Rich Placemark</name>
      <description>Has metadata</description>
      <ExtendedData>
        <Data name="owner"><value>City</value></Data>
        <SchemaData schemaUrl="#parcels">
          <SimpleData name="zone">residential</SimpleData>
          <SimpleData name="value">1234.5</SimpleData>
        </SchemaData>
      </ExtendedData>
      <Point><coordinates>-95.8,40.9,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

MULTIPOLYGON_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Two blocks</name>
      <MultiGeometry>
        <Polygon><outerBoundaryIs>
          <LinearRing><coordinates>0,0,0 1,0,0 1,1,0 0,1,0 0,0,0</coordinates></LinearRing>
        </outerBoundaryIs></Polygon>
        <Polygon><outerBoundaryIs>
          <LinearRing><coordinates>5,5,0 6,5,0 6,6,0 5,6,0 5,5,0</coordinates></LinearRing>
        </outerBoundaryIs></Polygon>
      </MultiGeometry>
    </Placemark>
  </Document>
</kml>
"""

# Mixed <MultiGeometry> becomes a GeometryCollection, which we do not store.
UNSUPPORTED_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Usable</name>
      <Point><coordinates>-95.8,40.9,0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>Mixed bag</name>
      <MultiGeometry>
        <Point><coordinates>1,2,0</coordinates></Point>
        <LineString><coordinates>0,0,0 1,1,0</coordinates></LineString>
      </MultiGeometry>
    </Placemark>
    <Placemark>
      <name>Model only</name>
      <Model><Location><coordinates>1,2,0</coordinates></Location></Model>
    </Placemark>
  </Document>
</kml>
"""

EMPTY_KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"></kml>
"""

INVALID_KML = b"this is not XML at all <<<"


def upload_kml(client, content: str | bytes, name: str = "sample.kml"):
    """Upload KML text and return (response, features list from DB)."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    response = client.post(
        "/api/files/",
        files={"file": (name, data, "application/vnd.google-earth.kml+xml")},
    )
    return response


def features_for(TestingSession, record_id):
    with TestingSession() as db:
        return (
            db.query(Feature)
            .filter_by(file_id=record_id)
            .order_by(Feature.feature_index)
            .all()
        )


# --------------------------------------------------------------------------- #
# 1. Single Point
# --------------------------------------------------------------------------- #
def test_point_kml_creates_one_feature(env):
    client, TestingSession, _ = env
    response = upload_kml(client, POINT_KML)

    assert response.status_code == 201
    body = response.json()
    assert body["file_type"] == "kml"
    assert body["feature_count"] == 1

    features = features_for(TestingSession, body["id"])
    assert len(features) == 1

    feature = features[0]
    assert feature.feature_index == 0
    assert feature.geometry_type == "Point"
    # WKT text round-trips: type keyword + coordinates present.
    assert feature.geometry.startswith("POINT")
    assert "-95.8" in feature.geometry and "40.9" in feature.geometry
    assert feature.crs == "EPSG:4326"
    assert feature.file_id == body["id"]


def test_point_feature_links_to_uploaded_file(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, POINT_KML).json()["id"]

    # Relationship access (feature.file / record.features) lazy-loads,
    # so the assertions must happen while the session is still open.
    with TestingSession() as db:
        record = db.get(UploadedFile, record_id)
        feature = db.query(Feature).filter_by(file_id=record_id).one()

        assert feature.file is record
        assert record.features == [feature]


def test_measurements_are_null_for_kml_features(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, POINT_KML).json()["id"]

    feature = features_for(TestingSession, record_id)[0]
    assert feature.measurement_type is None
    assert feature.measurement_value is None
    assert feature.measurement_unit is None


# --------------------------------------------------------------------------- #
# 2. Multiple features / sequential index
# --------------------------------------------------------------------------- #
def test_multiple_features_have_sequential_indices(env):
    client, TestingSession, _ = env
    response = upload_kml(client, MULTI_KML)

    assert response.status_code == 201
    assert response.json()["feature_count"] == 3

    features = features_for(TestingSession, response.json()["id"])
    assert [f.feature_index for f in features] == [0, 1, 2]
    assert [f.geometry_type for f in features] == ["Point", "LineString", "Polygon"]


def test_all_features_share_the_same_file_id(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, MULTI_KML).json()["id"]

    features = features_for(TestingSession, record_id)
    assert {f.file_id for f in features} == {record_id}
    assert len({f.id for f in features}) == 3  # distinct rows


# --------------------------------------------------------------------------- #
# 3. Polygon (with an interior ring)
# --------------------------------------------------------------------------- #
def test_polygon_kml_stores_wkt_with_hole(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, POLYGON_KML).json()["id"]

    feature = features_for(TestingSession, record_id)[0]
    assert feature.geometry_type == "Polygon"
    assert feature.geometry.startswith("POLYGON")
    # Two rings: outer boundary + inner hole.
    assert feature.geometry.count("(") >= 2
    assert "10.0" in feature.geometry


# --------------------------------------------------------------------------- #
# 4. LineString
# --------------------------------------------------------------------------- #
def test_linestring_kml_stores_wkt(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, LINESTRING_KML).json()["id"]

    feature = features_for(TestingSession, record_id)[0]
    assert feature.geometry_type == "LineString"
    assert feature.geometry.startswith("LINESTRING")
    assert "-96.1" in feature.geometry


# --------------------------------------------------------------------------- #
# 5. Properties
# --------------------------------------------------------------------------- #
def test_name_and_description_are_stored(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, POINT_KML).json()["id"]

    props = features_for(TestingSession, record_id)[0].properties
    assert props["name"] == "Sample Point"
    assert props["description"] == "Just a point"


def test_extended_data_is_stored(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, PROPERTIES_KML).json()["id"]

    props = features_for(TestingSession, record_id)[0].properties
    assert props["name"] == "Rich Placemark"
    assert props["description"] == "Has metadata"
    # <Data> entries
    assert props["owner"] == "City"
    # <SchemaData>/<SimpleData> entries
    assert props["zone"] == "residential"
    assert props["value"] == "1234.5"


# --------------------------------------------------------------------------- #
# 6. Empty KML
# --------------------------------------------------------------------------- #
def test_empty_kml_creates_record_without_features(env):
    client, TestingSession, _ = env
    response = upload_kml(client, EMPTY_KML)

    assert response.status_code == 201
    body = response.json()
    assert body["feature_count"] == 0

    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 1
        assert db.query(Feature).count() == 0


def test_document_with_no_placemarks_creates_no_features(env):
    client, TestingSession, _ = env
    kml = (
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document><name>Empty doc</name></Document></kml>"
    )
    response = upload_kml(client, kml)

    assert response.status_code == 201
    assert response.json()["feature_count"] == 0
    assert features_for(TestingSession, response.json()["id"]) == []


# --------------------------------------------------------------------------- #
# 7. Invalid KML — must not crash, must not leave rows behind
# --------------------------------------------------------------------------- #
def test_invalid_kml_rejected_with_400(env):
    client, TestingSession, upload_dir = env
    response = upload_kml(client, INVALID_KML)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "KML" in detail or "XML" in detail
    # No stack trace leaked to the client.
    assert "Traceback" not in detail
    assert "File \"" not in detail


def test_invalid_kml_leaves_no_database_rows(env):
    client, TestingSession, upload_dir = env
    upload_kml(client, INVALID_KML)

    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 0
        assert db.query(Feature).count() == 0
    # ...and no orphaned file either.
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


def test_non_kml_xml_rejected(env):
    """Valid XML that is not a KML document is a 400, not '0 features'."""
    client, _, _ = env
    response = upload_kml(client, "<html><body>not kml</body></html>", name="fake.kml")
    assert response.status_code == 400


def test_empty_file_rejected(env):
    client, _, _ = env
    response = upload_kml(client, b"")
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# 8. Unsupported geometry — skipped gracefully, upload still succeeds
# --------------------------------------------------------------------------- #
def test_unsupported_geometry_is_skipped_not_fatal(env, caplog):
    client, TestingSession, _ = env
    response = upload_kml(client, UNSUPPORTED_KML)

    # The usable Point survives; the GeometryCollection and the Model do not.
    assert response.status_code == 201
    assert response.json()["feature_count"] == 1

    features = features_for(TestingSession, response.json()["id"])
    assert len(features) == 1
    assert features[0].geometry_type == "Point"

    # A clear reason was logged for each skipped placemark.
    messages = [r.getMessage() for r in caplog.records]
    assert any("Mixed bag" in m and "unsupported geometry" in m for m in messages)
    assert any("Model only" in m and "no storable geometry" in m for m in messages)


def test_skipped_geometry_does_not_leave_gaps_in_index(env):
    client, TestingSession, _ = env
    # Unsupported placemark sits in the middle of the document.
    kml = """<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>A</name><Point><coordinates>1,2,0</coordinates></Point></Placemark>
  <Placemark><name>Bad</name><MultiGeometry>
    <Point><coordinates>1,2,0</coordinates></Point>
    <LineString><coordinates>0,0,0 1,1,0</coordinates></LineString>
  </MultiGeometry></Placemark>
  <Placemark><name>B</name><Point><coordinates>3,4,0</coordinates></Point></Placemark>
</Document></kml>"""
    response = upload_kml(client, kml)
    assert response.json()["feature_count"] == 2

    features = features_for(TestingSession, response.json()["id"])
    assert [f.feature_index for f in features] == [0, 1]


# --------------------------------------------------------------------------- #
# Bonus: multi-geometries come out of the library naturally
# --------------------------------------------------------------------------- #
def test_multipolygon_geometry_type(env):
    client, TestingSession, _ = env
    record_id = upload_kml(client, MULTIPOLYGON_KML).json()["id"]

    feature = features_for(TestingSession, record_id)[0]
    assert feature.geometry_type == "MultiPolygon"
    assert feature.geometry.startswith("MULTIPOLYGON")


# --------------------------------------------------------------------------- #
# ZIP uploads take the Shapefile path — never the KML parser
# --------------------------------------------------------------------------- #
def test_zip_upload_is_not_kml_processed(env):
    """KML *inside* a ZIP is not parsed as KML.

    Phase 5 processes .zip archives as Shapefiles only, so a ZIP
    containing just KML contains no Shapefile and is rejected with 400.
    """
    import io
    import zipfile

    client, TestingSession, _ = env
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("parcels.kml", POINT_KML)

    response = client.post(
        "/api/files/",
        files={"file": ("bundle.zip", buffer.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "Shapefile" in response.json()["detail"]
    with TestingSession() as db:
        assert db.query(UploadedFile).count() == 0
        assert db.query(Feature).count() == 0


# --------------------------------------------------------------------------- #
# KML files land on disk exactly once
# --------------------------------------------------------------------------- #
def test_kml_file_stored_and_readable(env):
    client, _, upload_dir = env
    upload_kml(client, POINT_KML)

    stored = list(upload_dir.iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".kml"
    assert stored[0].read_bytes() == POINT_KML.encode("utf-8")
