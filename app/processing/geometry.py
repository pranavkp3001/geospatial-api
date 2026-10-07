"""Geometry measurement + CRS handling (Phase 6).

Turns a stored WKT geometry plus its recorded CRS into an area or
length expressed in metres. This module is deliberately pure and never
touches the stored data:

    ParsedFeature (WKT + geometry_type + crs)
        -> calculate_measurement()
        -> MeasurementResult (measurement_type / value / unit)

Measurement rules:
    * Polygon / MultiPolygon        -> area in "m\u00b2"
    * LineString / MultiLineString  -> length in "m"
    * Point / MultiPoint            -> no measurement (all fields null)
    * Other / unparseable geometry  -> no measurement + warning

CRS handling — the core rule of Phase 6:
    A geographic CRS such as EPSG:4326 must NEVER be measured directly
    in degrees. Those features are reprojected into the UTM zone that
    covers their centroid (EPSG:326zz north, EPSG:327zz south) and the
    measurement is taken in that projected, metre-based CRS.
    * Projected CRS whose axes are metres      -> measure in its own
      plane, no reprojection.
    * Projected CRS in other units (e.g. feet) -> no measurement, rather
      than a misleading value.
    * Missing / empty / unparseable CRS        -> no measurement.
      EPSG:4326 is NEVER assumed from silence.

The original Feature.geometry and Feature.crs are never modified — the
reprojected copy exists only for the duration of the calculation.

This module has no knowledge of HTTP, SQLAlchemy, KML, or Shapefiles:
the parsers supply WKT/type/CRS and the upload service persists the
result. Exceptions never escape: any failure degrades to a null
measurement with a logged warning, never a crash and never a bogus
number.
"""

import logging
import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer

logger = logging.getLogger(__name__)

# Geometries that can be measured and what we measure on them.
AREA_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})
LENGTH_GEOMETRY_TYPES = frozenset({"LineString", "MultiLineString"})
# Geometries that are fine but have no extent worth measuring; handled
# silently (a Point measuring "area of 0 m2" would just be noise).
NO_MEASUREMENT_TYPES = frozenset({"Point", "MultiPoint"})

AREA_UNIT = "m\u00b2"  # "m²"
LENGTH_UNIT = "m"

# pyproj reports the unit of measurement metre under these spellings.
_METER_UNITS = frozenset({"metre", "meter", "m"})

# UTM zone formula: zone = floor((lon + 180) / 6) + 1, then add the
# northern/southern hemisphere EPSG base.
_UTM_NORTH_BASE = 32600
_UTM_SOUTH_BASE = 32700


@dataclass
class MeasurementResult:
    """Plain result of a measurement attempt.

    All fields default to None; a non-measurable input therefore comes
    back as a fully-null result without any special-casing.
    """

    measurement_type: str | None = None
    measurement_value: float | None = None
    measurement_unit: str | None = None


def calculate_measurement(
    geometry_wkt: str,
    geometry_type: str,
    crs: str | None,
) -> MeasurementResult:
    """Measure `geometry_wkt` in metres, honouring its recorded CRS.

    Args:
        geometry_wkt:  WKT text of the stored geometry.
        geometry_type: The geometry's type as recorded alongside the
                       stored WKT ("Polygon", "MultiPolygon",
                       "LineString", "MultiLineString", "Point", ...).
        crs:           The recorded CRS (e.g. "EPSG:4326", "EPSG:32643",
                       a .prj WKT string) or None when unknown.

    Returns:
        A MeasurementResult. `measurement_type` is "area" or "length"
        with the value in m\u00b2 / m when a measurement is possible and
        reliable; otherwise every field is None. Never raises.
    """
    kind = _measurement_kind(geometry_type)
    if kind is None:
        if geometry_type not in NO_MEASUREMENT_TYPES:
            logger.warning(
                "Measurement skipped: unsupported geometry type %r "
                "(not an area or length type).",
                geometry_type,
            )
        return MeasurementResult()

    if not geometry_wkt or not str(geometry_wkt).strip():
        logger.warning(
            "Measurement skipped: %r geometry has no WKT to measure.", geometry_type
        )
        return MeasurementResult()

    geom = _parse_geometry(geometry_wkt)
    if geom is None:
        return MeasurementResult()

    if geom.geom_type != geometry_type:
        logger.warning(
            "Measurement skipped: recorded type %r does not match parsed "
            "geometry %r; refusing to guess.",
            geometry_type,
            geom.geom_type,
        )
        return MeasurementResult()

    if geom.is_empty:
        logger.warning(
            "Measurement skipped: %r geometry is empty (nothing to measure).",
            geometry_type,
        )
        return MeasurementResult()

    target_crs, transformer = _measurement_crs_and_transformer(geom, crs)
    if target_crs is None:
        return MeasurementResult()

    try:
        if transformer is not None:
            # Reproject a working copy for the calculation only. The
            # stored WKT is untouched (test N guarantees this).
            working = _reproject(geom, transformer)
        else:
            working = geom
        value = working.area if kind == "area" else working.length
    except Exception as exc:  # projection/math failures degrade, never raise
        logger.warning(
            "Measurement skipped: could not compute %s for %r geometry: %s.",
            kind,
            geometry_type,
            exc,
        )
        return MeasurementResult()

    if not math.isfinite(value):
        logger.warning(
            "Measurement skipped: non-finite %s value (%r) for %r geometry.",
            kind,
            value,
            geometry_type,
        )
        return MeasurementResult()

    return MeasurementResult(
        measurement_type=kind,
        measurement_value=value,
        measurement_unit=AREA_UNIT if kind == "area" else LENGTH_UNIT,
    )


def utm_epsg_for(longitude: float, latitude: float) -> int | None:
    """Choose the UTM EPSG code covering a lon/lat position.

        zone = floor((longitude + 180) / 6) + 1
        northern hemisphere -> 32600 + zone (e.g. Bangalore ~77 E
                               -> zone 43N -> EPSG:32643)
        southern hemisphere -> 32700 + zone (e.g. Sydney ~151 E
                               -> zone 56S -> EPSG:32756)

    The zone is clamped to [1, 60] so the ±180° meridian maps onto a
    real zone instead of 61. Returns None for non-finite input, so
    callers always have a sentinel to handle instead of an exception.
    """
    if not (math.isfinite(longitude) and math.isfinite(latitude)):
        return None
    zone = math.floor((longitude + 180) / 6) + 1
    zone = min(max(zone, 1), 60)
    return (_UTM_NORTH_BASE if latitude >= 0 else _UTM_SOUTH_BASE) + zone


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _measurement_kind(geometry_type: str) -> str | None:
    """'area', 'length', or None when the type takes no measurement."""
    if geometry_type in AREA_GEOMETRY_TYPES:
        return "area"
    if geometry_type in LENGTH_GEOMETRY_TYPES:
        return "length"
    return None


def _reproject(geometry, transformer: Transformer) -> "shapely.Geometry":
    """Reproject a working copy of `geometry`; the original stays untouched.

    shapely.transform_coordseq hands its callback a single (N, 2)
    coordinate array, while pyproj's Transformer.transform wants x and y
    as separate arguments — the `_xy` closure bridges the two calling
    shapes (the transform never sees the stored geometry itself).
    """
    def _xy(coords):
        x, y = coords[:, 0], coords[:, 1]
        new_x, new_y = transformer.transform(x, y)
        return np.column_stack((new_x, new_y))

    return shapely.transform_coordseq(geometry, _xy)


def _parse_geometry(geometry_wkt: str):
    """Shapely geometry, or None (with a warning) if the WKT is bad."""
    try:
        return shapely.wkt.loads(geometry_wkt)
    except Exception as exc:
        logger.warning("Measurement skipped: cannot parse WKT %r: %s.", geometry_wkt, exc)
        return None


@lru_cache(maxsize=128)
def _parse_crs(value: str) -> CRS | None:
    """pyproj CRS, or None (with a warning) if the string is unusable."""
    try:
        return CRS.from_user_input(value)
    except Exception:
        logger.warning("Measurement skipped: CRS %r could not be interpreted.", value)
        return None


def _is_metre_based(crs: CRS) -> bool:
    """True when every axis of `crs` is expressed in metres.

    Anything else (feet, US survey foot, yards, ...) fails this check so
    we refuse to hand back a value that claims to be "m²" but is not.
    """
    units = {axis.unit_name.lower() for axis in crs.axis_info}
    return bool(units) and units.issubset(_METER_UNITS)


def _measurement_crs_and_transformer(
    geom: "shapely.Geometry", crs: str | None
) -> tuple[CRS | None, Transformer | None]:
    """Decide the CRS to measure in.

    Returns (target_crs, transformer):
        * (crs, None)        -> geometry already lives in target_crs,
                                measure it directly.
        * (crs, transformer) -> reproject a working copy first.
        * (None, None)       -> no reliable metre-based measurement is
                                possible; the reason is logged.
    """
    if crs is None or not str(crs).strip():
        logger.warning(
            "Measurement skipped: feature has no CRS recorded "
            "(missing CRS is never assumed to be EPSG:4326)."
        )
        return None, None

    source = _parse_crs(str(crs))
    if source is None:
        return None, None

    if source.is_projected:
        if not _is_metre_based(source):
            units = sorted({a.unit_name for a in source.axis_info})
            logger.warning(
                "Measurement skipped: projected CRS %r is not metre-based "
                "(axes in %s); refusing to claim metre measurements.",
                crs,
                units,
            )
            return None, None
        # Already projected and in metres: measure in the native plane.
        return source, None

    if source.is_geographic:
        try:
            lon, lat = geom.centroid.x, geom.centroid.y
            epsg = utm_epsg_for(lon, lat)
            if epsg is None:
                raise ValueError(f"non-finite centroid ({lon!r}, {lat!r})")
        except Exception as exc:
            logger.warning(
                "Measurement skipped: cannot derive a UTM zone from the "
                "centroid of the geometry with CRS %r: %s.",
                crs,
                exc,
            )
            return None, None

        target = _parse_crs(f"EPSG:{epsg}")
        if target is None:
            return None, None
        try:
            transformer = Transformer.from_crs(source, target, always_xy=True)
        except Exception as exc:
            logger.warning(
                "Measurement skipped: cannot build CRS transformation "
                "%r -> EPSG:%s: %s.",
                crs,
                epsg,
                exc,
            )
            return None, None
        return target, transformer

    logger.warning(
        "Measurement skipped: CRS %r is neither projected nor geographic.",
        crs,
    )
    return None, None