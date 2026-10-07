# models package
#
# Importing the model modules here matters: SQLAlchemy only creates tables
# for classes that have been imported (and therefore registered on
# Base.metadata). app.main imports this package before calling
# Base.metadata.create_all(...), so both tables are discovered.

from app.models.uploaded_file import UploadedFile
from app.models.feature import Feature

__all__ = ["UploadedFile", "Feature"]
