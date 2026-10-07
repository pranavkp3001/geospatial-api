"""
Retrieval service — read-only queries behind the GET endpoints (Phase 7).

    API (thin)  ->  Service (this file)  ->  Data (ORM)

Splitting the queries out of the route handlers keeps the API layer
thin and makes them reusable (CLI, tests) without going through HTTP.
The service returns plain ORM objects; the API layer converts them to
response schemas.

Query shape (no N+1):
    get_file_by_id            -> 1 row + 1 COUNT of that file's features.
    get_file_measurements     -> 1 row + 1 SELECT of the file's features,
                                 ordered by feature_index so responses are
                                 deterministic.
"""

from sqlalchemy.orm import Session

from app.models import Feature, UploadedFile


def get_file_by_id(db: Session, file_id: int) -> tuple[UploadedFile | None, int]:
    """Look up one uploaded file and how many features it produced.

    Returns (record, feature_count); `(None, 0)` when the ID does not
    exist. The count is the processing result persisted at upload time —
    it is a stored-value read, not a recalculation.
    """
    record = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
    if record is None:
        return None, 0
    feature_count = (
        db.query(Feature).filter(Feature.file_id == file_id).count()
    )
    return record, feature_count


def get_file_measurements(
    db: Session, file_id: int
) -> tuple[UploadedFile | None, list[Feature]]:
    """Look up a file and every stored measurement row belonging to it.

    Returns (record, features); `(None, [])` when the ID does not exist.
    Features come ordered by feature_index so repeated calls yield the
    same response. The measurement columns were filled during upload
    processing (Phase 6) — nothing is recalculated or reprojected here.
    """
    record = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
    if record is None:
        return None, []
    features = (
        db.query(Feature)
        .filter(Feature.file_id == file_id)
        .order_by(Feature.feature_index.asc())
        .all()
    )
    return record, features