"""Geometria obszaru: obrys włączony, dziury wraz z brzegiem wyłączone."""

import math


def normalize_geometry(value, *, legacy_energa=False):
    """Sprawdza GeoJSON; stary format ENERGA dopuszcza brak typu i domknięcia."""
    if not isinstance(value, dict):
        raise ValueError("Niepoprawna geometria: oczekiwano obiektu")
    coordinates = value.get("coordinates")
    kind = value.get("type")
    legacy = legacy_energa and "type" not in value
    if legacy and isinstance(coordinates, list) and coordinates:
        first = coordinates[0]
        if isinstance(first, list) and first:
            nested_ring = isinstance(first[0], list) and first[0] and isinstance(first[0][0], list)
            kind = "MultiPolygon" if nested_ring else "Polygon"
    if not isinstance(kind, str) or kind not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Niepoprawna geometria: wymagany Polygon lub MultiPolygon")
    polygons = [coordinates] if kind == "Polygon" else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("Niepoprawna geometria: brak wielokątów")
    normalized = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise ValueError("Niepoprawna geometria: brak obrysu")
        rings = []
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 3:
                raise ValueError("Niepoprawna geometria: zbyt krótki pierścień")
            points = []
            for point in ring:
                if not isinstance(point, list) or len(point) not in (2, 3):
                    raise ValueError("Niepoprawna geometria: błędny punkt")
                if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in point):
                    raise ValueError("Niepoprawna geometria: współrzędne muszą być skończonymi liczbami")
                lon, lat = point[:2]
                if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise ValueError("Niepoprawna geometria: współrzędne poza zakresem")
                points.append([lon, lat])
            if points[0] != points[-1]:
                if not legacy:
                    raise ValueError("Niepoprawna geometria: niedomknięty pierścień")
                points.append(points[0][:])
            if len(points) < 4 or len({tuple(p) for p in points[:-1]}) < 3:
                raise ValueError("Niepoprawna geometria: mniej niż trzy różne punkty")
            origin_x, origin_y = points[0]
            area = sum((a[0] - origin_x) * (b[1] - origin_y) - (b[0] - origin_x) * (a[1] - origin_y)
                       for a, b in zip(points, points[1:]))
            if area == 0:
                raise ValueError("Niepoprawna geometria: zerowe pole pierścienia")
            rings.append(points)
        normalized.append(rings)
    return {"type": kind, "coordinates": normalized[0] if kind == "Polygon" else normalized}


def ring_position(latitude, longitude, ring):
    """Zwraca -1 poza pierścieniem, 0 na brzegu, 1 wewnątrz."""
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        dx, dy = x2 - x1, y2 - y1
        cross = (longitude - x1) * dy - (latitude - y1) * dx
        tolerance = 1e-10
        if (abs(cross) <= tolerance * max(abs(dx), abs(dy), tolerance)
                and min(x1, x2) - tolerance <= longitude <= max(x1, x2) + tolerance
                and min(y1, y2) - tolerance <= latitude <= max(y1, y2) + tolerance):
            return 0
        if (y1 > latitude) != (y2 > latitude):
            crossing = dx * (latitude - y1) / dy + x1
            if longitude < crossing:
                inside = not inside
        previous = current
    return 1 if inside else -1


def geometry_contains(latitude, longitude, geometry):
    """Działa na geometrii sprawdzonej podczas normalizacji źródła."""
    return geometry_decision(latitude, longitude, geometry)["contains"]


def geometry_decision(latitude, longitude, geometry):
    """Wynik całej geometrii z uwzględnieniem dziur i innych części."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    excluded = None
    for index, polygon in enumerate(polygons):
        outer = ring_position(latitude, longitude, polygon[0])
        if outer < 0:
            continue
        holes = [ring_position(latitude, longitude, hole) for hole in polygon[1:]]
        if any(value >= 0 for value in holes):
            excluded = excluded or {"contains": False, "result": "hole_boundary" if 0 in holes else "hole", "polygon_index": index}
        else:
            return {"contains": True, "result": "outer_boundary" if outer == 0 else "inside", "polygon_index": index}
    return excluded or {"contains": False, "result": "outside", "polygon_index": None}
