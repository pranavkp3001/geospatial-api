"""
Application settings loaded from environment variables.

Uses pydantic-settings to read values from a .env file (if it exists)
or from actual environment variables. Every configurable value in the
application lives here — nothing is hardcoded inside business logic.

Usage anywhere in the app:
    from app.config import settings
    print(settings.MAX_FILE_SIZE_MB)
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for the application.

    Attributes:
        DATABASE_URL: SQLAlchemy connection string.
            Default points to a local SQLite file.
            To switch to PostgreSQL later, change this to:
            "postgresql://user:pass@localhost:5432/aereo"
        UPLOAD_DIR: Directory where uploaded files are stored on disk.
        MAX_FILE_SIZE_MB: Maximum upload size in megabytes.
            Used by the validation layer — not hardcoded there.
    """

    DATABASE_URL: str = "sqlite:///./aereo.db"
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 10

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


# Module-level singleton.
# Every module imports this same instance, so settings are consistent
# across the entire application.
settings = Settings()
