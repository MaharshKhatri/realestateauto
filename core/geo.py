"""Geo helpers."""
from __future__ import annotations

import math


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ring_centroid(rings: list[list[list[float]]]) -> tuple[float, float] | None:
    """Rough centroid (lon, lat) of the first polygon ring (vertex average; fine for parcels)."""
    if not rings or not rings[0]:
        return None
    pts = rings[0]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
