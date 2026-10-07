"""
UploadedFile model — one row per file uploaded to the API.

Represents the *parent* side of the UploadedFile 1 ---- * Feature
relationship. A KML or Shapefile is stored once here, and every geometry
extracted from it becomes a row in the `features` table.

Columns:
    id          — primary key.
    filename    — original name supplied by the client (e.g. "parcels.kml").
    file_type   — format of the file (e.g. "kml", "shapefile").
    file_path   — where the file is stored on disk (e.g. "./uploads/xyz.kml").
    upload_crs  — coordinate reference system declared by the file.
                  Nullable: many KML/Shapefile bundles ship without one,
                  and a missing CRS must not block the upload.
    file_size_bytes — size of the stored file in bytes, recorded at
                  upload time so clients can confirm what was received.
    uploaded_at — when the file was received (UTC).
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow() -> datetime:
    """Current UTC time, returned naive.

    SQLite's DATETIME column has no timezone support, so we strip the
    tzinfo to keep values portable and comparable.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UploadedFile(Base):
    """A single uploaded geospatial file."""

    __tablename__ = "uploaded_files"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    file_type = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    upload_crs = Column(String, nullable=True)
    file_size_bytes = Column(Integer, nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=_utcnow)

    # One UploadedFile -> many Features.
    # "Feature" is resolved by name after both modules are imported,
    # which is why the string form is used here.
    features = relationship(
        "Feature",
        back_populates="file",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<UploadedFile id={self.id} filename={self.filename!r}>"
