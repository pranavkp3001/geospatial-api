"""
FastAPI application entry point.

This module:
1. Creates the FastAPI app instance with metadata (title, description).
2. Uses a 'lifespan' context manager to run startup/shutdown logic.
3. Registers all API routers.

To run the server:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import engine, Base
from app.api.health import router as health_router
from app.api.files import router as files_router

# Importing the models package registers UploadedFile and Feature on
# Base.metadata. Without this import, create_all() below would silently
# create no tables at all.
from app.models import UploadedFile, Feature  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — runs on startup and shutdown.

    Startup:
        Creates all database tables defined by ORM models that inherit
        from `Base`. If the tables already exist, this is a no-op.
        (In production, you'd use Alembic migrations instead.)

    Shutdown:
        Nothing to clean up for now. The `yield` is required by the
        async context manager protocol.
    """
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Aereo Geospatial File Measurement API",
    description=(
        "Upload geospatial files (KML, Shapefile) and extract features "
        "with area and length measurements."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ---- Register routers ---------------------------------------------------- #
# Each router is a separate module in app/api/.
# To add a new router:
#   1. Create app/api/your_router.py with `router = APIRouter(...)`
#   2. Import it here
#   3. Call app.include_router(your_router, prefix="...", tags=["..."])

app.include_router(health_router)
app.include_router(files_router)  # POST /api/files/
