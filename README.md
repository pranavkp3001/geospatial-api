# Geospatial File Measurement API

A FastAPI service that accepts geospatial files (KML and Shapefile ZIPs), extracts
their features, stores geometry, attributes and coordinate reference system (CRS)
information, and computes geospatially correct measurements — area for polygons and
length for lines — using an automatically selected projected CRS.

The project is built for the Aereo **Software Development Engineer internship
assignment**. It demonstrates a layered, testable FastAPI codebase aimed at the kind
of file-ingestion workloads common in drone-data and geospatial pipelines: upload a
survey file, get structured features and measurements back, without guessing units or
cutting corners on coordinate systems.

## Overview

Geospatial files arrive in many formats and coordinate systems, and reporting an
"area" or "length" naively is wrong more often than it is right — measuring a
geographic CRS (e.g. EPSG:4326) in *degrees* produces numbers that are meaningless.
This API solves that by:

1. accepting `.kml` and `.zip` (Shapefile) uploads,
2. extracting every feature (geometry + attributes),
3. detecting each geometry's CRS,
4. transforming the *working copy* of the geometry into the UTM zone covering it,
5. computing area/length in metres, and
6. storing the result alongside the unmodified original geometry and CRS.

Everything is persisted in SQLite and remains retrievable through read-only APIs, so
uploaded files behave like a small, self-contained geodata store.

## Features

- **KML upload** — parses KML documents with fastkml (no external GIS service).
- **Shapefile ZIP upload** — accepts `.zip` archives containing a `.shp` bundle.
- **Feature extraction** — every placemark (KML) and record (Shapefile) becomes a stored feature.
- **Geometry handling** — geometries are stored as WKT text, ready for measurement and retrieval.
- **DBF property extraction** — Shapefile record attributes are stored as JSON.
- **CRS detection** — KML is EPSG:4326; Shapefile CRS comes from the optional `.prj` file.
- **CRS-aware measurements** — a geographic CRS is never measured in degrees.
- **Dynamic UTM transformation** — features in a geographic CRS are reprojected into the UTM zone covering their centroid before measuring.
- **Polygon/MultiPolygon area** — measured in square metres (`m²`).
- **LineString/MultiLineString length** — measured in metres (`m`).
- **Point/MultiPoint** — intentionally not measured.
- **Retrieval APIs** — metadata and per-feature measurement queries.
- **Validation** — extension, size, ZIP integrity, and filename safety rules.
- **Secure ZIP extraction** — path-traversal-proof extraction; names are validated before any file is written.
- **Rollback/error handling** — failed uploads leave no database rows and no files behind, and never expose internal paths or tracebacks.

## Tech Stack

- **Python 3.12**
- **FastAPI** (ASGI framework, automatic OpenAPI docs)
- **Uvicorn** (ASGI server)
- **SQLAlchemy** (ORM)
- **SQLite** (database)
- **Pydantic / pydantic-settings** (validation, response schemas, configuration)
- **Shapely** (geometry parsing and measurement math)
- **pyproj** (CRS handling and reprojection)
- **fastkml** (KML parsing)
- **pyshp** (Shapefile `.shp`/`.shx`/`.dbf` reading)
- **pytest** (test suite)
- **Docker** (containerization, see the Docker section)

## Architecture

```
Client
  ↓  HTTP (multipart upload / JSON responses)
FastAPI API layer        (app/api/)     — thin routes, HTTP status codes
  ↓
Service layer            (app/services/) — orchestration, transactions, queries
  ↓                          ↓
Processing layer         (app/processing/)   Database layer (app/models/ + app/database.py)
  — validation, parsing,        — SQLAlchemy ORM models
    ZIP extraction, geometry    — session management
```

The project follows a strict **layered** design:

| Layer | Location | Responsibility |
|---|---|---|
| **API** | `app/api/` | Translates HTTP into service calls and back. Route handlers are deliberately thin: they parse the request, call a service, and turn results/errors into status codes. No business logic lives here. |
| **Service** | `app/services/` | The orchestration layer. `upload.py` runs the whole upload pipeline — validate → store → parse → persist → commit — in one transaction with rollback on failure. `retrieval.py` contains the read-only queries behind the GET endpoints. |
| **Processing** | `app/processing/` | Pure logic with no HTTP or database knowledge. Validation rules, safe ZIP extraction, KML and Shapefile parsers (which emit plain `ParsedFeature` dataclasses), and the CRS-aware measurement code. Every module is independently testable. |
| **Database** | `app/models/` + `app/database.py` | SQLAlchemy `UploadedFile`/`Feature` models, the engine/session factory, and the `get_db` dependency. |

Additional design points:

- **API routes stay thin**; all real work sits behind them in services.
- **Business logic lives in services**, so it is reusable outside HTTP (CLI, tests).
- **KML/Shapefile parsing lives in processing**, as pure functions with no framework imports.
- **Database persistence uses SQLAlchemy** against a plain SQLite file — no PostGIS or geospatial extensions; geometry math happens in the processing layer.
- **Original uploaded files are stored locally** in `UPLOAD_DIR` (default `./uploads/`) under a generated UUID name.
- **Temporary Shapefile extraction directories are cleaned up** — ZIP contents are unpacked into a `tempfile.TemporaryDirectory` that is always removed, success or failure. Only the original ZIP remains stored.

## Project Structure

```
aereo-geospatial-api/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app, lifespan (create_all), routers, generic 500 handler
│   ├── config.py                # Settings (pydantic-settings): DATABASE_URL, UPLOAD_DIR, MAX_FILE_SIZE_MB
│   ├── database.py              # Engine, SessionLocal, Base, get_db dependency
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py            # GET /health
│   │   └── files.py             # POST /api/files/, GET /api/files/{id}/, GET /api/files/{id}/measurements/
│   ├── models/
│   │   ├── __init__.py
│   │   ├── uploaded_file.py     # UploadedFile model (parent side of the relation)
│   │   └── feature.py           # Feature model (child side)
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── upload.py            # FileUploadResponse (201 response model)
│   │   └── retrieval.py         # FileResponse, MeasurementsResponse, MeasurementFeatureResponse
│   ├── services/
│   │   ├── __init__.py
│   │   ├── upload.py            # Upload orchestration, persistence, rollback, cleanup
│   │   └── retrieval.py         # Read-only queries behind the GET endpoints
│   └── processing/
│       ├── __init__.py
│       ├── validation.py        # Extension / size / ZIP / filename rules
│       ├── zip_extract.py       # Safe (path-traversal-proof) ZIP extraction
│       ├── kml_parser.py        # fastkml -> list[ParsedFeature]
│       ├── shapefile_parser.py  # pyshp -> list[ParsedFeature] (+ .prj CRS)
│       ├── parsed_feature.py    # ParsedFeature dataclass (parser output contract)
│       └── geometry.py          # CRS-aware area/length measurement (UTM transformation)
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Shared fixtures: TestClient, upload helpers, ZIP builders
│   ├── test_upload.py           # Upload endpoint basics
│   ├── test_models.py           # ORM model behaviour
│   ├── test_kml_processing.py   # KML parsing + measurement
│   ├── test_shapefile_processing.py  # ZIP/Shapefile handling
│   ├── test_geometry_measurement.py  # UTM/CRS measurement rules
│   ├── test_retrieval.py        # GET metadata + measurements endpoints
│   └── test_error_handling.py   # 400/404/500 behaviours, rollback, security probes
├── requirements.txt
├── .env.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Setup

### Clone

```bash
git clone <repository-url>
cd aereo-geospatial-api
```

### Virtual environment

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
python -m venv venv
source venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Environment

All configuration is read from environment variables via pydantic-settings. Optional —
copy the template and edit if you want non-default values:

```bash
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

Available configuration values (all optional — sensible defaults exist):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./aereo.db` | SQLAlchemy connection string. |
| `UPLOAD_DIR` | `./uploads` | Directory where uploaded files are stored. |
| `MAX_FILE_SIZE_MB` | `10` | Maximum upload size in megabytes. |

### Run

```bash
uvicorn app.main:app --reload
```

- API: <http://127.0.0.1:8000>
- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>

On startup the application creates its tables (and the upload directory is created
on first upload).

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload a `.kml` or `.zip` (Shapefile) file. Returns `201 Created`. |
| `GET` | `/api/files/{id}/` | Metadata for one uploaded file. Returns `200`, or `404` if it does not exist. |
| `GET` | `/api/files/{id}/measurements/` | All stored features with their measurements for one file. Returns `200`, or `404` if it does not exist. |
| `GET` | `/health` | Liveness + database connectivity check. |

### POST /api/files/ — upload a file

Send a `multipart/form-data` request with a single part named `file`:

```bash
# Linux / macOS
curl -X POST -F "file=@parcels.kml" http://127.0.0.1:8000/api/files/

# Windows PowerShell (curl.exe)
curl.exe -X POST -F "file=@parcels.zip" http://127.0.0.1:8000/api/files/
```

- **Accepted file types:** `.kml` (parsed directly) and `.zip` (must contain exactly
  one Shapefile bundle). The extension check is case-insensitive.
- **Upload size limit:** `MAX_FILE_SIZE_MB` (default **10 MB**). The limit is enforced
  *while* the file streams to disk, so an oversized upload is aborted instead of being
  fully written first.
- **Successful response (`201 Created`):**

```json
{
  "id": 1,
  "filename": "parcels.kml",
  "file_type": "kml",
  "file_size_bytes": 1234,
  "feature_count": 3,
  "upload_crs": null,
  "uploaded_at": "2026-10-08T12:00:00"
}
```

  `feature_count` is the number of features extracted from the file; `upload_crs` is
  reserved for a file-level CRS and is currently always `null` (per-feature CRS is
  what carries real information).

- **Common `400 Bad Request` errors** (validation problems — the message explains the
  cause, but never reveals internal paths or stack traces):

| Cause | Example message |
|---|---|
| Unsupported extension | `Unsupported file type '.txt'. Allowed extensions: .kml, .zip.` |
| File too large | `File is too large: 11534336 bytes exceeds the 10485760 byte limit (10 MB).` |
| `.zip` is not an archive | `File is not a valid ZIP archive.` |
| Corrupt archive | `ZIP archive could not be read (it may be corrupt or encrypted).` |
| Path traversal | `ZIP contains a path that escapes the archive: '..\..\x.shp'.` |
| No Shapefile inside | `ZIP does not contain a Shapefile (.shp file).` |
| Incomplete bundle | `Shapefile is incomplete: parcels.shp is missing .dbf.` |
| Multiple Shapefiles | `ZIP contains multiple Shapefiles (...); upload exactly one per ZIP.` |
| Shapefile not readable | `Shapefile could not be read: ...` |
| KML not parseable | `File is not a valid KML document.` |

- **`404` behavior:** only the retrieval endpoints return `404` (detail: `File not
  found`). Uploads never 404 — a bad upload is a client error (400).

### GET /api/files/{id}/ — file metadata

Returns the persisted metadata for one uploaded file (with its feature count):

```json
{
  "id": 1,
  "filename": "parcels.kml",
  "file_type": "kml",
  "upload_crs": null,
  "uploaded_at": "2026-10-08T12:00:00",
  "file_size_bytes": 1234,
  "feature_count": 3
}
```

Unknown IDs return `404 {"detail": "File not found"}`.

### GET /api/files/{id}/measurements/ — stored measurements

Returns every stored feature of the file, ordered by `feature_index`, with its
measurement. Fields that do not apply (points, unsupported geometry, missing or
unusable CRS) come back as `null`:

```json
{
  "file_id": 1,
  "features": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "measurement_type": "area",
      "measurement_value": 123456.78,
      "measurement_unit": "m²"
    },
    {
      "feature_index": 1,
      "geometry_type": "MultiLineString",
      "measurement_type": "length",
      "measurement_value": 2345.67,
      "measurement_unit": "m"
    },
    {
      "feature_index": 2,
      "geometry_type": "Point",
      "measurement_type": null,
      "measurement_value": null,
      "measurement_unit": null
    }
  ]
}
```

These endpoints are **read-only**: they serve stored rows and never re-parse the
file or touch disk. Note that `file_path` is intentionally excluded from every
response — it is an internal filesystem detail and is never exposed to clients.

### GET /health — health check

```json
{
  "status": "healthy",
  "database": "connected"
}
```

Runs `SELECT 1` against the database so both process liveness and DB connectivity
are verified.

## Processing Flow

Every upload follows the same chain:

```
Upload
  → validation          (extension, size, ZIP integrity, filename safety)
  → save original file  (UPLOAD_DIR, generated name)
  → parse               (KML in place; ZIP extracted to a temp dir first)
  → extract features    (placemarks / records -> ParsedFeature list)
  → persist features    (Feature rows linked to the UploadedFile row)
  → calculate measurements (CRS-aware area/length per feature)
  → commit transaction
```

The whole chain runs inside **one database transaction** and one try/except: if any
step fails, the transaction is rolled back, the stored file is deleted, and the
client receives a clean `400` (expected input problems) or `500` (unexpected internal
failure). A failed upload leaves no `UploadedFile` row, no `Feature` rows, and no
file on disk.

### KML processing

1. The file is streamed to disk (size limit enforced while writing).
2. `fastkml` parses the document; placemarks are discovered inside documents and
   folders (recursively).
3. Geometries are converted to a `ParsedFeature` (WKT geometry, type, attributes,
   and `crs="EPSG:4326"` — the WGS84 datum KML coordinates are defined in).
4. Unsupported KML geometries (e.g. `Model`, multi-geometries) are skipped with a
   logged reason instead of crashing the upload.

### Shapefile processing (`.zip`)

1. The ZIP is validated as a readable, uncorrupted archive (`testzip()`).
2. The archive is extracted into a **temporary directory** (see the next section).
3. Exactly one valid Shapefile bundle must be present — `.shp`, `.shx` and `.dbf`
   are required, `.prj` is optional.
4. `pyshp` reads geometries and DBF attributes; the geometry becomes WKT, attributes
   become a JSON object, and the CRS comes from `.prj` if present.
5. Feature rows are persisted; the temporary directory is deleted; **only the
   original ZIP remains stored** in `UPLOAD_DIR`.

## Shapefile Processing (ZIP security)

- **Secure extraction:** `zip_extract.safe_extract()` validates *every* member name
  before writing anything — entries that escape the extraction directory
  (`../`, `..\`) or use absolute paths are rejected with a `400` and nothing is
  written. Python's `extractall` is never used.
- **Exactly one valid bundle:** the ZIP must contain exactly one `.shp` file with its
  `.shx` and `.dbf` siblings. Missing siblings or multiple Shapefiles are rejected.
- **`.prj` is optional:** when present it supplies the CRS; when absent the features
  are still stored but cannot be measured (measurement fields stay `null`).
- **DBF properties are stored as JSON** in the `properties` column.
- **Extracted files are temporary:** they live in a `TemporaryDirectory` under the
  system temp dir (never in `uploads/`) and are always removed.
- **The original ZIP remains stored:** only the archive itself is kept in `UPLOAD_DIR`.

## Measurement Flow

```
Feature geometry (WKT) + geometry type + CRS
  → parse WKT (Shapely)
  → inspect geometry type       (area if Polygon/MultiPolygon, length if Line/MultiLine, none otherwise)
  → inspect CRS
  → determine an appropriate projected CRS
  → transform a working copy of the geometry
  → calculate area/length in the projected CRS
  → store the result (measurement_type / value / unit)
```

**Why EPSG:4326 must never be measured directly:** in a geographic CRS, coordinates
are angles (degrees of latitude/longitude). A degree of latitude and a degree of
longitude cover different real-world distances, which change with location. "Area"
or "length" computed directly on such coordinates is not a real area or length — a
1° × 1° box is not a square. A projected CRS works in a linear unit (metres), so
measurements only make sense after transformation into one.

**Dynamic UTM selection:** for features in a geographic CRS, `geometry.py` computes
the feature's centroid, derives the UTM zone covering it (3°-wide zones), and picks
`EPSG:326xx` for the northern hemisphere or `EPSG:327xx` for the southern
hemisphere. The working copy is transformed to that UTM zone with pyproj
(`always_xy=True`, so longitudes are always the first coordinate) and the
area/length is computed there. A different UTM zone is chosen for each feature, so
zones crossing the usual 6° UTM boundaries are measured correctly. Feature inputs
and transformation are all handled in the projected plane — a geographic CRS is never
measured directly.

**No mutation:** the transformation applies only to a throwaway working copy. The
stored WKT geometry and the stored CRS are never altered by measuring.

## CRS Handling

- **KML** is treated as **EPSG:4326** (WGS84), which is what KML coordinates
  are specified in.
- **Shapefile** CRS comes from the **`.prj`** file when the bundle ships with one.
- **Missing or invalid CRS ⇒ measurement unavailable:** if no `.prj` is present, or
  its contents cannot be parsed, the upload still succeeds and the features are
  stored — but their measurement fields stay `null`. The API *never* assumes a
  default CRS such as EPSG:4326 to rescue a measurement.
- **Geographic CRS is transformed before measurement** using the dynamic UTM
  selection described above.
- **Projected CRS with unsupported/non-metre units is not blindly treated as
  metres:** a projected CRS whose axes are metres (UTM, state plane metre variants,
  …) is measured in its own plane; a projected CRS in other units (e.g. feet) can
  produce misleading numbers and is refused — those features keep `null` measurement
  fields rather than a guessed value.
- **The original CRS is preserved:** `crs` on each stored feature is exactly what the
  file declared, never rewritten to whatever was used for measuring.

## Database Design

Two tables form a one-to-many relation:

```
UploadedFile 1 ──── * Feature
```

**`UploadedFile`** — one row per uploaded file. Key fields: `id`, `filename` (the
client-supplied name, sanitized, kept for display), `file_type` (`kml` or `zip`),
`file_path` (internal storage location — never exposed in API responses),
`upload_crs` (reserved; nullable), `file_size_bytes`, `uploaded_at`.

**`Feature`** — one row per extracted geometry, linked to its parent file. Key
fields: `file_id` (foreign key), `feature_index` (position in the source file,
0-based), `geometry_type`, `geometry` (WKT text), `crs` (nullable), `properties`
(JSON), and the measurement trio `measurement_type` / `measurement_value` /
`measurement_unit` (all nullable).

Design choices:

- **WKT is stored as text** because it is a compact, unambiguous, human-readable
  serialization that both Shapely and pyproj can load directly. All geometry math
  happens in the processing layer, so no spatial database features are needed.
- **`properties` is stored as JSON** because DBF/KML attributes are free-form
  key/value data; JSON keeps them lossless and portable without a fixed schema.
- **SQLite was selected for this assignment** instead of PostGIS because the workload
  is a self-contained API — upload, measure, retrieve — with no concurrent spatial
  analytics. SQLite gives the same SQLAlchemy interface for the assignment's scope
  and requires no external server. Switching to PostgreSQL/PostGIS later only means
  changing `DATABASE_URL` (and adding geometry columns if server-side spatial queries
  are ever wanted).

## Error Handling

| Situation | Response |
|---|---|
| Expected invalid input (extension, size, ZIP, Shapefile, KML, filename) | `400` with a message-only `detail`. |
| Missing resource (retrieval of an unknown id) | `404 {"detail": "File not found"}`. |
| Unexpected internal failure | Generic `500 {"detail": "Internal server error while processing the file."}` — no traceback, no internal paths, logged fully server-side. |

- **Database rollback:** the upload pipeline runs in one transaction. On any failure
  the session is rolled back, so no `UploadedFile` or `Feature` rows survive a
  rejected upload.
- **Uploaded-file cleanup:** the stored copy in `UPLOAD_DIR` is deleted when
  processing fails (`unlink(missing_ok=True)`).
- **Temporary extraction cleanup:** Shapefile extraction directories are
  `TemporaryDirectory`s removed on success *and* failure; parser file handles are
  owned and always closed so cleanup never blocks on Windows.
- **No internal filesystem paths or tracebacks are exposed to clients.** Exception
  messages that can embed server paths (e.g. `OSError`) are replaced with fixed safe
  text, and the catch-all `500` handler never includes exception details in the body.
- Errors that are the client's fault carry just enough information to fix the upload,
  and nothing more.

## Testing

The suite uses **pytest** with a `TestClient` against the FastAPI app, plus an
in-memory-per-test SQLite database and generated KML/ZIP fixtures.

```bash
pytest -q
```

Final result for this repository: **142 passed**.

Known warnings (library-level, not caused by the project code, and harmless):

- `StarletteDeprecationWarning` emitted by the `httpx`/fastapi `TestClient` used in
  the test suite.
- A fastkml `UserWarning` noting that the optional `lxml` package is not installed
  (lxml only affects KML pretty-printing, not parsing).

## Docker

A simple two-asset setup ships with the repo: a `Dockerfile` (Python 3.12-slim) and
a `docker-compose.yml` that runs the API with persistent volumes for the database
and uploaded files. No extra infrastructure (Redis, Postgres, …) is included.

```bash
# Build and start
docker compose up --build

# The API is now at http://127.0.0.1:8000
# Swagger UI at http://127.0.0.1:8000/docs

# Stop the container (data persists in the named volumes)
docker compose down
```

The database file and uploads live in named volumes (`db-data`, `uploads-data`), so
they survive rebuilds. `UPLOAD_DIR` and `DATABASE_URL` are overridden inside the
container to point at those mounted paths; `MAX_FILE_SIZE_MB` defaults to 10 and can
be overridden in the compose `environment` block.