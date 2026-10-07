"""
Shapefile parsing — the "Processing" layer for .zip uploads.

Responsibilities:
    1. `locate_shapefile()` — find the one complete Shapefile bundle
       inside an extracted ZIP (discovery policy below).
    2. `parse_shapefile()`  — read every feature with pyshp and return
       plain `ParsedFeature` records: WKT geometry, DBF attributes as a
       JSON-ready dict, and the CRS from the .prj file (or None).

Like the KML parser, this module knows nothing about HTTP or
SQLAlchemy; the service layer decides what to persist.

Library choice: `pyshp` — pure Python, no GDAL/GeoPandas needed. It
exposes each shape through `__geo_interface__`, a GeoJSON-shaped dict
with the rings already grouped (holes detected by ring orientation,
disjoint outers grouped into MultiPolygon), so geometry conversion is
formatting, not coordinate math.

DISCOVERY POLICY (deliberately strict — we never guess):
    * zero .shp files              -> 400 "does not contain a Shapefile"
    * any .shp missing .shx/.dbf   -> 400 naming the missing sidecar(s)
    * more than one complete .shp  -> 400 "upload exactly one per ZIP"
    * exactly one complete .shp    -> process it

CRS POLICY:
    * no .prj               -> crs = None (we NEVER assume EPSG:4326)
    * .prj has an EPSG code  -> "EPSG:xxxx" (AUTHORITY/ID node, or a
                                plain "EPSG:xxxx" string)
    * .prj is WKT without one -> the WKT string itself
    * .prj unreadable/garbage -> crs = None
    Actual CRS *transformation* happens in Phase 6; this phase only
    stores what the file declares.
"""

import logging
import math
import re
import datetime as dt
from pathlib import Path

import shapefile  # pyshp

from app.processing.parsed_feature import ParsedFeature
from app.processing.validation import UploadValidationError

logger = logging.getLogger(__name__)

# WKT1 writes AUTHORITY["EPSG","32643"], WKT2 writes ID["EPSG",32643].
_WKT1_EPSG = re.compile(r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"(\d+)"\s*\]', re.IGNORECASE)
_WKT2_EPSG = re.compile(r'\bID\s*\[\s*"EPSG"\s*,\s*(\d+)\s*\]', re.IGNORECASE)
_PLAIN_EPSG = re.compile(r"^EPSG:\d+$", re.IGNORECASE)

# Top-level keywords that mean "this .prj really is CRS WKT".
_WKT_KEYWORDS = (
    "GEOGCS", "PROJCS", "GEOCCS", "LOCAL_CS", "COMPD_CS", "VERT_CS",
    "GEOGCRS", "PROJCRS", "GEODCRS", "ENGCRS", "PARAMETRICCRS", "TIMECRS",
    "BOUNDCRS", "DERIVEDPROJCRS",
)

# Sidecars a .shp cannot live without (plus .prj, which is optional).
_REQUIRED_SIDECARS = (".shx", ".dbf")


def locate_shapefile(root: Path) -> Path:
    """Find the single complete Shapefile inside extracted `root`.

    Search is recursive — ZIPs often nest everything in a folder.

    Raises:
        UploadValidationError: zero Shapefiles, an incomplete bundle,
            or more than one candidate (see the policy in the module
            docstring). Nothing is ever chosen at random.
    """
    candidates = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".shp"
    )
    if not candidates:
        raise UploadValidationError("ZIP does not contain a Shapefile (.shp file).")

    incomplete: list[tuple[Path, list[str]]] = []
    complete: list[Path] = []
    for shp in candidates:
        missing = [ext for ext in _REQUIRED_SIDECARS if _sibling(shp, ext) is None]
        if missing:
            incomplete.append((shp, missing))
        else:
            complete.append(shp)

    if incomplete:
        details = "; ".join(
            f"{shp.relative_to(root)} is missing {', '.join(missing)}"
            for shp, missing in incomplete
        )
        raise UploadValidationError(f"Shapefile is incomplete: {details}.")
    if len(complete) > 1:
        names = ", ".join(str(path.relative_to(root)) for path in complete)
        raise UploadValidationError(
            f"ZIP contains multiple Shapefiles ({names}); upload exactly one per ZIP."
        )
    return complete[0]


def parse_shapefile(shp_path: Path) -> list[ParsedFeature]:
    """Read the Shapefile bundle at `shp_path` and return its features.

    The bundle must already be complete (.shp + .shx + .dbf) — that is
    `locate_shapefile()`'s job.

    Raises:
        UploadValidationError: the bundle exists but cannot be read as
            a Shapefile (corrupt header, broken attribute table, ...).
            The API layer turns this into HTTP 400.
    """
    try:
        # The file handles are opened *here* by design. When pyshp opens
        # files itself from a path and construction fails halfway (for
        # example a corrupt .dbf), it leaks that handle; the temporary
        # extraction directory can then not be deleted on Windows (file
        # still in use) and a clean 400 turns into a 500. Handing pyshp
        # file objects we own means the `with` block closes every handle,
        # success and failure alike, before the temp dir is removed.
        with (
            open(shp_path, "rb") as shp_fh,
            open(Path(shp_path).with_suffix(".shx"), "rb") as shx_fh,
            open(Path(shp_path).with_suffix(".dbf"), "rb") as dbf_fh,
        ):
            try:
                # encodingErrors="replace" — a stray non-decodable byte in
                # the DBF must never crash the upload; it just shows as
                # "?" in one attribute value.
                reader = shapefile.Reader(
                    shp=shp_fh,
                    shx=shx_fh,
                    dbf=dbf_fh,
                    encodingErrors="replace",
                )
            except Exception as exc:
                # pyshp raises struct.error / ShapefileException / ...
                # depending on which part of an untrusted file is broken.
                raise UploadValidationError(f"Shapefile could not be read: {exc}") from exc

            try:
                if reader.numRecords is None:
                    raise UploadValidationError(
                        "Shapefile's attribute table (.dbf) could not be read."
                    )
                crs = _crs_from_prj(shp_path)
                features: list[ParsedFeature] = []
                for index in range(reader.numRecords):
                    parsed = _parse_feature(reader, index, crs)
                    if parsed is not None:
                        # Index comes from the number of features kept so
                        # far, so skipped records leave no gaps (as in KML).
                        parsed.feature_index = len(features)
                        features.append(parsed)
                return features
            finally:
                reader.close()  # close file handles before the temp dir is deleted
    except UploadValidationError:
        raise


def _parse_feature(
    reader: "shapefile.Reader", index: int, crs: str | None
) -> ParsedFeature | None:
    """Convert record `index` into a ParsedFeature, or None to skip it.

    Skipping (rather than failing) keeps one unusable record — an empty
    NULL shape, for example — from rejecting the whole upload.
    """
    try:
        shape = reader.shape(index)
        record = reader.record(index)
    except Exception as exc:
        raise UploadValidationError(
            f"Shapefile feature {index} could not be read: {exc}"
        ) from exc

    try:
        geo = shape.__geo_interface__
    except Exception as exc:
        # pyshp raises GeoJSON_Error for NULL shapes, which have no
        # geometry to store.
        logger.warning("Skipping Shapefile feature %d: %s.", index, exc)
        return None

    converted = _geojson_to_wkt(geo) if geo else None
    if converted is None:
        logger.warning(
            "Skipping Shapefile feature %d: geometry %r cannot be stored.",
            index,
            (geo or {}).get("type"),
        )
        return None

    geometry_type, wkt = converted
    return ParsedFeature(
        feature_index=index,  # corrected by the caller after skips
        geometry_type=geometry_type,
        geometry=wkt,
        properties=_properties(record),
        crs=crs,
    )


# --------------------------------------------------------------------------- #
# Geometry: GeoJSON (from pyshp) -> WKT text
# --------------------------------------------------------------------------- #
def _geojson_to_wkt(geo: dict) -> tuple[str, str] | None:
    """Format a GeoJSON-shaped geometry dict as (type, WKT).

    Returns None when the type is one we do not store (e.g. a
    GeometryCollection). Coordinates come from pyshp already grouped
    correctly; this only formats them — no coordinate math happens here.
    """
    kind = geo.get("type")
    coords = geo.get("coordinates")
    if not coords:
        return None

    if kind == "Point":
        return "Point", f"POINT ({_xy(coords)})"
    if kind == "MultiPoint":
        points = ", ".join(f"({_xy(c)})" for c in coords)
        return "MultiPoint", f"MULTIPOINT ({points})"
    if kind == "LineString":
        return "LineString", f"LINESTRING ({_vertices(coords)})"
    if kind == "MultiLineString":
        lines = ", ".join(f"({_vertices(line)})" for line in coords)
        return "MultiLineString", f"MULTILINESTRING ({lines})"
    if kind == "Polygon":
        rings = ", ".join(f"({_vertices(ring)})" for ring in coords)
        return "Polygon", f"POLYGON ({rings})"
    if kind == "MultiPolygon":
        polygons = ", ".join(
            "(" + ", ".join(f"({_vertices(ring)})" for ring in poly) + ")"
            for poly in coords
        )
        return "MultiPolygon", f"MULTIPOLYGON ({polygons})"
    return None


def _xy(coord) -> str:
    """One position -> "x y" (pyshp's GeoJSON view is 2D)."""
    return f"{coord[0]} {coord[1]}"


def _vertices(coords) -> str:
    """A list of positions -> "x y, x y, ..."."""
    return ", ".join(_xy(c) for c in coords)


# --------------------------------------------------------------------------- #
# Attributes: DBF record -> JSON-serializable dict
# --------------------------------------------------------------------------- #
def _properties(record) -> dict:
    """DBF attributes as {field name: JSON-safe value}.

    pyshp returns Python objects that the DBF format allows but strict
    JSON does not (notably `datetime.date` for "D" fields), so every
    value passes through `_json_safe`.
    """
    return {name: _json_safe(value) for name, value in record.as_dict().items()}


def _json_safe(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()          # "2024-01-15"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float):
        return value if math.isfinite(value) else None  # NaN/Inf are not JSON
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)                     # anything unexpected, readable


# --------------------------------------------------------------------------- #
# CRS: .prj file -> identifier (or None)
# --------------------------------------------------------------------------- #
def _crs_from_prj(shp_path: Path) -> str | None:
    """Read the bundle's .prj and describe its CRS, if at all possible.

    Never falls back to EPSG:4326 — an unknown CRS stays unknown
    (None), which is honest and safe for the transformations in Phase 6.
    """
    prj = _sibling(shp_path, ".prj")
    if prj is None:
        return None

    try:
        text = prj.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None

    if _PLAIN_EPSG.match(text):
        return text.upper()

    # The outermost CRS carries the LAST authority node in a WKT string
    # (inner GEOGCS nodes come first), so the last match is the right
    # one: EPSG:32643 rather than its EPSG:4326 base.
    for pattern in (_WKT1_EPSG, _WKT2_EPSG):
        matches = pattern.findall(text)
        if matches:
            return f"EPSG:{matches[-1]}"

    # No EPSG code (ESRI-style .prj often has none): keep the WKT
    # itself rather than throwing the information away.
    if text.upper().startswith(_WKT_KEYWORDS):
        return text

    return None  # not interpretable as a CRS -> NULL


def _sibling(shp: Path, suffix: str) -> Path | None:
    """Find the file next to `shp` with `suffix`, case-insensitively.

    Archives from Windows often shout their extensions ("ROADS.SHX"),
    so matching is done on lower-cased names.
    """
    wanted = suffix.lower()
    for entry in shp.parent.iterdir():
        if (
            entry.is_file()
            and entry.suffix.lower() == wanted
            and entry.stem.lower() == shp.stem.lower()
        ):
            return entry
    return None
