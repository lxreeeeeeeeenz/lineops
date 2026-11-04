"""Geometria pura. Aeroportos, fusos e famílias de tipo não moram aqui: vêm do AeroScale (ver lineops/rules.py)."""
from __future__ import annotations

import math


def nm(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def nearest_airport(apmap: dict, lat: float, lon: float, max_nm: float = 15.0) -> str | None:
    """Mesma busca do ACARS do AeroScale (acars._nearest_airport), com o mapa de aeroportos dele."""
    best, bd = None, max_nm
    for icao, a in apmap.items():
        if a.get("lat") is None:
            continue
        if abs(a["lat"] - lat) > 1.0 or abs(a["lon"] - lon) > 1.5:
            continue
        d = nm(lat, lon, a["lat"], a["lon"])
        if d < bd:
            best, bd = icao, d
    return best
