"""
ParsedFeature — the common output type of every geometry parser.

Both parsers (KML in Phase 4, Shapefile in Phase 5) hand the service
layer a list of these plain dataclasses. The class lives in its own
module so neither parser has to import from the other, and so neither
one needs to know about SQLAlchemy or HTTP: the service layer decides
what to persist.
"""

from dataclasses import dataclass, field


@dataclass
class ParsedFeature:
    """One geometry extracted from an uploaded file, ready to persist.

    Attributes:
        feature_index: position among the *kept* features (0-based), so
            skipped geometries leave no gaps in the sequence.
        geometry_type: "Point", "Polygon", "LineString", ... — the same
            names GeoJSON/pygeoif use.
        geometry: the geometry itself as WKT text, e.g.
            "POLYGON ((30 10, 40 40, ...))".
        properties: source attributes as a JSON-serializable dict.
        crs: the geometry's coordinate reference system, e.g.
            "EPSG:4326". None when the source declares no CRS — we
            never guess one. Defaults to None; the KML parser passes
            "EPSG:4326" explicitly because the KML spec pins it.
    """

    feature_index: int
    geometry_type: str
    geometry: str  # WKT text
    properties: dict = field(default_factory=dict)
    crs: str | None = None
