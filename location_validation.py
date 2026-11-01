"""Wspólna walidacja współrzędnych zapisanych obiektów."""

import math
from address_matching import house_number
from power_supply import normalize_power_supply


def coordinate(value, field):
    """Zwraca float/None albo zgłasza czytelny błąd konkretnego pola."""
    label = "szerokość geograficzna" if field == "latitude" else "długość geograficzna"
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"Nieprawidłowa {label}")
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Nieprawidłowa {label}") from error
    limit = 90 if field == "latitude" else 180
    if not math.isfinite(result) or not -limit <= result <= limit:
        raise ValueError(f"Nieprawidłowa {label}")
    return result


def coordinates(latitude, longitude):
    latitude = coordinate(latitude, "latitude")
    longitude = coordinate(longitude, "longitude")
    if (latitude is None) != (longitude is None):
        raise ValueError("Podaj jednocześnie szerokość i długość geograficzną")
    return latitude, longitude


def validate_object_list(items):
    """Waliduje listę bez modyfikowania wejścia; zwraca te same ID i GPS."""
    if not isinstance(items, list):
        raise TypeError("Lista obiektów musi być tablicą JSON")
    seen = set()
    for index, item in enumerate(items, start=1):
        if isinstance(item, str):
            object_id = item.strip()
            city_sym = object_id
            latitude = longitude = None
        elif isinstance(item, dict):
            city_sym = str(item.get("city_sym") or "").strip()
            object_id = str(item.get("id") or city_sym).strip()
            latitude = item.get("latitude")
            longitude = item.get("longitude")
        else:
            raise TypeError(f"Obiekt #{index}: wymagany obiekt JSON albo kod TERYT")
        label = object_id or f"#{index}"
        if not city_sym.isdigit():
            raise ValueError(f"Obiekt {label}: nieprawidłowy kod miejscowości (city_sym)")
        if not object_id:
            raise ValueError(f"Obiekt #{index}: brak identyfikatora (id)")
        if object_id in seen:
            raise ValueError(f"Obiekt {object_id}: powtórzony identyfikator")
        seen.add(object_id)
        try:
            coordinates(latitude, longitude)
            if isinstance(item, dict):
                house_number(item.get("house_number"))
                if "power_supply" in item:
                    normalize_power_supply(item["power_supply"])
        except ValueError as error:
            raise ValueError(f"Obiekt {label}: {error}") from error
    return True
