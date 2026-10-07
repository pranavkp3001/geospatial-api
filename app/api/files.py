"""
File upload + retrieval endpoints.

This layer is intentionally thin: parse the multipart request / path
parameters, call the upload or retrieval service, and translate service
results into HTTP responses. Validation, disk I/O, and database queries
all live elsewhere.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.processing.validation import UploadValidationError
from app.schemas.retrieval import FileResponse, MeasurementsResponse
from app.schemas.upload import FileUploadResponse
from app.services import retrieval as retrieval_service
from app.services import upload as upload_service

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_file(
    file: UploadFile = File(..., description="A .kml or .zip file to upload"),
    db: Session = Depends(get_db),
) -> FileUploadResponse:
    """Upload a geospatial file.

    Accepts `multipart/form-data` with a single part named `file`.

    Returns 201 with the stored file's metadata. Returns 400 if the
    extension is unsupported, the file exceeds `MAX_FILE_SIZE_MB`, a
    `.zip` upload is not a valid archive or does not hold exactly one
    complete Shapefile, or a file's geometry cannot be parsed.
    """
    try:
        record = await upload_service.upload_file(file, db)
    except UploadValidationError as exc:
        # Validation problems are the client's fault -> 400, message only.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return record


# --------------------------------------------------------------------------- #
# Retrieval (Phase 7). The measurements route is declared BEFORE the detail
# route so a request for /api/files/{id}/measurements/ can never be matched
# against /api/files/{id} with "measurements" parsed as the id. Both are
# read-only: they serve stored rows, they never touch disk or re-parse.
# --------------------------------------------------------------------------- #
@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    status_code=status.HTTP_200_OK,
)
def get_file_measurements(
    file_id: int,
    db: Session = Depends(get_db),
) -> MeasurementsResponse:
    """Return the stored measurement rows of the uploaded file.

    Features are ordered by feature_index. Measurement fields are NULL
    for features that were stored without a measurement (Points,
    unmeasured geometries, missing/unusable CRS). 404 if the file does
    not exist.
    """
    record, features = retrieval_service.get_file_measurements(db, file_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return MeasurementsResponse(file_id=record.id, features=features)


@router.get(
    "/{file_id}/",
    response_model=FileResponse,
    status_code=status.HTTP_200_OK,
)
def get_file(
    file_id: int,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Return metadata for one uploaded file. 404 if it does not exist."""
    record, feature_count = retrieval_service.get_file_by_id(db, file_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(
        id=record.id,
        filename=record.filename,
        file_type=record.file_type,
        upload_crs=record.upload_crs,
        uploaded_at=record.uploaded_at,
        file_size_bytes=record.file_size_bytes,
        feature_count=feature_count,
    )
