"""
Shared pytest fixtures for the whole test suite.

`env` gives every test an isolated upload directory and an isolated
SQLite database (both under pytest's tmp_path), wired into the real app
via a FastAPI dependency override. Nothing here touches ./uploads or
./aereo.db.

Also here: `build_zip`, the in-memory ZIP builder used by the upload
and Shapefile tests. Its default payload is a *complete* Shapefile
bundle, because Phase 5 actually processes .zip contents and would
reject a fixture of random bytes as an incomplete Shapefile.
"""

import io
import tempfile
import zipfile
from pathlib import Path

import pytest
import shapefile  # pyshp
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app

# A real WGS 84 / UTM zone 43N WKT definition with its EPSG authority.
UTM43N_PRJ = (
    'PROJCS["WGS 84 / UTM zone 43N",GEOGCS["WGS 84",'
    'DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",75],'
    'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
    'PARAMETER["false_northing",0],UNIT["metre",1],'
    'AUTHORITY["EPSG","32643"]]'
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated upload dir + database, wired into a TestClient."""
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(upload_dir))

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    # Swap the app's database dependency for our throwaway one.
    app.dependency_overrides[get_db] = override_get_db
    # raise_server_exceptions=False: Starlette's ServerErrorMiddleware always
    # re-raises unhandled exceptions after sending the 500 response (so real
    # servers can log them). The Phase 8 internal-failure tests must see the
    # safe 500 response, not the re-raised exception.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, TestingSession, upload_dir
    app.dependency_overrides.clear()
    engine.dispose()


def post_file(client, name: str, content: bytes):
    """POST a single part named `file`, as a browser/curl would."""
    return client.post(
        "/api/files/",
        files={"file": (name, content, "application/octet-stream")},
    )


def default_shapefile_entries() -> dict[str, bytes]:
    """Bytes of a tiny but complete Shapefile bundle, generated with pyshp.

    Returns {name: bytes} for roads.shp/.shx/.dbf/.prj — one Point
    feature with one attribute. Generated on the fly so no binary
    fixtures are committed to the repository.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "roads"
        writer = shapefile.Writer(str(stem))
        writer.field("name", "C", size=40)
        writer.point(-95.8, 40.9)
        writer.record(name="Main Street")
        writer.close()
        (stem.with_suffix(".prj")).write_text(UTM43N_PRJ, encoding="utf-8")
        return {
            path.name: path.read_bytes()
            for path in Path(tmp).iterdir()
            if path.stem == "roads"
        }


def build_zip(entries: dict[str, bytes] | None = None) -> bytes:
    """Build a real ZIP archive in memory.

    Defaults to a complete Shapefile bundle — what a valid `.zip`
    upload must contain now that Phase 5 processes archive contents.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (entries or default_shapefile_entries()).items():
            zf.writestr(name, data)
    return buffer.getvalue()
