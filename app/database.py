"""
Database engine, session factory, and base class.

This module is database-agnostic: SQLAlchemy works with SQLite, PostgreSQL,
MySQL, etc. The only SQLite-specific detail is the `check_same_thread`
connect arg, which is guarded by an if-check so switching to PostgreSQL
requires only changing DATABASE_URL in .env.

Key exports:
    engine       — the SQLAlchemy engine (manages the connection pool)
    SessionLocal — factory that creates new database sessions
    Base         — declarative base; all ORM models inherit from this
    get_db       — FastAPI dependency that yields a session per request
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
# SQLite requires check_same_thread=False because FastAPI handles requests
# across multiple threads, but SQLite's default mode restricts connections
# to the thread that created them. PostgreSQL doesn't have this limitation.
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)


# --------------------------------------------------------------------------- #
# Session factory
# --------------------------------------------------------------------------- #
# autocommit=False: we control when commits happen (explicit is better).
# autoflush=False:  prevents SQLAlchemy from issuing SQL before we're ready.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# --------------------------------------------------------------------------- #
# Declarative base
# --------------------------------------------------------------------------- #
# Every ORM model class (e.g., UploadedFile, Feature) will inherit from Base.
# Base.metadata.create_all(engine) creates all tables that don't exist yet.
Base = declarative_base()


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #
def get_db():
    """Yield a database session for the duration of a single request.

    FastAPI calls this automatically when a route parameter is typed as:
        db: Session = Depends(get_db)

    The `finally` block guarantees the session is closed even if the
    route handler raises an exception.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
