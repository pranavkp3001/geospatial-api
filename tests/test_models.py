"""
Minimal Phase 2 tests: models, tables, and the UploadedFile -> Feature
relationship. Not the full test suite — that comes later.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import app
from app.models import UploadedFile, Feature


@pytest.fixture()
def engine():
    """Fresh in-memory SQLite database with all tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """A database session bound to the in-memory engine."""
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


def test_application_starts():
    """1. The application starts and /health responds."""
    # `with TestClient(app)` runs the lifespan handler, which is what
    # actually exercises application startup.
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_database_tables_are_created(engine):
    """2. Both tables exist with the expected columns."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "uploaded_files" in tables
    assert "features" in tables

    file_cols = {c["name"] for c in inspector.get_columns("uploaded_files")}
    assert file_cols == {
        "id", "filename", "file_type", "file_path", "upload_crs",
        "file_size_bytes", "uploaded_at",
    }

    feature_cols = {c["name"] for c in inspector.get_columns("features")}
    assert feature_cols == {
        "id", "file_id", "feature_index", "geometry_type", "geometry", "crs",
        "properties", "measurement_type", "measurement_value", "measurement_unit",
    }


def test_relationship_is_configured():
    """3. Both sides of the relationship exist and point at each other."""
    assert hasattr(UploadedFile, "features")
    assert hasattr(Feature, "file")

    # back_populates must reference the attribute on the other class.
    file_rel = UploadedFile.__mapper__.relationships["features"]
    feature_rel = Feature.__mapper__.relationships["file"]
    assert file_rel.back_populates == "file"
    assert feature_rel.back_populates == "features"


def test_one_file_can_have_multiple_features(session):
    """4. A single UploadedFile holds many Features."""
    uploaded = UploadedFile(
        filename="parcels.kml",
        file_type="kml",
        file_path="./uploads/parcels.kml",
        upload_crs="EPSG:4326",
        file_size_bytes=2048,
    )
    session.add(uploaded)
    session.flush()  # assigns uploaded.id

    for i in range(3):
        session.add(
            Feature(
                file=uploaded,          # navigate down: feature.file = uploaded
                feature_index=i,
                geometry_type="Polygon",
                geometry=f"POLYGON ((0 0, 1 0, 1 1, 0 0))",
                properties={"name": f"parcel-{i}"},
            )
        )
    session.commit()

    # Navigate back up: uploaded.features -> many features
    assert len(uploaded.features) == 3
    assert all(f.file is uploaded for f in uploaded.features)
    assert sorted(f.feature_index for f in uploaded.features) == [0, 1, 2]


def test_upload_crs_is_optional_and_defaults_applied(session):
    """upload_crs may be NULL; uploaded_at gets a default value."""
    uploaded = UploadedFile(
        filename="no_crs.kml",
        file_type="kml",
        file_path="./uploads/no_crs.kml",
        upload_crs=None,
        file_size_bytes=1024,
    )
    session.add(uploaded)
    session.commit()

    assert uploaded.id is not None
    assert uploaded.upload_crs is None
    assert uploaded.uploaded_at is not None


def test_feature_file_id_references_uploaded_file(session):
    """5. Feature.file_id stores the parent's primary key."""
    uploaded = UploadedFile(
        filename="roads.shp",
        file_type="shapefile",
        file_path="./uploads/roads.shp",
        file_size_bytes=512,
    )
    session.add(uploaded)
    session.flush()

    feature = Feature(
        file_id=uploaded.id,       # explicit foreign key, not the object
        feature_index=0,
        geometry_type="LineString",
        geometry="LINESTRING (0 0, 10 10)",
        crs="EPSG:3857",
        measurement_type="length",
        measurement_value=14.14,
        measurement_unit="m",
    )
    session.add(feature)
    session.commit()

    # Re-read from the database to prove it persisted, not just in memory.
    stored = session.query(Feature).filter_by(file_id=uploaded.id).one()
    assert stored.file_id == uploaded.id
    assert stored.file.id == uploaded.id
    assert stored.measurement_value == pytest.approx(14.14)
