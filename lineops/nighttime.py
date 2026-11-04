"""Minutos de voo noturno a partir da trilha (elevação solar < -6°, crepúsculo civil). Aproximação astronômica simples."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def sun_elevation_deg(lat: float, lon: float, t: datetime) -> float:
    t = t.astimezone(timezone.utc)
    n = t.timetuple().tm_yday
    hours = t.hour + t.minute / 60 + t.second / 3600
    g = math.radians(360 / 365.24 * (n - 1 + (hours - 12) / 24))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = (hours * 60 + eqt + 4 * lon) % 1440
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))


def night_minutes(points: list[dict], start: str, end: str) -> int | None:
    """points: [{t, lat, lon}] ordenados. Soma o tempo (entre OFF e ON) em que o sol está abaixo de -6°. None se a trilha for insuficiente."""
    pts = [p for p in points if start <= p["t"] <= end]
    if len(pts) < 2:
        return None
    parse = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        dt = (parse(b["t"]) - parse(a["t"])).total_seconds()
        if dt <= 0 or dt > 900:            # lacuna grande na trilha: não presume
            continue
        mid = (a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2
        if sun_elevation_deg(mid[0], mid[1], parse(a["t"])) < -6:
            total += dt
    return int(round(total / 60))
