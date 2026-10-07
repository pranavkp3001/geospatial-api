"""
Pydantic response models for the file upload endpoints.

Response models do two jobs:
  1. Document the JSON shape (they show up in /docs automatically).
  2. Act as a whitelist — only fields declared here reach the client,
     so internal values like `file_path` cannot leak by accident.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FileUploadResponse(BaseModel):
    """Returned by POST /api/files/ on success (201 Created).

    `file_path` is intentionally absent: it is an internal filesystem
    detail and exposing it leaks server layout information.

    `feature_count` reports how many Feature rows were extracted from
    the file: KML placemarks, or Shapefile records for .zip uploads.
    """

    # Allows construction directly from the SQLAlchemy ORM object.
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_type: str
    file_size_bytes: int
    feature_count: int = 0
    upload_crs: str | None = None
    uploaded_at: datetime
