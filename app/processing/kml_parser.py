"""
KML parsing — the "Processing" layer for .kml uploads.

Responsibilities:
    1. Read a .kml file and reject anything that is not a KML document.
    2. Walk every Placemark, however deeply it is nested in
       Document/Folder containers.
    3. Extract geometry as WKT text plus the KML metadata as a dict.
    4. Skip features we cannot store, logging a clear reason.

Deliberately knows nothing about HTTP or SQLAlchemy: it returns plain
`ParsedFeature` records and the service layer decides what to persist.

Library choice: `fastkml` — a maintained, pure-Python KML reader built on
ElementTree. It hands back pygeoif geometry objects, which expose both
`geom_type` ("Point", "Polygon", "MultiPolygon", ...) and `.wkt`, so no
manual coordinate parsing and no extra geometry dependency are needed.
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from fastkml import KML, Placemark, SchemaData

from app.processing.parsed_feature import ParsedFeature
from app.processing.validation import UploadValidationError

logger = logging.getLogger(__name__)

# The KML spec pins coordinates to WGS84 lon/lat, i.e. EPSG:4326.
KML_CRS = "EPSG:4326"
KML_NS = "http://www.opengis.net/kml/2.2"

# Geometry types we store. Anything else (e.g. a mixed GeometryCollection
# inside a <MultiGeometry>) is skipped with a logged reason.
SUPPORTED_GEOMETRY_TYPES = {
    "Point",
    "LineString",
    "Polygon",
    "MultiPoint",
    "MultiLineString",
    "MultiPolygon",
}


def parse_kml(path: Path) -> list[ParsedFeature]:
    """Parse the KML file at `path` and return its features.

    Raises:
        UploadValidationError: the file is not readable XML or not a KML
            document. The API layer turns this into HTTP 400 with a clear
            message instead of a stack trace.
    """
    raw = path.read_bytes()
    root = _validate_root(raw)

    # fastkml matches elements on the OGC namespace. A file written
    # without it would silently parse to zero features, so add it back.
    if root.tag == "kml":
        root.set("xmlns", KML_NS)
        raw = ET.tostring(root, encoding="utf-8")

    try:
        kml = KML.from_string(raw)
    except Exception as exc:
        # Any failure while reading untrusted input becomes a client
        # error; the original exception stays chained for the logs.
        raise UploadValidationError(f"File could not be parsed as KML: {exc}") from exc

    features: list[ParsedFeature] = []
    for placemark in _iter_placemarks(kml):
        # The index comes from the number of features kept so far, so
        # skipped placemarks leave no gaps in the sequence.
        parsed = _parse_placemark(placemark, feature_index=len(features))
        if parsed is not None:
            features.append(parsed)
    return features


def _validate_root(raw: bytes) -> ET.Element:
    """Parse the XML and check that it really is a KML document.

    fastkml returns zero features for HTML or foreign XML rather than
    raising, which would look exactly like an empty KML file. Checking
    the root element ourselves lets us answer 400 instead of "success,
    0 features".
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UploadValidationError(f"File is not valid XML: {exc}") from exc

    # Strip a possible {namespace} prefix: "{...}kml" -> "kml".
    if root.tag.rsplit("}", 1)[-1] != "kml":
        raise UploadValidationError("File is not a KML document.")
    return root


def _iter_placemarks(node):
    """Yield every Placemark at or below `node`.

    `KML`, `Document` and `Folder` all expose a `.features` list;
    `Placemark` does not, which is what distinguishes containers from
    leaves here. Non-placemark leaves (GroundOverlay, NetworkLink, ...)
    carry no storable geometry and are ignored.
    """
    children = getattr(node, "features", None)
    if children:
        for child in children:
            yield from _iter_placemarks(child)
    elif isinstance(node, Placemark):
        yield node


def _parse_placemark(placemark: Placemark, feature_index: int) -> ParsedFeature | None:
    """Convert one Placemark into a ParsedFeature, or None if we skip it.

    Skipping (rather than failing) keeps one unusable placemark from
    rejecting the whole upload.
    """
    geometry = placemark.geometry
    if geometry is None:
        logger.warning(
            "Skipping KML placemark %r: it has no storable geometry "
            "(Model, link-only, or unsupported element).",
            placemark.name,
        )
        return None

    geometry_type = geometry.geom_type
    if geometry_type not in SUPPORTED_GEOMETRY_TYPES:
        # A mixed <MultiGeometry> becomes a GeometryCollection here;
        # per the assignment we do not decompose collections.
        logger.warning(
            "Skipping KML placemark %r: unsupported geometry type %r.",
            placemark.name,
            geometry_type,
        )
        return None

    return ParsedFeature(
        feature_index=feature_index,
        geometry_type=geometry_type,
        geometry=geometry.wkt,
        properties=_properties(placemark),
        crs=KML_CRS,  # the KML spec pins coordinates to WGS84 lon/lat
    )


def _properties(placemark: Placemark) -> dict:
    """Collect name, description and ExtendedData into a JSON-ready dict.

    Handles both ExtendedData forms:
        <Data name="owner"><value>City</value></Data>
        <SchemaData schemaUrl="#p"><SimpleData name="zone">a</SimpleData></SchemaData>
    """
    props: dict = {}

    extended = placemark.extended_data
    if extended is not None:
        for element in getattr(extended, "elements", []) or []:
            if isinstance(element, SchemaData):
                for simple in getattr(element, "data", []) or []:
                    if simple.name is not None:
                        props[simple.name] = simple.value
            elif getattr(element, "name", None) is not None:
                props[element.name] = getattr(element, "value", None)

    # Placemark-level values win over ExtendedData of the same name.
    if placemark.name is not None:
        props["name"] = placemark.name
    if placemark.description is not None:
        props["description"] = placemark.description
    return props
