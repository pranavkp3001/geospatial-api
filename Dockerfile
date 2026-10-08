# ---- Aereo Geospatial File Measurement API ---------------------------------
# Production-appropriate but intentionally simple Docker image.
# The image contains only the application code and its Python dependencies;
# the SQLite database and uploaded files live in mounted volumes at runtime
# (see docker-compose.yml).

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so rebuilds reuse the cached layer whenever
# requirements.txt is unchanged.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Runtime directories. ./uploads holds stored uploads and ./data holds the
# SQLite database file; both may be replaced by mounted volumes (compose does
# exactly that). They are created here so the container works even without
# mounts.
RUN mkdir -p /app/uploads /app/data

# The API port.
EXPOSE 8000

# Start the server on all interfaces inside the container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]