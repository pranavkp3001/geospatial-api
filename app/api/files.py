"""
File upload endpoints.

This layer is intentionally thin: parse the multipart request, call the
upload service, and translate service errors into HTTP responses.
Validation, disk I/O, and database writes all live elsewhere.
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.processing.validation import UploadValidationError
from app.schemas.upload import FileUploadResponse
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
