"""
Feature model — one row per geometry extracted from an uploaded file.

A shapefile may contain 500 parcels; each parcel becomes one Feature row
that belongs to the parent UploadedFile.

Columns:
    id                — primary key.
    file_id           — foreign key -> uploaded_files.id.
    feature_index     — position of the feature inside the source file
                        (0 for the first geometry, 1 for the next, ...).
    geometry_type     — "Polygon", "LineString", "Point", etc.
    geometry          — the geometry itself, serialized as WKT text
                        (e.g. "POLYGON ((30 10, 40 40, ...))").
                        No PostGIS / GeoAlchemy — processing happens in
                        the processing layer, not in the database.
    crs               — CRS of this geometry. Nullable.
    properties        — source attributes as JSON (name, id, area, ...).
    measurement_type  — "area" or "length". Nullable (filled in Phase 4+).
    measurement_value — numeric result. Nullable.
    measurement_unit  — "m2", "km2", "m", ... Nullable.
"""

from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class Feature(Base):
    """A single geometry extracted from an uploaded file."""

    __tablename__ = "features"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key: points at the parent row in `uploaded_files`.
    file_id = Column(Integer, ForeignKey("uploaded_files.id"), nullable=False, index=True)

    feature_index = Column(Integer, nullable=False)
    geometry_type = Column(String, nullable=False)

    # WKT text on purpose — see Phase 2 design notes.
    geometry = Column(String, nullable=False)

    crs = Column(String, nullable=True)

    # JSON works on SQLite (stored as TEXT) and on PostgreSQL later,
    # so the column type needs no change when we swap databases.
    properties = Column(JSON, nullable=True)

    measurement_type = Column(String, nullable=True)
    measurement_value = Column(Float, nullable=True)
    measurement_unit = Column(String, nullable=True)

    # Many Features -> one UploadedFile. This is the other half of the
    # pair started by UploadedFile.features.
    file = relationship("UploadedFile", back_populates="features")

    def __repr__(self) -> str:
        return f"<Feature id={self.id} file_id={self.file_id} type={self.geometry_type!r}>"
