"""
Health check endpoint.

A lightweight route that confirms:
1. The API process is running.
2. The database is reachable.

This is the first router registered in the application. It also serves as
a reference example for how to add new routers in later phases.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Return the health status of the API and database.

    The route uses FastAPI's dependency injection: `Depends(get_db)` tells
    FastAPI to call `get_db()`, pass the yielded session as `db`, and
    close the session after the response is sent.

    We run `SELECT 1` as the simplest possible query to verify the
    database connection is alive.
    """
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    return {
        "status": "healthy",
        "database": db_status,
    }
