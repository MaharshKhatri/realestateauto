"""Point -> ZIP (Census ZCTA) without per-point web calls.

Downloads ZCTA polygons for a bounding box once (cached on disk via PoliteSession), then does
point-in-polygon locally. Used because NJGIN parcels carry the owner's mailing ZIP, not the
property ZIP.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.http import PoliteSession

ZCTA_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
            "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query")


@dataclass
class _Zcta:
    zip: str
    rings: list[list[tuple[float, float]]]
    bbox: tuple[float, float, float, float]


def _point_in_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class ZipResolver:
    def __init__(self, http: PoliteSession | None = None):
        self.http = http or PoliteSession(min_interval=0.5, cache_ttl_hours=24 * 30)
        self._zctas: list[_Zcta] = []
        self._loaded_boxes: list[tuple[float, float, float, float]] = []

    def load_bbox(self, xmin: float, ymin: float, xmax: float, ymax: float) -> int:
        params = {
            "geometry": f"{xmin},{ymin},{xmax},{ymax}", "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "outFields": "ZCTA5",
            "returnGeometry": "true", "outSR": "4326", "maxAllowableOffset": "0.0002", "f": "json",
        }
        data = self.http.get_json(ZCTA_URL, params=params)
        seen = {z.zip for z in self._zctas}
        added = 0
        for f in data.get("features", []):
            zip_ = f["attributes"]["ZCTA5"]
            if zip_ in seen:
                continue
            rings = [[(p[0], p[1]) for p in ring] for ring in f["geometry"]["rings"]]
            xs = [p[0] for r in rings for p in r]
            ys = [p[1] for r in rings for p in r]
            self._zctas.append(_Zcta(zip_, rings, (min(xs), min(ys), max(xs), max(ys))))
            added += 1
        self._loaded_boxes.append((xmin, ymin, xmax, ymax))
        return added

    def zip_for(self, lon: float, lat: float) -> str | None:
        if not any(b[0] <= lon <= b[2] and b[1] <= lat <= b[3] for b in self._loaded_boxes):
            pad = 0.03
            self.load_bbox(lon - pad, lat - pad, lon + pad, lat + pad)
        for z in self._zctas:
            x0, y0, x1, y1 = z.bbox
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            # Even-odd rule across all rings handles holes.
            hits = sum(_point_in_ring(lon, lat, r) for r in z.rings)
            if hits % 2 == 1:
                return z.zip
        return None
