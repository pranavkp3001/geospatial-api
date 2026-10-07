"""
Pydantic response models for the retrieval endpoints (Phase 7).

Like the upload schemas, these act as whitelists: only the fields
declared here can reach the client, so internal values such as
`file_path` or raw ORM plumbing cannot leak by accident.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FileResponse(BaseModel):
    """Returned by GET /api/files/{id}/ on success (200 OK).

    `feature_count` is how many Feature rows the upload produced; it is
    queried at retrieval time rather than stored as a column.

    `file_path` is intentionally absent: it is an internal filesystem
    detail, and exposing it leaks server layout information.
    """

    id: int
    filename: str
    file_type: str
    upload_crs: str | None = None
    uploaded_at: datetime
    file_size_bytes: int
    feature_count: int


class MeasurementFeatureResponse(BaseModel):
    """One stored measurement row from the features table.

    The measurement fields map 1:1 onto Feature columns and are nullable
    on purpose: Points, unmeasured geometries, and features whose CRS was
    missing or unusable are stored with NULL measurements (Phase 6) and
    must be reported as NULL — not dropped, and not refabricated.

    `from_attributes` lets responses be built straight from ORM Feature
    rows; `file_path` and friends are not declared here, so they cannot
    leak into the payload.
    """

    model_config = ConfigDict(from_attributes=True)

    feature_index: int
    geometry_type: str
    measurement_type: str | None = None
    measurement_value: float | None = None
    measurement_unit: str | None = None


class MeasurementsResponse(BaseModel):
    """Returned by GET /api/files/{id}/measurements/ on success (200 OK)."""

    file_id: int
    features: list[MeasurementFeatureResponse]