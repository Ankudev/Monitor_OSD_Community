"""Deklarowane wyposażenie obiektu, bez oceny jego aktualnego działania."""

POWER_VALUES = frozenset({"unknown", "none", "manual", "szr"})
DEFAULT_POWER_SUPPLY = {"generator": "unknown", "secondary_feed": "unknown", "notes": ""}


def normalize_power_supply(value):
    if not isinstance(value, dict):
        raise ValueError("Zasilanie rezerwowe musi być obiektem JSON")
    unknown = set(value) - set(DEFAULT_POWER_SUPPLY)
    if unknown:
        raise ValueError("Nieznane pola zasilania rezerwowego: " + ", ".join(sorted(unknown)))
    result = dict(DEFAULT_POWER_SUPPLY, **value)
    for field in ("generator", "secondary_feed"):
        if not isinstance(result[field], str) or result[field] not in POWER_VALUES:
            raise ValueError(f"Zasilanie rezerwowe: nieprawidłowa wartość {field}")
    if not isinstance(result["notes"], str) or len(result["notes"]) > 1000:
        raise ValueError("Uwagi dotyczące zasilania muszą być tekstem o długości do 1000 znaków")
    result["notes"] = result["notes"].strip()
    return result


def merge_power_supply(current, changes):
    # Walidacja częściowego PATCH przed scaleniem; pominięte pola pozostają.
    normalize_power_supply(changes)
    return normalize_power_supply(dict(normalize_power_supply(current), **changes))
