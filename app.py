"""Społecznościowy monitor awarii pięciu głównych OSD."""

import sys
if __name__ == '__main__':
    sys.modules['app'] = sys.modules[__name__]

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import re
import sqlite3
import tempfile
import time
import unicodedata
import uuid
from contextlib import closing, asynccontextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from api_v1 import register_api_v1
from service_backup import create_snapshot, private_files, DATABASES
from runtime_health import JobHealth, Readiness, RequestMetrics, OperationsToken, backup_health
import admin_auth
import public_origin
import publication_policy as publication
from public_projection import public_data
import community
from scan_diagnostics import SourceMaintenanceError
from scan_diagnostics import Diagnostics, measured, fingerprint, CURRENT, source_trace, PUBLICATION_FIELDS
from catalog_quality import QualityCache
from catalog_journal import CatalogJournal, fingerprint as catalog_revision
from address_matching import house_number, address_decision, public_address_audit, address_verified
from power_supply import normalize_power_supply, merge_power_supply
from event_lifecycle import reconcile
from engine_store import EngineStore, EngineStoreError, StoreWriteResult, history_record, SCHEMA_VERSION
from notification_queue import NotificationWorker, make_job, destination_id, validate_url
from event_history import (parse_event_datetime, event_time_windows, event_time_phase,
                           first_event_time_value, EVENT_START_FIELDS, EVENT_STOP_FIELDS,
                           event_time_warnings)
from geometry import geometry_contains, ring_position
from matching_explanation import explain, explanation_details, evidence_signature, source_signature
from geometry import geometry_decision
from matching_evidence import geometry_reference, matching_evidence, match_details, point_confirmed, source_map_geometry, public_location_audit
from geocoder import (
    Geocoder,
    GeocoderBusyError,
    GeocoderCacheError,
    GeocoderError,
    GeocoderNotFoundError,
    GeocoderProviderError,
    GeocoderRateLimitedError,
    validate_provider_url,
)
from location_validation import coordinates as validated_coordinates, validate_object_list
from geoportal import Geoportal
from location_review import ReviewStore, expected_commune
from osd_sources import (
    DEFAULT_SOURCE_INTERVALS,
    DEFAULT_SOURCES,
    SOURCE_LABELS,
    SourceHttpError,
    fetch_source,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
OBJECTS_PATH = BASE_DIR / "objects.json"
TERYT_DB_PATH = BASE_DIR / "data" / "teryt.db"
WEB_DIR = BASE_DIR / "web"
SNUT_DIR = BASE_DIR / "snut-current"

VOIVODESHIP_CODES = {
    "dolnoslaskie": "02",
    "kujawsko-pomorskie": "04",
    "lubelskie": "06",
    "lubuskie": "08",
    "lodzkie": "10",
    "malopolskie": "12",
    "mazowieckie": "14",
    "opolskie": "16",
    "podkarpackie": "18",
    "podlaskie": "20",
    "pomorskie": "22",
    "slaskie": "24",
    "swietokrzyskie": "26",
    "warminsko-mazurskie": "28",
    "wielkopolskie": "30",
    "zachodniopomorskie": "32",
}

CONFIG_KEY = web.AppKey("config", dict)
CONFIG_PATH_KEY = web.AppKey("config_path", Path)
STATE_PATH_KEY = web.AppKey("state_path", Path)
ENGINE_STORE_KEY = web.AppKey("engine_store", EngineStore)
DIAGNOSTICS_KEY = web.AppKey("diagnostics", Diagnostics)
QUALITY_KEY = web.AppKey("quality", QualityCache)
STORAGE_STATUS_KEY = web.AppKey("storage_status", dict)
OBJECTS_PATH_KEY = web.AppKey("objects_path", Path)
CATALOG_JOURNAL_KEY = web.AppKey("catalog_journal", CatalogJournal)
TERYT_DB_PATH_KEY = web.AppKey("teryt_db_path", Path)
SNUT_DIR_KEY = web.AppKey("snut_dir", Path)
STATE_KEY = web.AppKey("state", dict)
START_SCANNER_KEY = web.AppKey("start_scanner", bool)
SCAN_LOCK_KEY = web.AppKey("scan_lock", asyncio.Lock)
BACKUP_LOCK_KEY = web.AppKey("backup_lock", asyncio.Lock)
JOBS_KEY = web.AppKey("jobs_health", dict)
READINESS_KEY = web.AppKey("local_readiness", object)
METRICS_KEY = web.AppKey("request_metrics", object)
OPERATIONS_TOKEN_KEY = web.AppKey("operations_token", object)
BACKUP_TOKEN_KEY = web.AppKey("backup_token", object)
SOURCE_LOCKS_KEY = web.AppKey("source_locks", dict)
PGE_MIN_INTERVAL_SECONDS = 3600
PGE_MAX_ERROR_INTERVAL_SECONDS = 21600
OBJECTS_LOCK_KEY = web.AppKey("objects_lock", asyncio.Lock)
RUNTIME_KEY = web.AppKey("runtime", dict)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
GEOCODER_KEY = web.AppKey("geocoder", Geocoder)
GEOPORTAL_KEY = web.AppKey("geoportal", Geoportal)
REVIEW_STORE_KEY = web.AppKey("location_review_store", ReviewStore)
SCANNER_TASK_KEY = web.AppKey("scanner_task", asyncio.Task)
NOTIFICATION_WORKER_KEY = web.AppKey("notification_worker", NotificationWorker)
SOURCE_FETCH_TIMEOUT_SECONDS = 180
MAX_CATALOG_MATCH_ATTEMPTS = 3
LEGACY_CATALOG_CHANGE_ERROR = "RuntimeError: Lista obiektów zmieniła się podczas skanu. Ponowię odczyt."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("osd-monitor")

LOCAL_TIMEZONE = ZoneInfo("Europe/Warsaw")
MATCHING_VERSION = 4
ENEA_MATCHING_VERSION = 4
ENERGA_MATCHING_VERSION = 3
ALLOWED_MAPS_HOSTS = {
    "google.com",
    "www.google.com",
    "maps.google.com",
    "google.pl",
    "www.google.pl",
    "maps.google.pl",
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default):
    if not path.exists():
        return deepcopy(default)
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    # Przerwany zapis nie może zostawić połowy state.json.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def optional_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in text if not unicodedata.combining(character)).lower()


def coordinates_from_text(value):
    text = unquote(value.strip())
    patterns = (
        r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$",
        r"^\s*(-?\d+,\d+)\s+(-?\d+,\d+)\s*$",
        r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)",
        r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)",
        r"(?:center|ll)=(-?\d+(?:\.\d+)?)(?:%2C|,)(-?\d+(?:\.\d+)?)",
        r"search/(-?\d+(?:\.\d+)?),\s*\+?(-?\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        latitude = float(match.group(1).replace(",", "."))
        longitude = float(match.group(2).replace(",", "."))
        if (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
        ):
            return latitude, longitude
    return None


def has_control_character(value):
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def looks_like_url(value):
    text = value.strip()
    return bool(
        re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE)
        or re.match(
            r"^(?:www\.|maps\.|google\.|goo\.|maps\.app\.|[a-z0-9.-]+\.[a-z]{2,})(?:[/:?#]|$)",
            text,
            re.IGNORECASE,
        )
    )


def coordinates_from_maps_url(value):
    text = value.strip()
    if has_control_character(text):
        raise ValueError("Link zawiera niedozwolone znaki")
    parsed = urlsplit(text if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE) else f"https://{text}")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or hostname not in ALLOWED_MAPS_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError("Wklej pełny odnośnik do Google Maps ze współrzędnymi albo wpisz same współrzędne")
    coordinates = coordinates_from_text(f"{parsed.path}\n{parsed.query}")
    if coordinates:
        return coordinates
    query = parse_qs(parsed.query, keep_blank_values=True)
    for field in ("q", "ll"):
        for item in query.get(field, []):
            coordinates = coordinates_from_text(item)
            if coordinates:
                return coordinates
    raise ValueError("Odnośnik do Google Maps nie zawiera jednoznacznych współrzędnych")


def public_error_message(error):
    name = type(error).__name__
    text = str(error).strip()
    if not text and getattr(error, "__cause__", None) is not None:
        text = public_error_message(error.__cause__)
    return f"{name}: {text}" if text and text != name else name


def voivodeship_code(address):
    iso_code = str(address.get("ISO3166-2-lvl4") or "")
    match = re.search(r"PL-(\d{2})", iso_code, re.IGNORECASE)
    if match:
        return match.group(1)

    state = normalized_text(address.get("state"))
    for name, code in VOIVODESHIP_CODES.items():
        if name in state:
            return code
    return ""


class TerytResolutionError(ValueError):
    """Serwis OpenStreetMap odpowiedział, ale nie udało się jednoznacznie wybrać miejscowości w rejestrze TERYT."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code
        self.osm_result = None


def teryt_from_address(database_path, address):
    city_query = next(
        (
            str(address.get(field) or "").strip()
            for field in ("city", "town", "village", "municipality", "hamlet")
            if str(address.get(field) or "").strip()
        ),
        "",
    )
    if not city_query:
        raise TerytResolutionError("Nie rozpoznano miejscowości dla wskazanego punktu", "missing_city")

    province_code = voivodeship_code(address)
    with closing(open_teryt_database(database_path)) as connection:
        query = "SELECT sym, nazwa, woj, pow, gmi FROM simc WHERE nazwa = ? COLLATE NOCASE"
        parameters = [city_query]
        if province_code:
            query += " AND woj = ?"
            parameters.append(province_code)
        query += " ORDER BY sym LIMIT 2"
        cities = connection.execute(query, parameters).fetchall()
        if len(cities) > 1:
            raise TerytResolutionError(f"Niejednoznaczna miejscowość: {city_query}. Wybierz kod TERYT ręcznie.", "ambiguous_city")
        city = cities[0] if cities else None

        if city is None:
            query = "SELECT sym, nazwa, woj, pow, gmi FROM simc WHERE nazwa LIKE ? COLLATE NOCASE"
            parameters = [f"{city_query}%"]
            if province_code:
                query += " AND woj = ?"
                parameters.append(province_code)
            query += " ORDER BY sym LIMIT 2"
            cities = connection.execute(query, parameters).fetchall()
            if len(cities) > 1:
                raise TerytResolutionError(f"Niejednoznaczna miejscowość: {city_query}. Wybierz kod TERYT ręcznie.", "ambiguous_city")
            city = cities[0] if cities else None

        if city is None:
            raise TerytResolutionError(f"Nie znaleziono miejscowości w rejestrze TERYT: {city_query}", "city_not_found")

        street_query = str(address.get("road") or "").strip()
        street = None
        if street_query:
            clean_street = re.sub(
                r"^(ulica|ul\.|aleja|al\.|plac|pl\.|rondo)\s+",
                "",
                street_query,
                flags=re.IGNORECASE,
            ).strip()
            prefix = re.match(r"^(ulica|ul\.|aleja|al\.|plac|pl\.|rondo)\s+", street_query, re.IGNORECASE)
            features = {"ulica": "ul.", "ul.": "ul.", "aleja": "al.", "al.": "al.",
                        "plac": "pl.", "pl.": "pl.", "rondo": "rondo"}
            feature = features.get(prefix[1].lower()) if prefix else None
            feature_sql = " AND cecha = ? COLLATE NOCASE" if feature else ""
            feature_args = [feature] if feature else []
            street_sql = "SELECT sym_ul, cecha, nazwa_1, nazwa_2 FROM ulic WHERE sym = ? AND "
            streets = connection.execute(
                street_sql + "(nazwa_1 = ? COLLATE NOCASE OR nazwa_2 = ? COLLATE NOCASE)"
                + feature_sql + " GROUP BY sym_ul ORDER BY sym_ul LIMIT 2",
                [str(city["sym"]), clean_street, clean_street, *feature_args],
            ).fetchall()
            if not streets:
                literal = clean_street.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                streets = connection.execute(
                    street_sql + "(nazwa_1 LIKE ? ESCAPE '\\' COLLATE NOCASE OR nazwa_2 LIKE ? ESCAPE '\\' COLLATE NOCASE)"
                    + feature_sql + " GROUP BY sym_ul ORDER BY sym_ul LIMIT 2",
                    [str(city["sym"]), f"%{literal}%", f"%{literal}%", *feature_args],
                ).fetchall()
            if len(streets) > 1:
                raise TerytResolutionError(f"Niejednoznaczna ulica: {street_query}. Wybierz kod ULIC ręcznie.", "ambiguous_street")
            street = streets[0] if streets else None

    street_name = ""
    street_sym = ""
    if street is not None:
        street_sym = str(street["sym_ul"])
        street_name = " ".join(
            part
            for part in (
                str(street["cecha"] or "").strip(),
                str(street["nazwa_1"] or "").strip(),
                str(street["nazwa_2"] or "").strip(),
            )
            if part
        )

    return {
        "city_sym": str(city["sym"]),
        "city": str(city["nazwa"]),
        "street_sym": street_sym,
        "street": street_name,
        "source_street": street_query,
        "voivodeship_code": str(city["woj"] or ""),
        "county_code": str(city["pow"] or ""),
        "commune_code": str(city["gmi"] or ""),
        "warning": "" if not street_query or street is not None else f"Nie znaleziono ulicy ULIC: {street_query}",
    }


def normalize_locations(locations):
    validate_object_list(locations)

    normalized = []
    seen_ids = set()
    for index, item in enumerate(locations, start=1):
        if isinstance(item, str):
            city_sym = item.strip()
            raw_item = {}
        elif isinstance(item, dict):
            city_sym = str(item.get("city_sym") or "").strip()
            raw_item = item
        else:  # validate_object_list podało już czytelny błąd
            raise AssertionError("Nieosiągalny typ obiektu")

        street_sym = str(raw_item.get("street_sym") or "").strip()
        object_id = str(raw_item.get("id") or city_sym).strip()
        seen_ids.add(object_id)

        latitude, longitude = validated_coordinates(
            raw_item.get("latitude"), raw_item.get("longitude")
        )

        normalized.append(
            {
                "id": object_id,
                "name": str(raw_item.get("name") or "").strip(),
                "keyword": str(raw_item.get("keyword") or "").strip(),
                "city_sym": city_sym,
                "street_sym": street_sym,
                "latitude": latitude,
                "longitude": longitude,
            }
        )
        if isinstance(raw_item.get("location_audit"), dict):
            normalized[-1]["location_audit"] = deepcopy(raw_item["location_audit"])
        if "house_number" in raw_item:
            normalized[-1]["house_number"] = house_number(raw_item["house_number"])
        if isinstance(raw_item.get("address_audit"), dict):
            normalized[-1]["address_audit"] = deepcopy(raw_item["address_audit"])
        if "power_supply" in raw_item:
            normalized[-1]["power_supply"] = normalize_power_supply(raw_item["power_supply"])
    return normalized


def load_objects(path, fallback):
    if path.exists():
        return normalize_locations(load_json(path, []))
    return normalize_locations(fallback)


def load_config(path=CONFIG_PATH):
    config = load_json(path, {})
    if not isinstance(config, dict):
        raise TypeError("config.json musi zawierać obiekt JSON")

    locations = config.get("teryt_locations", [])
    if not isinstance(locations, list):
        raise TypeError("teryt_locations musi być listą")

    normalized_locations = normalize_locations(locations)

    enabled_sources = config.get("enabled_sources", list(DEFAULT_SOURCES))
    if not isinstance(enabled_sources, list):
        raise TypeError("enabled_sources musi być listą")
    enabled_sources = [
        str(source).strip().lower()
        for source in enabled_sources
        if str(source).strip().lower() in SOURCE_LABELS
    ]
    enabled_sources = list(dict.fromkeys(enabled_sources))

    configured_intervals = config.get("source_scan_intervals_seconds", {})
    if not isinstance(configured_intervals, dict):
        raise TypeError("source_scan_intervals_seconds musi być obiektem")
    source_intervals = {
        source: max(
            PGE_MIN_INTERVAL_SECONDS if source == "pge" else 60,
            int(configured_intervals.get(source, DEFAULT_SOURCE_INTERVALS[source])),
        )
        for source in enabled_sources
    }

    enea_regions = config.get("enea_regions", [])
    if not isinstance(enea_regions, list):
        raise TypeError("enea_regions musi być listą")
    enea_regions = sorted(
        {
            int(value)
            for value in enea_regions
            if str(value).isdigit() and 1 <= int(value) <= 32 and int(value) != 14
        }
    )

    if type(config.get('notifications_enabled', False)) is not bool:
        raise ValueError('notifications_enabled musi być wartością true/false')
    notification_url = str(config.get('our_api_url') or '').strip()
    if notification_url:
        validate_url(notification_url)
    pilot = config.get('notification_object_ids', [])
    if not isinstance(pilot, list) or any(not isinstance(x, str) or not x for x in pilot):
        raise ValueError('notification_object_ids musi być listą ID obiektów')
    return {
        "profile": community.profile(config.get("profile", "local")),
        "notifications_enabled": config.get('notifications_enabled', False),
        "notification_object_ids": list(pilot),
        "scan_interval_seconds": max(30, int(config.get("scan_interval_seconds", 300))),
        "geoportal_enabled": config.get("geoportal_enabled", True) is True,
        "request_timeout_seconds": max(5, int(config.get("request_timeout_seconds", 30))),
        "web_host": str(config.get("web_host") or "127.0.0.1"),
        "web_port": int(config.get("web_port", 8080)),
        "our_api_url": str(config.get("our_api_url") or "").strip(),
        "geocoding_provider_url": validate_provider_url(
            config.get("geocoding_provider_url") or "https://nominatim.openstreetmap.org"
        ),
        "geocoding_user_agent": str(
            config.get("geocoding_user_agent")
            or "OSD-Monitor-LITE/2.0 (+https://osd-demo.bieda.it/readme)"
        ).strip(),
        "geocoding_timeout_seconds": max(1, int(config.get("geocoding_timeout_seconds", 15))),
        "geocoding_success_ttl_seconds": max(
            1, int(config.get("geocoding_success_ttl_seconds", 2592000))
        ),
        "geocoding_empty_ttl_seconds": max(
            1, int(config.get("geocoding_empty_ttl_seconds", 86400))
        ),
        "geocoding_cache_max_entries": max(
            1, int(config.get("geocoding_cache_max_entries", 10000))
        ),
        "geocoding_max_pending": max(1, int(config.get("geocoding_max_pending", 20))),
        "enabled_sources": enabled_sources,
        "source_scan_intervals_seconds": source_intervals,
        "spatial_match_radius_m": max(
            100, int(config.get("spatial_match_radius_m", 1000))
        ),
        "enea_regions": enea_regions,
        "teryt_locations": normalized_locations,
    }


def event_type(raw):
    boolean_fields = ("massOutage", "isMassOutage", "is_mass_outage")
    for field in boolean_fields:
        if raw.get(field) is True:
            return "mass_outage"

    mass_labels = {"mass_outage", "mass-outage", "mass outage"}
    for field in ("eventType", "outageType", "typeName", "category", "type"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip().lower() in mass_labels:
            return "mass_outage"

    value = raw.get("type")
    if value == 1 or str(value).strip() == "1":
        return "outage"
    if value == 2 or str(value).strip() == "2":
        return "planned_outage"
    return "unknown"


def event_hash(raw):
    text = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def matching_addresses(
    raw,
    city_sym,
    street_sym="",
    keyword="",
):
    city_matches = []
    addresses = raw.get("addresses")
    if not isinstance(addresses, list):
        return city_matches

    for address in addresses:
        if not isinstance(address, dict):
            continue
        teryt = address.get("teryt")
        if not isinstance(teryt, dict):
            continue
        if str(teryt.get("citySym") or "").strip() != city_sym:
            continue
        city_matches.append(deepcopy(address))

    if street_sym:
        street_matches = [
            address
            for address in city_matches
            if str((address.get("teryt") or {}).get("streetSymUl") or "").strip()
            == street_sym
        ]
        if street_matches:
            return street_matches

    keywords = [part.strip().lower() for part in keyword.split(",") if part.strip()]
    if keywords:
        description = str(raw.get("description") or "").lower()
        keyword_matches = []
        for address in city_matches:
            searchable = json.dumps(address, ensure_ascii=False).lower()
            if any(part in searchable or part in description for part in keywords):
                keyword_matches.append(address)
        return keyword_matches

    return city_matches if not street_sym else []


def build_location_profiles(locations, database_path):
    """Buduje tekstowy profil obiektu bez zmieniania objects.json."""

    profiles = {}
    if not locations:
        return profiles
    city_syms = sorted({str(item["city_sym"]) for item in locations})
    city_names = {}
    street_names = {}
    if database_path.exists():
        placeholders = ",".join("?" for _ in city_syms)
        with closing(open_teryt_database(database_path)) as connection:
            for row in connection.execute(
                f"SELECT sym, nazwa, woj, pow, gmi FROM simc WHERE sym IN ({placeholders})",
                city_syms,
            ):
                voivodeship_code = str(row["woj"] or "").zfill(2)
                county_part = str(row["pow"] or "").zfill(2)
                commune_part = str(row["gmi"] or "").zfill(2)
                city_names[str(row["sym"])] = {
                    "city": str(row["nazwa"] or ""),
                    "voivodeship_code": voivodeship_code,
                    "county_code": f"{voivodeship_code}{county_part}",
                    "commune_code": f"{voivodeship_code}{county_part}{commune_part}",
                }
            for row in connection.execute(
                f"""
                SELECT sym, sym_ul, cecha, nazwa_1, nazwa_2
                FROM ulic
                WHERE sym IN ({placeholders})
                """,
                city_syms,
            ):
                street_names[(str(row["sym"]), str(row["sym_ul"]))] = " ".join(
                    value
                    for value in (
                        str(row["cecha"] or "").strip(),
                        str(row["nazwa_1"] or "").strip(),
                        str(row["nazwa_2"] or "").strip(),
                    )
                    if value
                )

    for location in locations:
        object_id = str(location["id"])
        city_sym = str(location["city_sym"])
        street_sym = str(location.get("street_sym") or "")
        city_data = city_names.get(city_sym, {})
        profiles[object_id] = {
            "city": str(city_data.get("city") or location.get("name") or ""),
            "street": street_names.get((city_sym, street_sym), ""),
            "voivodeship_code": str(city_data.get("voivodeship_code") or ""),
            "county_code": str(city_data.get("county_code") or ""),
            "commune_code": str(city_data.get("commune_code") or ""),
            "gps_region": str(
                (location.get("location_audit") or {}).get("gps_region") or ""
            ),
            "gps_commune": str(
                (location.get("location_audit") or {}).get("gps_commune") or ""
            ),
        }
    return profiles


def haversine_meters(latitude_a, longitude_a, latitude_b, longitude_b):
    earth_radius = 6_371_000
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def point_in_polygon(latitude, longitude, polygon):
    if len(polygon) < 3:
        return False
    ring = [[float(point["longitude"]), float(point["latitude"])] for point in polygon]
    return ring_position(latitude, longitude, ring) >= 0


def enea_detail_mentions_city(detail, city):
    """Rozpoznaje nazwę miejscowości, a nie nazwę ulicy lub obrębu."""

    detail = normalized_text(detail)
    city = normalized_text(city)
    if not detail or not city:
        return False
    city_pattern = r"(?<!\w)" + re.escape(city) + r"(?!\w)"
    return bool(
        re.search(r"^\s*" + city_pattern, detail)
        or re.search(r"[;:\n]\s*" + city_pattern, detail)
        or re.search(
            r"(?<!\w)(?:miejscowosc(?:i)?|miasto|wies)\s+" + city_pattern,
            detail,
        )
        or re.search(r"(?<!\w)(?:gmina|gm\.)\s+" + city_pattern, detail)
    )


def canonical_matching_addresses(source_event, location, profile, max_distance_m, searchable=None):
    """Funkcja zgodności dla klientów używających dotychczasowej trójki."""
    decision = canonical_matching_decision(source_event, location, profile, max_distance_m, searchable)
    return decision['matched'], decision['method'], decision['matched_locations']


def canonical_matching_decision(
    source_event,
    location,
    profile,
    max_distance_m,
    searchable=None,
):
    """Zwraca (dopasowanie, pewność, pasujące adresy)."""

    addresses = [
        address
        for address in source_event.get("addresses") or []
        if isinstance(address, dict)
    ]
    city_sym = str(location.get("city_sym") or "")
    street_sym = str(location.get("street_sym") or "")
    keyword = normalized_text(location.get("keyword"))
    review_required = bool(
        (location.get("location_audit") or {}).get("requires_review")
    )
    address = address_decision(source_event, location)
    details = {"object_city_sym": city_sym, "object_street_sym": street_sym,
               "object_house_number": location.get("house_number"), "address": address}
    def finish(matched, method, matches):
        result = deepcopy(details)
        rows = result["address"].get("rows", [])
        relevant = [row for row in rows if row.get("source_city_sym") == city_sym or row.get("result") == "unknown"]
        result["address"]["rows"] = relevant[:50]
        result["address"]["row_count"] = len(rows)
        limitations = []
        if len(relevant) > 50: limitations.append("details_truncated")
        if not location.get("house_number"): limitations.append("house_number_missing")
        if address["result"] == "unknown": limitations.append("address_not_verified")
        if method == "exact_teryt":
            result["street_compared"] = bool(street_sym and any(str((row.get("teryt") or {}).get("streetSymUl") or "") == street_sym for row in matches))
            result["matched_teryt"] = [{"city_sym":(row.get("teryt") or {}).get("citySym"), "street_sym":(row.get("teryt") or {}).get("streetSymUl")} for row in matches]
        return {"matched":matched,"method":method,"matched_locations":matches,
                "details":result,"limitations":limitations}
    geometry = source_event.get("geometry")
    latitude = optional_float(location.get("latitude"))
    longitude = optional_float(location.get("longitude"))
    if address["result"] != "unknown":
        inside = (geometry_contains(latitude, longitude, geometry)
                  if geometry is not None and latitude is not None and longitude is not None else None)
        if inside is not None:
            details["geometry"] = dict(geometry_decision(latitude, longitude, geometry), type=geometry["type"], geometry_ref=geometry_reference(geometry))
        if inside is not None and inside != (address["result"] == "matches"):
            return finish(True, "address_geometry_conflict", deepcopy(addresses))
        if address["result"] == "does_not_match":
            return finish(False, "address_excluded", [])
        return finish(True, "exact_address", deepcopy(addresses))

    if geometry is not None:
        if latitude is None or longitude is None:
            return finish(False, "spatial_missing_gps", [])
        details["geometry"] = dict(geometry_decision(latitude, longitude, geometry), type=geometry["type"], geometry_ref=geometry_reference(geometry))
        if details["geometry"]["contains"]:
            return finish(True, "spatial_polygon", deepcopy(addresses))
        return finish(False, "spatial_outside", [])

    if source_event.get("source") == "pge" and not review_required:
        pge_matches = matching_addresses(
            source_event.get("raw") or {}, city_sym, street_sym, location.get("keyword", "")
        )
        if pge_matches:
            return finish(True, "exact_teryt", pge_matches)
        return finish(False, "none", [])

    has_spatial_data = False
    if latitude is not None and longitude is not None:
        for polygon in source_event.get("polygons") or []:
            if len(polygon) >= 3:
                has_spatial_data = True
            if point_in_polygon(latitude, longitude, polygon):
                details["geometry"] = {"type":"legacy_polygon","result":"inside","geometry_ref":None}
                return finish(True, "spatial_polygon", deepcopy(addresses))
        if has_spatial_data:
            return finish(False, "spatial_outside", [])

        center = source_event.get("center") or {}
        center_lat = optional_float(center.get("latitude"))
        center_lon = optional_float(center.get("longitude"))
        radius_m = optional_float(source_event.get("radius_m"))
        if (
            source_event.get("source") != "tauron"
            and center_lat is not None
            and center_lon is not None
            and radius_m
        ):
            has_spatial_data = True
            details["proximity"] = {"distance_m":haversine_meters(latitude, longitude, center_lat, center_lon),
                "limit_m":radius_m,"limit_source":"operator_radius", "latitude":center_lat,"longitude":center_lon}
            if details["proximity"]["distance_m"] <= radius_m:
                return finish(True, "spatial_radius", deepcopy(addresses))

        distances = []
        distance_points = []
        for point in source_event.get("coordinates") or []:
            point_lat = optional_float(point.get("latitude"))
            point_lon = optional_float(point.get("longitude"))
            if point_lat is not None and point_lon is not None:
                has_spatial_data = True
                distance_points.append({"latitude":point_lat,"longitude":point_lon})
                distances.append(
                    haversine_meters(latitude, longitude, point_lat, point_lon)
                )
        if distances:
            index = min(range(len(distances)), key=lambda index:distances[index])
            details["proximity"] = dict(distance_points[index], distance_m=distances[index], limit_m=max_distance_m, limit_source="application_config")
        if distances and min(distances) <= max_distance_m:
            return finish(True, "spatial_nearby", deepcopy(addresses))

        # Gdy operator podał geometrię, nie zastępujemy jej luźnym dopasowaniem
        # nazwy z innej części Polski.
        if has_spatial_data:
            return finish(False, "spatial_outside", [])

    # Przy niepewnym TERYT nie zgadujemy po nazwie miejscowości ani powiatu.
    # Taki obiekt może zostać dopasowany tylko do geometrii źródła.
    if review_required:
        return finish(False, "gps_review_no_match", [])

    if source_event.get("type") == "mass_outage":
        county_code = str(profile.get("county_code") or "")
        if county_code and county_code in source_event.get("county_codes", []):
            details["county_code"] = county_code
            return finish(True, "county_mass", deepcopy(addresses))

    if searchable is None:
        searchable = normalized_text(
            " ".join(
                [
                    str(source_event.get("description") or ""),
                    *(str(address.get("text") or "") for address in addresses),
                ]
            )
        )
    city = normalized_text(profile.get("city"))
    street = normalized_text(profile.get("street"))
    if source_event.get("source") == "enea":
        raw = source_event.get("raw") or {}
        area_source = str(raw.get("title") or str(source_event.get("description") or "").split("—", 1)[0])
        detail_source = str(raw.get("description") or "")
        if not detail_source:
            detail_source = " ".join(str(address.get("text") or "") for address in addresses)
        area = normalized_text(area_source)
        detail = normalized_text(detail_source)
        commune = normalized_text(profile.get("gps_commune"))
        if (
            commune
            and city
            and commune != city
            and area
            and not re.search(r"(?<!\w)" + re.escape(commune) + r"(?!\w)", area)
        ):
            return finish(False, "area_mismatch", [])
        qualifier_match = re.search(r"\(([^)]*)\)", area_source)
        if qualifier_match:
            qualifier = normalized_text(qualifier_match.group(1))
            if "szczecin" in qualifier and str(profile.get("voivodeship_code") or "") != "32":
                return finish(False, "area_mismatch", [])
        if detail:
            if city and not enea_detail_mentions_city(detail, city):
                return finish(False, "none", [])
            searchable = detail
    details["text"] = {"city":city,"street":street,"keyword":keyword,
                       "commune":normalized_text(profile.get("gps_commune"))}
    details["street_compared"] = bool(street and street in searchable)
    city_position = searchable.find(city) if city else -1
    details["text"]["fragment"] = searchable[max(0,city_position-60):max(0,city_position-60)+240]
    # „Chełm” nie może pasować do ulicy „Chełmońskiego” w innym mieście.
    if city and re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", searchable):
        if street and street not in searchable:
            if keyword and keyword in searchable:
                return finish(True, "text_keyword", deepcopy(addresses))
            return finish(False, "none", [])
        if keyword and keyword not in searchable:
            return finish(False, "none", [])
        return finish(True, "text_address", deepcopy(addresses))
    return finish(False, "none", [])


def _event_periods(event):
    periods = event.get("periods") if isinstance(event.get("periods"), list) else []
    if not periods:
        periods = [{"start_at": event.get("start_at"), "stop_at": event.get("stop_at")}]
    result = {}
    for period in periods:
        if not isinstance(period, dict):
            continue
        start_at = first_event_time_value(period, EVENT_START_FIELDS)
        stop_at = first_event_time_value(period, EVENT_STOP_FIELDS)
        if start_at in (None, "") and stop_at in (None, ""):
            continue
        item = {"startAt": start_at, "stopAt": stop_at}
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        result[key] = item
    return [result[key] for key in sorted(result)]


def _merge_dict_lists(*values):
    result = {}
    for items in values:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            result[key] = deepcopy(item)
    return [result[key] for key in sorted(result)]


def merge_tauron_object_event(existing, current):
    if (existing.get("type"), existing.get("status")) != (
        current.get("type"),
        current.get("status"),
    ):
        raise ValueError("TAURON: sprzeczny typ lub status dla tego samego zdarzenia")
    periods = _event_periods(existing) + _event_periods(current)
    periods = _merge_dict_lists(periods)
    existing["periods"] = periods
    if periods:
        existing["start_at"] = periods[0].get("startAt")
        existing["stop_at"] = periods[0].get("stopAt")
    existing["matched_locations"] = _merge_dict_lists(
        existing.get("matched_locations"), current.get("matched_locations")
    )
    existing["coordinates"] = _merge_dict_lists(
        existing.get("coordinates"), current.get("coordinates")
    )
    stored = existing["source_event"]
    stored["periods"] = deepcopy(periods)
    stored["start_at"] = existing.get("start_at")
    stored["stop_at"] = existing.get("stop_at")
    stored["addresses"] = _merge_dict_lists(
        stored.get("addresses"),
        current.get("source_event", {}).get("addresses"),
    )
    stored["coordinates"] = deepcopy(existing["coordinates"])
    existing["_source_variant_hashes"].extend(current["_source_variant_hashes"])
    existing['match_explanation']['details']['variants'] = _merge_dict_lists(
        existing['match_explanation']['details']['variants'], current['match_explanation']['details']['variants'])
    if len({row['method'] for row in existing['match_explanation']['details']['variants']}) > 1:
        existing['match_explanation']['limitations'] = sorted(set(existing['match_explanation']['limitations']) | {'variant_bases_differ'})


def normalize_source_events(source_events, config):
    normalized = {}
    profiles = config.get("location_profiles", {})
    max_distance_m = int(config.get("spatial_match_radius_m", 1000))
    locations = config["teryt_locations"]
    locations_by_city = {}
    for location in locations:
        locations_by_city.setdefault(str(location.get("city_sym") or ""), []).append(
            location
        )

    ordered_source_events = sorted(
        source_events,
        key=lambda item: event_hash(item) if isinstance(item, dict) else "",
    )
    for source_event in ordered_source_events:
        if not isinstance(source_event, dict):
            continue
        source = str(source_event.get("source") or "")
        source_event_id = str(source_event.get("source_event_id") or "")
        if source not in SOURCE_LABELS or not source_event_id:
            continue
        source_hash = event_hash(source_event)
        map_reference = geometry_reference(source_map_geometry(source_event))
        searchable = normalized_text(
            " ".join(
                [
                    str(source_event.get("description") or ""),
                    *(
                        str(address.get("text") or "")
                        for address in source_event.get("addresses") or []
                        if isinstance(address, dict)
                    ),
                ]
            )
        )
        event_locations = locations
        if source == "pge":
            candidates = {}
            for address in (source_event.get("raw") or {}).get("addresses") or []:
                if not isinstance(address, dict):
                    continue
                city_sym = str((address.get("teryt") or {}).get("citySym") or "")
                for location in locations_by_city.get(city_sym, []):
                    candidates[str(location["id"])] = location
            for location in locations:
                if (location.get("location_audit") or {}).get("requires_review"):
                    candidates[str(location["id"])] = location
            event_locations = candidates.values()

        for location in event_locations:
            object_id = str(location["id"])
            profile = profiles.get(object_id, {})
            decision = canonical_matching_decision(
                source_event,
                location,
                profile,
                max_distance_m,
                searchable,
            )
            matched, confidence, matches = decision['matched'], decision['method'], decision['matched_locations']
            if not matched:
                continue
            evidence = matching_evidence(source_event, location, confidence, decision)
            rule_version = f'matching:{MATCHING_VERSION}'
            if source == 'enea': rule_version += f':enea:{ENEA_MATCHING_VERSION}'
            if source == 'energa': rule_version += f':energa:{ENERGA_MATCHING_VERSION}'
            explanation = explain(decision, evidence, rule_version)
            event_key = f"{source}:{source_event_id}:object:{object_id}"
            city = str(profile.get("city") or location.get("name") or location["city_sym"])
            raw = deepcopy(source_event.get("raw") or {})
            stored_source_event = deepcopy(source_event)
            # Geometria została już wykorzystana do dopasowania i nie powinna być
            # kopiowana do state.json dla każdego obiektu pasującego do zdarzenia.
            stored_source_event["polygons"] = []
            stored_source_event.pop("geometry", None)
            compact_coordinates = deepcopy(source_event.get("coordinates") or [])
            if source == "pge":
                # PGE potrafi zwrócić tysiące punktów granicy dla jednego
                # zdarzenia. W panelu wystarczy znacznik, a dopasowanie i tak
                # odbywa się po dokładnym TERYT.
                compact_coordinates = compact_coordinates[:1]
                stored_source_event["addresses"] = deepcopy(matches)
            stored_source_event["coordinates"] = compact_coordinates
            compact_raw = deepcopy(raw)
            if source == "pge":
                compact_raw = {}
            elif source == "energa":
                compact_raw.pop("polygon", None)
                compact_raw.pop("topologyElements", None)
            elif source == "stoen":
                compact_raw.pop("area", None)
                compact_raw.pop("points", None)
            stored_source_event["raw"] = compact_raw
            event = {
                "event_key": event_key,
                "source": source,
                "operator": SOURCE_LABELS[source],
                "source_event_id": source_event_id,
                "object_id": object_id,
                "type": source_event.get("type", "unknown"),
                "status": source_event.get("status", "active"),
                "revoked": source_event.get("status") == "cancelled",
                "city_sym": str(location["city_sym"]),
                "city": city,
                "configured_name": str(location.get("name") or ""),
                "keyword": str(location.get("keyword") or ""),
                "street_sym": str(location.get("street_sym") or ""),
                "object_latitude": location.get("latitude"),
                "object_longitude": location.get("longitude"),
                "object_house_number": location.get("house_number"),
                "location_review_required": bool(location.get("location_audit", {}).get("requires_review")),
                "matched_locations": matches,
                "match_confidence": confidence,
                "match_evidence": evidence,
                "match_explanation": explanation,
                "geometry_ref": map_reference,
                "description": source_event.get("description"),
                "start_at": source_event.get("start_at"),
                "stop_at": source_event.get("stop_at"),
                "periods": deepcopy(source_event.get("periods") or []),
                "coordinates": compact_coordinates,
                "source_url": source_event.get("source_url"),
                "source_hash": source_hash,
                "source_event": stored_source_event,
            }
            if source == "tauron":
                event["_source_variant_hashes"] = [source_hash]
                explanation['details']['variants'] = [
                    {'method':confidence, 'scope':evidence['scope'], 'basis':explanation['basis'],
                     'geometry_ref':map_reference, 'periods':deepcopy(event['periods']),
                     'details':deepcopy(explanation['details'])}]
            if source == "pge":
                event["pge_uuid"] = source_event_id
                event["pge_event"] = raw
            if source == "tauron" and event_key in normalized:
                merge_tauron_object_event(normalized[event_key], event)
            else:
                normalized[event_key] = event
    for event in normalized.values():
        variant_hashes = event.pop("_source_variant_hashes", None)
        if variant_hashes:
            event["source_hash"] = event_hash(
                {
                    "variants": sorted(set(variant_hashes)),
                    "periods": event.get("periods") or [],
                }
            )
    return normalized


def normalize_events(
    raw_events,
    config,
):
    """Zwraca zdarzenia osobno dla każdego pasującego TERYT-u.

    Oryginalny rekord PGE jest kopiowany w całości do pola pge_event.
    """

    normalized = {}

    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        source_uuid = str(raw.get("uuid") or "").strip().lower()
        if not source_uuid:
            continue

        raw_copy = deepcopy(raw)
        raw_hash = event_hash(raw_copy)
        kind = event_type(raw_copy)
        revoked = raw_copy.get("revoked") is True

        for location in config["teryt_locations"]:
            object_id = str(location["id"])
            city_sym = str(location["city_sym"])
            street_sym = str(location.get("street_sym") or "")
            configured_name = str(location.get("name") or "")
            keyword = str(location.get("keyword") or "")
            matches = matching_addresses(raw_copy, city_sym, street_sym, keyword)
            if address_decision({"source": "pge", "raw": raw_copy}, location)["result"] == "does_not_match":
                continue
            if not matches:
                continue

            first_teryt = matches[0].get("teryt") or {}
            city_name = str(first_teryt.get("cityName") or configured_name or city_sym)
            event_key = f"{source_uuid}:object:{object_id}"
            normalized[event_key] = {
                "event_key": event_key,
                "object_id": object_id,
                "pge_uuid": source_uuid,
                "type": kind,
                "status": "cancelled" if revoked else "active",
                "revoked": revoked,
                "city_sym": city_sym,
                "city": city_name,
                "configured_name": configured_name,
                "keyword": keyword,
                "street_sym": street_sym,
                "object_latitude": location.get("latitude"),
                "object_longitude": location.get("longitude"),
                "matched_locations": matches,
                "source_hash": raw_hash,
                "pge_event": raw_copy,
                "object_house_number": location.get("house_number"),
            }

    return normalized


def compute_changes(
    previous,
    current,
):
    changes = []

    for key, current_event in current.items():
        previous_event = previous.get(key)
        if previous_event is None:
            action = "cancelled" if current_event.get("revoked") else "new"
            changes.append((action, current_event))
            continue

        if previous_event.get("source_hash") == current_event.get("source_hash"):
            continue

        was_revoked = previous_event.get("revoked") is True
        is_revoked = current_event.get("revoked") is True
        action = "cancelled" if is_revoked and not was_revoked else "updated"
        changes.append((action, current_event))

    for key, previous_event in previous.items():
        if key not in current:
            changes.append(("resolved", previous_event))

    return sorted(changes, key=lambda item: (item[1].get("city", ""), item[0]))


def notification_payload(action, event):
    source_hash = str(event.get("source_hash") or "")
    source = str(event.get("source") or "pge")
    source_event_id = str(
        event.get("source_event_id") or event.get("pge_uuid") or "unknown"
    )
    notification_id = ":".join(
        [
            source,
            source_event_id,
            str(event.get("object_id") or event.get("city_sym") or "unknown"),
            action,
            source_hash[:12],
        ]
    )

    status = action if action in {"resolved", "out_of_scope", "not_published"} else str(event.get("status") or "active")
    return {
        "notification_id": notification_id,
        "action": action,
        "type": event.get("type"),
        "status": status,
        "resolution_reason": event.get("resolution_reason"),
        "source": source,
        "operator": event.get("operator") or SOURCE_LABELS.get(source, source),
        "source_event_id": source_event_id,
        "pge_uuid": event.get("pge_uuid"),
        "object_id": event.get("object_id"),
        "city_sym": event.get("city_sym"),
        "city": event.get("city"),
        "configured_name": event.get("configured_name"),
        "keyword": event.get("keyword"),
        "street_sym": event.get("street_sym"),
        "object_latitude": event.get("object_latitude"),
        "object_longitude": event.get("object_longitude"),
        "matched_locations": deepcopy(event.get("matched_locations", [])),
        "match_confidence": event.get("match_confidence"),
        "source_event": deepcopy(event.get("source_event", {})),
        "match_evidence": match_details(event),
        "match_explanation": explanation_details(event),
        "pge_event": deepcopy(event.get("pge_event", {})),
        "sent_at": now_iso(),
    }


async def send_notification(
    session,
    api_url,
    payload,
):
    if not api_url:
        return {
            "notification_id": payload["notification_id"],
            "result": "disabled",
            "http_status": None,
        }

    try:
        async with session.post(api_url, json=payload) as response:
            body = await response.text()
            if 200 <= response.status < 300:
                result = "sent"
                error = None
            else:
                result = "failed"
                error = body[:200]
            return {
                "notification_id": payload["notification_id"],
                "result": result,
                "http_status": response.status,
                "error": error,
            }
    except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError) as error:
        return {
            "notification_id": payload["notification_id"],
            "result": "failed",
            "http_status": None,
            "error": str(error),
        }


def schedule_notifications(app, api_url, changes):
    """Pomocnicza funkcja wybudzająca zgodna z dotychczasowym interfejsem; nie wysyła danych i nie tworzy zadań."""
    if any(point_confirmed(event) for _action, event in changes):
        worker = app.get(NOTIFICATION_WORKER_KEY)
        if worker is not None:
            worker.wake.set()
    return None


def empty_state():
    return {"saved_at": None, "events": {}}


def load_state(path=STATE_PATH):
    state = load_json(path, empty_state())
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        raise EngineStoreError("Plik state.json ma niepoprawną strukturę; migracja przerwana")
    return state


async def persist_state(app, state, history_records=(), notification_jobs=()):
    store = app[ENGINE_STORE_KEY]
    try:
        if notification_jobs:
            result = await store.write_state(state, history_records=history_records, notification_jobs=notification_jobs)
        elif history_records:
            result = await store.write_state(state, history_records=history_records)
        else:
            result = await store.write_state(state)
    except EngineStoreError:
        # Nie nadpisuj zatwierdzonej transakcji starym stanem po niepewnym COMMIT.
        committed, revision = await asyncio.to_thread(store.load_current)
        if revision <= app[STORAGE_STATUS_KEY]['revision'] or committed != state:
            raise
        result = StoreWriteResult(revision, 'Zapis SQLite zatwierdzony; wynik eksportu JSON wymaga sprawdzenia')
    app[STORAGE_STATUS_KEY]["revision"] = result.revision
    app[STORAGE_STATUS_KEY]["state_json_export_error"] = result.export_error
    if result.export_error:
        logger.error("Nie udało się odświeżyć eksportu state.json: %s", result.export_error)
    return result


def public_event(event, *, include_raw=False):
    result = {
        key: deepcopy(value)
        for key, value in event.items()
        if key not in {"source_hash", "pge_event"}
    }
    source_event = result.get("source_event")
    result["match_evidence"] = match_details(event)
    result["match_explanation"] = explanation_details(event)
    if isinstance(source_event, dict) and not include_raw:
        source_event["raw"] = {}
    if include_raw and event.get("pge_event") is not None:
        result["pge_event"] = deepcopy(event.get("pge_event"))
    result["source_status"] = str(event.get("status") or "unknown")
    result["time_phase"] = event_time_phase(event)
    result["time_windows"] = event_time_windows(event)
    return result


def sorted_public_events(events, *, include_raw=False):
    result = [
        public_event(event, include_raw=include_raw) for event in events.values()
    ]
    result.sort(
        key=lambda item: (
            str(item.get("city") or ""),
            str(
                item.get("start_at")
                or (item.get("pge_event") or {}).get("startAt")
                or ""
            ),
            str(item.get("type") or ""),
        )
    )
    return result


def event_source(event):
    return str(event.get("source") or ("pge" if event.get("pge_event") else "unknown"))


def source_is_due(health, interval_seconds, now):
    last_attempt = health.get("last_attempt_at")
    if not last_attempt:
        return True
    try:
        last = datetime.fromisoformat(last_attempt)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last).total_seconds() >= interval_seconds


def source_effective_interval(source, health, configured_interval):
    interval = max(
        PGE_MIN_INTERVAL_SECONDS if source == "pge" else 0,
        int(configured_interval),
    )
    if source != "pge":
        return interval
    if health.get("error_category") == "catalog_changed" or health.get("error") == LEGACY_CATALOG_CHANGE_ERROR:
        return interval
    errors = max(0, int(health.get("consecutive_errors") or 0))
    if not errors:
        return interval
    return max(
        interval,
        min(interval * (2 ** min(errors, 3)), PGE_MAX_ERROR_INTERVAL_SECONDS),
    )


def source_wait_seconds(source, health, configured_interval, now):
    interval = source_effective_interval(source, health, configured_interval)
    last_attempt = health.get("last_attempt_at")
    interval_wait = 0.0
    if last_attempt:
        try:
            last = datetime.fromisoformat(last_attempt)
        except (TypeError, ValueError):
            last = None
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            interval_wait = interval - (now - last).total_seconds()

    blocked_wait = 0.0
    blocked_until = health.get("blocked_until")
    if blocked_until:
        try:
            blocked = datetime.fromisoformat(str(blocked_until).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            blocked = None
        if blocked is not None:
            if blocked.tzinfo is None:
                blocked = blocked.replace(tzinfo=now.tzinfo)
            blocked_wait = (blocked - now).total_seconds()
    return max(0.0, interval_wait, blocked_wait)


def later_timestamp(current_value, candidate):
    current = parse_event_datetime(current_value)
    if current is not None and current >= candidate:
        return str(current_value)
    return candidate.astimezone().isoformat(timespec="seconds")


def object_fingerprints(config, source=None):
    matching_version = (
        f"{MATCHING_VERSION}:enea:{ENEA_MATCHING_VERSION}"
        if source == "enea"
        else MATCHING_VERSION
    )
    if source == "energa":
        matching_version = f"{MATCHING_VERSION}:energa:{ENERGA_MATCHING_VERSION}"
    return {
        str(obj["id"]): event_hash({"matching_version": matching_version,
                                   "object": {key: value for key, value in obj.items() if key != "power_supply"}})
        for obj in config["teryt_locations"]
    }


def capture_export_context(application):
    config = deepcopy(application[CONFIG_KEY])
    view = publication.current() or application[publication.KEY].snapshot()
    objects = view.objects(enriched_objects(config['teryt_locations'], application[TERYT_DB_PATH_KEY]))
    runtime = deepcopy(application[RUNTIME_KEY])
    def project(state):
        saved_runtime = dict(runtime, events=state['events'])
        saved_runtime['sources'] = {key: dict(value, **state.get('sources', {}).get(key, {}))
                                    for key, value in runtime['sources'].items()}
        shadow = {CONFIG_KEY: config, STATE_KEY: state, RUNTIME_KEY: saved_runtime, publication.KEY: application[publication.KEY]}
        visible, pending = event_visibility(shadow)
        return {'config': lambda: config, 'runtime': lambda: saved_runtime, 'state': lambda: state,
                'objects': lambda: objects, 'visible_events': lambda: visible, 'matching_pending': lambda: pending,
                'event_time_phase': event_time_phase, 'event_time_windows': event_time_windows,
                'source_wait_seconds': source_wait_seconds}
    return project


def event_visibility(app):
    """Stan zachowujemy do diagnostyki; publikujemy tylko aktualne przypisania."""
    config = app[CONFIG_KEY]
    objects = {str(obj["id"]): obj for obj in config["teryt_locations"]}
    enabled = set(config["enabled_sources"])
    recorded = app[STATE_KEY].get("object_fingerprints", {})
    pending = {object_id: [] for object_id in objects}
    for source in config["enabled_sources"]:
        previous = recorded.get(source)
        if previous is not None:
            for object_id, fingerprint in object_fingerprints(config, source).items():
                if previous.get(object_id) != fingerprint:
                    pending[object_id].append(source)
    events = app[RUNTIME_KEY]["events"]
    # Zgodność ze stanem sprzed zapisywania fingerprintów: porównaj znane pola.
    fields = {"city_sym": "city_sym", "street_sym": "street_sym", "keyword": "keyword",
              "object_latitude": "latitude", "object_longitude": "longitude",
              "object_house_number": "house_number"}
    for event in events.values():
        object_id = str(event.get("object_id") or "")
        source = event_source(event)
        if source not in enabled or object_id not in objects or source in recorded:
            continue
        obj = objects[object_id]
        number_changed = event.get("object_house_number") != obj.get("house_number")
        if number_changed or any(event[key] != obj.get(field) for key, field in fields.items() if key in event):
            if source not in pending[object_id]:
                pending[object_id].append(source)
    visible = {key: event for key, event in events.items()
               if event_source(event) in enabled
               and str(event.get("object_id") or "") in objects
               and event_source(event) not in pending[str(event["object_id"])]}
    policy = publication.current() or (app[publication.KEY].snapshot() if publication.KEY in app else None)
    if policy is not None:
        visible = {key:value for key,value in visible.items() if policy.event_allowed(value, list(objects.values()))}
        pending = {key:[source for source in value if policy.allows(source)] for key,value in pending.items()
                   if policy.public_object(objects[key]) is not None}
    return visible, {key: value for key, value in pending.items() if value}


def refresh_runtime_health(app):
    runtime = app[RUNTIME_KEY]
    health = {source: runtime["sources"][source] for source in app[CONFIG_KEY]["enabled_sources"]}
    visible, pending = event_visibility(app)
    for source, item in health.items():
        item["matched_event_count"] = sum(event_source(event) == source for event in visible.values())
        item["matching_pending_count"] = sum(source in values for values in pending.values())
    runtime["scan_running"] = any(item.get("running") for item in health.values())
    runtime["error"] = "; ".join(
        f"{SOURCE_LABELS[source]}: {item['error']}"
        for source, item in health.items() if item.get("error")
    ) or None
    runtime["source_event_count"] = sum(int(item.get("source_event_count") or 0) for item in health.values())
    runtime["matched_event_count"] = len(visible)


class CatalogChangedError(RuntimeError):
    pass


@asynccontextmanager
async def matched_current_catalog(app, source, source_events, config):
    """Ponawia tylko obliczenia; stabilny katalog pozostaje zablokowany do zapisu."""
    for _ in range(MAX_CATALOG_MATCH_ATTEMPTS):
        fingerprints = object_fingerprints(config, source)
        with measured("match"):
            matched = await asyncio.to_thread(normalize_source_events, source_events, config)
        async with app[OBJECTS_LOCK_KEY]:
            if fingerprints != object_fingerprints(app[CONFIG_KEY], source):
                config = deepcopy(app[CONFIG_KEY])
                continue
            yield config, fingerprints, matched
        return
    raise CatalogChangedError("Lista obiektów zmieniała się podczas dopasowania. Spróbuję w następnym skanie.")


async def scan_source(app, source):
    if publication.KEY in app and not app[publication.KEY].snapshot().allows(source, "collect"):
        return {"ok": True, "source": source, "skipped": True, "publication_restricted": True}
    lock = app[SOURCE_LOCKS_KEY][source]
    async with lock:
        runtime = app[RUNTIME_KEY]
        health = runtime["sources"][source]
        configured_interval = app[CONFIG_KEY]["source_scan_intervals_seconds"][source]
        wait_seconds = source_wait_seconds(
            source,
            health,
            configured_interval,
            datetime.now().astimezone(),
        )
        if wait_seconds > 0:
            return {
                "ok": True,
                "source": source,
                "skipped": True,
                "wait_seconds": wait_seconds,
            }

        scope = fingerprint({"source": source, "parser": "contracts:20261011:1", "regions": app[CONFIG_KEY].get("enea_regions"), "filters": app[CONFIG_KEY].get("enea_region_ids")})
        async with app[DIAGNOSTICS_KEY].capture(source, scope) as diagnostic:
            attempt_at = now_iso()
            reserved_health = dict(health, last_attempt_at=attempt_at, running=True)
            try:
                async with app[SCAN_LOCK_KEY]:
                    reserved_state = deepcopy(app[STATE_KEY])
                    reserved_state.setdefault("sources", {})[source] = deepcopy(reserved_health)
                    await persist_state(app, reserved_state)
                    app[STATE_KEY].clear()
                    app[STATE_KEY].update(reserved_state)
            except Exception as error:
                diagnostic.failed(error)
                message = f"Nie udało się zapisać rozpoczęcia skanu: {public_error_message(error)}"
                health.update(error=message, running=False)
                refresh_runtime_health(app)
                logger.exception("Nie udało się zarezerwować skanu źródła %s", source)
                return {
                    "ok": False,
                    "source": source,
                    "error": message,
                    "storage_error": True,
                }

            health.update(reserved_health)
            runtime["last_scan_started_at"] = health["last_attempt_at"]
            refresh_runtime_health(app)
            config = deepcopy(app[CONFIG_KEY])
            try:
                async with asyncio.timeout(SOURCE_FETCH_TIMEOUT_SECONDS):
                    source_events = await fetch_source(app[SESSION_KEY], source, config)
                if not isinstance(source_events, list):
                    raise ValueError("Źródło nie zwróciło kompletnej listy")
                diagnostic.data["completeness"] = "complete"
                diagnostic.data['source_event_count'] = len(source_events)
                diagnostic.data['unknown_type_count'] = sum(item.get('type') == 'unknown' for item in source_events)
                diagnostic.data['warnings'].extend(sorted({warning for item in source_events
                    for warning in event_time_warnings(item)}))
                if diagnostic.data['unknown_type_count']:
                    diagnostic.data['warnings'].append('unknown_event_types')
                source_ids = {str(item["source_event_id"]).lower() for item in source_events}
                # Sprawdzenie i zapis nie mogą ścigać się z edycją obiektów.
                async with matched_current_catalog(app, source, source_events, config) as (config, fingerprints, matched), app[SCAN_LOCK_KEY]:
                    if fingerprints != object_fingerprints(app[CONFIG_KEY], source):
                        raise CatalogChangedError("Lista obiektów zmieniła się przed zapisem. Spróbuję w następnym skanie.")
                    state = app[STATE_KEY]
                    previous = {key: event for key, event in state["events"].items()
                                if event_source(event) == source}
                    previous_fingerprints = state.get("object_fingerprints", {}).get(source)
                    baseline = previous_fingerprints is None
                    changed = {key for key in set(fingerprints) | set(previous_fingerprints or {})
                               if fingerprints.get(key) != (previous_fingerprints or {}).get(key)}
                    current, missing, departures = reconcile(
                        previous, matched, source_ids, state.get("missing", {}).get(source, {}), changed
                    )
                    # Brak z listy jest obsługiwany osobno, z uwzględnieniem całego źródła.
                    all_events = {key: event for key, event in state["events"].items()
                                  if event_source(event) != source}
                    all_events.update(current)
                    saved_at = now_iso()
                    history_records = []
                    notification_changes = set()
                    for key, event in sorted(current.items()):
                        if event.get('verification_pending'):
                            continue
                        old = previous.get(key)
                        if baseline or str(event.get('object_id')) in changed:
                            history_records.append(history_record('snapshot', event, None, saved_at, 'baseline'))
                        elif old is None:
                            action = 'cancelled' if event.get('revoked') else 'new'
                            history_records.append(history_record(action, event, None, saved_at))
                            notification_changes.add(key)
                        else:
                            candidate = history_record('cancelled' if event.get('revoked') and not old.get('revoked') else 'updated',
                                                       event, old, saved_at)
                            source_changed = source_signature(candidate['before']) != source_signature(candidate['after'])
                            evidence_changed = evidence_signature(old) != evidence_signature(event)
                            if source_changed:
                                history_records.append(candidate)
                                notification_changes.add(key)
                            elif not old.get('match_explanation') and event.get('match_explanation'):
                                history_records.append(history_record('snapshot', event, old, saved_at, 'baseline'))
                            elif evidence_changed:
                                history_records.append(candidate)
                    history_records.extend(history_record(action, event, previous.get(event.get('event_key')), saved_at)
                                           for action, event in departures)
                    notification_changes.update(event['event_key'] for _action, event in departures)
                    new_state = deepcopy(state)
                    new_state.update(saved_at=saved_at, events=all_events)
                    geometries = new_state.setdefault("source_geometries", {})
                    active_source_ids = {event.get("source_event_id") for event in all_events.values() if event_source(event) == source}
                    for item in source_events:
                        if item.get("source_event_id") not in active_source_ids:
                            continue
                        geometry = source_map_geometry(item)
                        if geometry is not None:
                            geometries[geometry_reference(geometry)] = deepcopy(geometry)
                    references = {event.get("geometry_ref") for event in all_events.values()}
                    new_state["source_geometries"] = {key: value for key, value in geometries.items() if key in references}
                    new_state.setdefault("missing", {})[source] = missing
                    new_state.setdefault("object_fingerprints", {})[source] = fingerprints
                    history = new_state.setdefault("history", [])
                    history.extend({"at": saved_at, "action": action, "event": event}
                                   for action, event in departures)
                    new_state["history"] = history[-200:]
                    recovered = bool(int(health.get("consecutive_errors") or 0))
                    source_health = dict(health, last_success_at=saved_at, error=None,
                                         error_category=None,
                                         consecutive_errors=0,
                                         blocked_until=None,
                                         running=False,
                                         recovered_at=saved_at if recovered else health.get("recovered_at"),
                                         source_event_count=len(source_events),
                                         matched_event_count=len(current))
                    source_health.update(diagnostic.data.get('_publication_metadata', {}))
                    new_state.setdefault("sources", {})[source] = source_health
                    if publication.KEY in app and not app[publication.KEY].snapshot().allows(source, "collect"):
                        return {"ok": True, "source": source, "skipped": True, "publication_restricted": True}
                    # Historia i zadania korzystają z tej samej listy semantycznych zmian.
                    changed_events = dict(current)
                    changed_events.update({event['event_key']: event for _action, event in departures})
                    changes = [(record['action'], changed_events[record['after']['event_id']])
                               for record in history_records if record['origin'] == 'scan']
                    jobs = []
                    if config['notifications_enabled'] and config['our_api_url']:
                        for record in history_records:
                            if record['origin'] != 'scan' or record['after']['event_id'] not in notification_changes:
                                continue
                            event = changed_events.get(record['after']['event_id'])
                            if event is None or not point_confirmed(event):
                                continue
                            object_id = str(event['object_id'])
                            if config['notification_object_ids'] and object_id not in config['notification_object_ids']:
                                continue
                            if object_id not in fingerprints:
                                continue
                            if not app[publication.KEY].snapshot().event_allowed(event, config["teryt_locations"], "notify"):
                                continue
                            jobs.append(make_job(record, event, config['our_api_url'], fingerprints[object_id],
                                                 notification_payload(record['action'], event)))
                    with measured("persist"):
                        await persist_state(app, new_state, history_records, jobs)
                    state.clear()
                    state.update(new_state)
                    runtime.update(events=all_events, last_scan_at=saved_at,
                                   last_success_at=saved_at, last_change_count=len(changes))
                    health.update(source_health)
                    refresh_runtime_health(app)

                # Wynik jest już dostępny, a webhook działa poza blokadą źródła.
                diagnostic.data.update(status="success", source_event_count=len(source_events),
                    matched_event_count=len(current), confirmed_count=sum(point_confirmed(e) for e in current.values()),
                    candidate_count=sum(not point_confirmed(e) for e in current.values()),
                    state_revision=app[STORAGE_STATUS_KEY]["revision"], catalog_fingerprint=fingerprint(fingerprints))
                app[NOTIFICATION_WORKER_KEY].wake.set()
                logger.info("Skan %s: źródłowe=%s, dopasowane=%s, zmiany=%s",
                            source, len(source_events), len(current), len(changes))
                return {"ok": True, "source": source, "change_count": len(changes)}
            except Exception as error:
                diagnostic.failed(error)
                message = public_error_message(error)
                failed_at = now_iso()
                prior_local_error = health.get("error_category") == "catalog_changed" or health.get("error") == LEGACY_CATALOG_CHANGE_ERROR
                provider_errors = 0 if prior_local_error else int(health.get("consecutive_errors") or 0)
                blocked_until = health.get("blocked_until")
                if isinstance(error, SourceHttpError) and error.retry_after is not None:
                    blocked_until = later_timestamp(blocked_until, error.retry_after)
                health.update(
                    error=message,
                    error_category="catalog_changed" if isinstance(error, CatalogChangedError) else "maintenance" if isinstance(error, SourceMaintenanceError) else "source",
                    last_error=message,
                    last_error_at=failed_at,
                    consecutive_errors=0 if isinstance(error, CatalogChangedError) else provider_errors + 1,
                    blocked_until=blocked_until,
                    running=False,
                )
                # Błąd źródła zapisujemy bez usuwania jego ostatnich poprawnych danych.
                try:
                    async with app[SCAN_LOCK_KEY]:
                        failed_state = deepcopy(app[STATE_KEY])
                        failed_state.setdefault("sources", {})[source] = deepcopy(health)
                        await persist_state(app, failed_state)
                        app[STATE_KEY].clear()
                        app[STATE_KEY].update(failed_state)
                except Exception:
                    logger.exception("Nie udało się zapisać stanu błędu źródła %s", source)
                logger.exception("Skan źródła %s nie powiódł się", source)
                result = {"ok": False, "source": source, "error": message}
                if isinstance(error, EngineStoreError):
                    result["storage_error"] = True
                return result
            finally:
                health["running"] = False
                refresh_runtime_health(app)


async def perform_scan(app):
    now = datetime.now().astimezone()
    due = [source for source in app[CONFIG_KEY]["enabled_sources"]
           if source_wait_seconds(
               source,
               app[RUNTIME_KEY]["sources"][source],
               app[CONFIG_KEY]["source_scan_intervals_seconds"][source],
               now,
           ) <= 0]
    results = await asyncio.gather(*(scan_source(app, source) for source in due))
    return {"ok": all(item["ok"] for item in results),
            "updated_sources": [item["source"] for item in results if item["ok"]],
            "failed_sources": [item for item in results if not item["ok"]]}


async def source_loop(app, source):
    job = app[JOBS_KEY][source]
    job.task = asyncio.current_task()
    while True:
        if not app[publication.KEY].snapshot().allows(source, "collect"):
            job.mark("waiting", 5)
            await asyncio.sleep(5)
            continue
        configured_interval = app[CONFIG_KEY]["source_scan_intervals_seconds"][source]
        wait_seconds = source_wait_seconds(
            source,
            app[RUNTIME_KEY]["sources"][source],
            configured_interval,
            datetime.now().astimezone(),
        )
        if wait_seconds:
            job.mark('waiting', wait_seconds)
            await asyncio.sleep(wait_seconds)
            continue
        job.mark('fetching', 1800)
        result = await scan_source(app, source)
        if result.get("storage_error"):
            job.mark('storage_backoff', 60)
            await asyncio.sleep(60)


async def scanner_loop(app):
    tasks = [asyncio.create_task(source_loop(app, source))
             for source in app[CONFIG_KEY]["enabled_sources"]]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def json_response(data, status=200):
    return web.json_response(
        data,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


async def admin_check_handler(request):
    return json_response({"ok": True})


def open_teryt_database(path):
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


async def coordinates_for_query(
    geocoder,
    query,
):
    query = str(query or "").strip()
    if len(query) > 1000:
        raise ValueError("Wpis jest za długi")
    if has_control_character(query):
        raise ValueError("Wpis zawiera niedozwolone znaki")
    if looks_like_url(query):
        return coordinates_from_maps_url(query)

    coordinates = coordinates_from_text(query)
    if coordinates:
        return coordinates

    def validate_search(value):
        if not isinstance(value, list):
            raise GeocoderProviderError("Geokoder zwrócił niepoprawny wynik wyszukiwania")
        if value:
            if not isinstance(value[0], dict):
                raise GeocoderProviderError("Geokoder zwrócił niepoprawny wynik wyszukiwania")
            latitude, longitude = validated_coordinates(value[0].get("lat"), value[0].get("lon"))
            if latitude is None or longitude is None:
                raise GeocoderProviderError("Geokoder nie zwrócił współrzędnych")

    data = await geocoder.request_json(
        "search",
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "countrycodes": "pl",
            "limit": "1",
        },
        empty=lambda value: not value,
        validator=validate_search,
    )
    if not isinstance(data, list) or not data:
        raise GeocoderNotFoundError("Nie znaleziono adresu. Dopisz miejscowość albo kod pocztowy")
    return validated_coordinates(data[0].get("lat"), data[0].get("lon"))


async def resolved_location(
    geocoder,
    database_path,
    latitude,
    longitude,
):
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Nieprawidłowe współrzędne")

    def validate_reverse(value):
        if not isinstance(value, dict) or not isinstance(value.get("address"), dict):
            raise GeocoderProviderError("Geokoder zwrócił niepoprawny wynik geokodowania odwrotnego")

    data = await geocoder.request_json(
        "reverse",
        {
            "lat": f"{latitude:.7f}",
            "lon": f"{longitude:.7f}",
            "format": "jsonv2",
            "addressdetails": "1",
            "zoom": "18",
        },
        validator=validate_reverse,
    )
    if not isinstance(data, dict):
        raise TypeError("Nie udało się rozpoznać wskazanego punktu")
    address = data.get("address")
    if not isinstance(address, dict):
        raise GeocoderNotFoundError("Geokoder nie zwrócił danych adresowych")

    try:
        teryt = teryt_from_address(database_path, address)
    except TerytResolutionError as error:
        error.osm_result = {"latitude": latitude, "longitude": longitude,
                            "display_name": str(data.get("display_name") or ""),
                            "osm_address": deepcopy(address)}
        raise
    return {
        **teryt,
        "latitude": latitude,
        "longitude": longitude,
        "display_name": str(data.get("display_name") or ""),
        "osm_address": deepcopy(address),
    }


def geocoder_error_response(error):
    if isinstance(error, TerytResolutionError):
        return json_response({"error": str(error), "code": error.code,
                              "resolution_status": "requires_review", "osm_result": error.osm_result}, status=400)
    if isinstance(error, GeocoderNotFoundError):
        return json_response({"error": str(error), "code": "not_found"}, status=404)
    if isinstance(error, GeocoderBusyError):
        return json_response(
            {"error": str(error), "code": "busy"}, status=429,
            headers={"Retry-After": "1"},
        )
    if isinstance(error, GeocoderRateLimitedError):
        return json_response(
            {"error": str(error), "code": "rate_limited"}, status=503,
            headers={"Retry-After": str(error.retry_after)},
        )
    if isinstance(error, asyncio.TimeoutError):
        return json_response(
            {"error": "Przekroczono czas oczekiwania na geokoder", "code": "timeout"},
            status=504,
        )
    if isinstance(error, GeocoderCacheError):
        return json_response({"error": str(error), "code": "cache_error"}, status=503)
    if isinstance(error, (GeocoderProviderError, GeocoderError)):
        return json_response({"error": str(error), "code": "provider_error"}, status=502)
    return json_response({"error": str(error)}, status=400)


async def resolve_location_handler(request):
    query = request.query.get("q", "").strip()
    if not query:
        return json_response({"error": "Wpisz adres, współrzędne albo odnośnik do Google Maps"}, status=400)
    try:
        latitude, longitude = await coordinates_for_query(request.app[GEOCODER_KEY], query)
        result = await resolved_location(
            request.app[GEOCODER_KEY],
            request.app[TERYT_DB_PATH_KEY],
            latitude,
            longitude,
        )
        return json_response(result)
    except (
        TypeError, ValueError, GeocoderError, asyncio.TimeoutError
    ) as error:
        return geocoder_error_response(error)


async def reverse_location_handler(request):
    try:
        latitude, longitude = validated_coordinates(
            request.query.get("lat"), request.query.get("lon")
        )
        if latitude is None:
            raise ValueError("Podaj prawidłowe współrzędne")
        result = await resolved_location(
            request.app[GEOCODER_KEY],
            request.app[TERYT_DB_PATH_KEY],
            latitude,
            longitude,
        )
        return json_response(result)
    except (
        TypeError, ValueError, GeocoderError, asyncio.TimeoutError
    ) as error:
        return geocoder_error_response(error)


async def location_check_handler(request):
    headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST, OPTIONS",
               "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Expose-Headers": "Retry-After", "Cache-Control": "no-store"}
    def response(value, status=200):
        return web.json_response(value, status=status, headers=headers)
    if request.method == "OPTIONS":
        return response({})
    if request.content_type != "application/json":
        return response({"error": "Wymagany Content-Type application/json"}, 415)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeError, LookupError):
        return response({"error": "Niepoprawny JSON"}, 400)
    except web.HTTPException as error:
        return response({"error": "Żądanie przekracza dozwolony rozmiar"}, error.status)
    if not isinstance(body, dict) or set(body) != {"object_id"} or not isinstance(body["object_id"], str):
        return response({"error": "Podaj wyłącznie tekstowy object_id"}, 400)
    async with request.app[OBJECTS_LOCK_KEY]:
        obj = next((deepcopy(row) for row in request.app[CONFIG_KEY]["teryt_locations"] if str(row["id"]) == body["object_id"]), None)
    if obj is None:
        return response({"error": "Nie znaleziono obiektu"}, 404)
    if GEOPORTAL_KEY not in request.app:
        return response({"error": "Porównywanie z Geoportalem jest wyłączone w konfiguracji"}, 503)
    store = request.app[REVIEW_STORE_KEY]
    try:
        context = await asyncio.to_thread(expected_commune, request.app[TERYT_DB_PATH_KEY], obj)
        cached = await asyncio.to_thread(store.cached, obj, context)
        if cached is not None:
            return response({"item": cached, "cached": True})
        report, evidence = await request.app[GEOPORTAL_KEY].compare(obj, context)
        context_path = request.app[TERYT_DB_PATH_KEY].parent / "geoportal-teryt-context.json"
        if context_path.exists():
            reference = await asyncio.to_thread(lambda: json.loads(context_path.read_text(encoding="utf-8")))
            report["teryt_reference"] = {key: value for key, value in reference.items() if key != "codes"}
        async with request.app[OBJECTS_LOCK_KEY]:
            current = next((row for row in request.app[CONFIG_KEY]["teryt_locations"] if str(row["id"]) == body["object_id"]), None)
            from address_matching import address_binding
            if current is None or address_binding(current) != report["binding"]:
                return response({"error": "Dane obiektu zmieniono podczas porównania; spróbuj ponownie"}, 409)
            await asyncio.to_thread(store.save, report, evidence)
            result = await asyncio.to_thread(store.read, current)
        return response({"item": result, "cached": False})
    except GeocoderRateLimitedError as error:
        result = response({"error": "Geoportal ograniczył ruch; zachowano poprzedni wynik", "retry_after": error.retry_after}, 429)
        result.headers["Retry-After"] = str(error.retry_after)
        return result
    except GeocoderBusyError:
        return response({"error": "Geoportal ma zbyt wiele oczekujących porównań"}, 503)
    except (GeocoderError, asyncio.TimeoutError, OSError, sqlite3.Error, ValueError, TypeError) as error:
        logger.warning("Porównanie Geoportalu: %s", type(error).__name__)
        return response({"error": "Nie udało się ukończyć porównania z Geoportalem; zachowano poprzedni wynik"}, 503)


def enriched_objects(objects, database_path):
    city_names = {}
    street_names = {}

    if database_path.exists() and objects:
        city_syms = sorted({str(item["city_sym"]) for item in objects})
        placeholders = ",".join("?" for _ in city_syms)
        with closing(open_teryt_database(database_path)) as connection:
            for row in connection.execute(
                f"SELECT sym, nazwa FROM simc WHERE sym IN ({placeholders})",
                city_syms,
            ):
                city_names[str(row["sym"])] = str(row["nazwa"])

            streets = sorted({(str(item["city_sym"]), str(item.get("street_sym") or ""))
                              for item in objects if item.get("street_sym")})
            # Czytamy tylko używane pary SIMC/ULIC, nie wszystkie ulice miast.
            # 400 par mieści się również w starszym limicie 999 parametrów SQLite.
            for offset in range(0, len(streets), 400):
                batch = streets[offset:offset + 400]
                values = ",".join("(?,?)" for _ in batch)
                parameters = [value for pair in batch for value in pair]
                for row in connection.execute(
                    f"""WITH requested(sym, sym_ul) AS (VALUES {values})
                    SELECT ulic.sym, ulic.sym_ul, ulic.cecha, ulic.nazwa_1, ulic.nazwa_2
                    FROM requested JOIN ulic
                      ON ulic.sym = requested.sym AND ulic.sym_ul = requested.sym_ul""",
                    parameters,
                ):
                    street_name = " ".join(
                        str(row[key] or "").strip() for key in ("cecha", "nazwa_1", "nazwa_2")
                        if str(row[key] or "").strip()
                    )
                    street_names[(str(row["sym"]), str(row["sym_ul"]))] = street_name

    result = []
    for item in objects:
        city_sym = str(item["city_sym"])
        street_sym = str(item.get("street_sym") or "")
        public_item = deepcopy(item)
        public_item["location_audit"] = public_location_audit(item)
        public_item["power_supply"] = normalize_power_supply(item.get("power_supply", {}))
        public_item["address_verified"] = address_verified(item)
        public_item["address_audit"] = public_address_audit(item)
        public_item["city"] = city_names.get(city_sym, city_sym)
        public_item["street"] = street_names.get((city_sym, street_sym), "")
        if not public_item.get("name"):
            public_item["name"] = public_item["city"]
        result.append(public_item)
    return result


def teryt_locality_details(city_syms, database_path):
    city_syms = sorted({str(value) for value in city_syms if str(value)})
    if not database_path.exists() or not city_syms:
        return {}
    placeholders = ",".join("?" for _ in city_syms)
    with closing(open_teryt_database(database_path)) as connection:
        rows = connection.execute(
            f"SELECT sym, nazwa, woj, pow, gmi FROM simc WHERE sym IN ({placeholders})",
            city_syms,
        ).fetchall()
    return {
        str(row["sym"]): {
            "city": str(row["nazwa"] or ""),
            "voivodeship_code": str(row["woj"] or ""),
            "county_code": str(row["pow"] or ""),
            "commune_code": str(row["gmi"] or ""),
        }
        for row in rows
    }


async def objects_handler(request):
    objects = enriched_objects(
        request.app[CONFIG_KEY]["teryt_locations"],
        request.app[TERYT_DB_PATH_KEY],
    )
    private = request.path.startswith("/api/admin/")
    if not private:
        objects = publication.current().objects(objects)
    return json_response({"count": len(objects), "objects": objects, "catalog_revision": catalog_revision(request.app[CONFIG_KEY]["teryt_locations"] if private else objects)})


async def publication_public_handler(request):
    return json_response(publication.public_metadata(publication.current()))


async def publication_handler(request):
    """Istniejące middleware wymaga sesji, pochodzenia i CSRF przy zapisie."""
    policy = request.app[publication.KEY]
    view = publication.current()
    if request.method == 'GET':
        if request.path.endswith('/setup'):
            return web.FileResponse(WEB_DIR / 'publication-admin.html')
        return json_response({'revision':view.revision, 'available':view.available,
                              'sources':view.sources, 'decisions':view.decisions,
                              'objects':deepcopy(request.app[CONFIG_KEY]['teryt_locations']),
                              'field_hashes':{str(obj['id']):{field:publication.digest(obj[field]) for field in publication.FIELDS if field in obj}
                                              for obj in request.app[CONFIG_KEY]['teryt_locations']}})
    if request.content_type != 'application/json' or request.content_length is None or request.content_length > 40000:
        return json_response({'error':'Wymagany dokument JSON do 40 000 bajtów.'},status=400)
    try:
        body = await request.json()
        if not isinstance(body,dict) or set(body) != {'revision','decision'}:
            raise ValueError('Podaj rewizję i decyzję.')
        kind, identifier = request.match_info['kind'], request.match_info['identifier']
        # Zapis decyzji i kopia/modyfikacja katalogu nie mogą się minąć.
        async with request.app[BACKUP_LOCK_KEY], request.app[OBJECTS_LOCK_KEY]:
            if kind == 'object':
                obj = next((item for item in request.app[CONFIG_KEY]['teryt_locations'] if str(item['id']) == identifier),None)
                if obj is None:
                    raise web.HTTPNotFound(text='Nie znaleziono obiektu.')
                for field, entry in body['decision'].get('fields',{}).items():
                    if field not in obj or entry.get('value_sha256') != publication.digest(obj[field]):
                        raise ValueError('Wartość pola zmieniła się. Wczytaj katalog ponownie.')
            revision = await asyncio.to_thread(policy.write,kind,identifier,body['decision'],body['revision'])
        return json_response({'revision':revision,'saved':True})
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError):
        return json_response({'error':'Nie zapisano decyzji. Sprawdź pola, dowody oraz aktualną rewizję.'},status=409)
    except (OSError,sqlite3.Error):
        return json_response({'error':'Magazyn decyzji jest chwilowo niedostępny.'},status=503)


async def teryt_search_handler(request):
    query = request.query.get("q", "").strip()
    if len(query) < 2:
        return json_response({"results": []})

    database_path = request.app[TERYT_DB_PATH_KEY]
    if not database_path.exists():
        return json_response({"error": "Brak lokalnego katalogu TERYT"}, status=503)

    with closing(open_teryt_database(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT sym, nazwa, woj, pow, gmi
            FROM simc
            WHERE sym LIKE ? OR nazwa LIKE ? COLLATE NOCASE
            ORDER BY CASE WHEN sym = ? THEN 0 ELSE 1 END, nazwa, sym
            LIMIT 30
            """,
            (f"{query}%", f"%{query}%", query),
        ).fetchall()

    return json_response(
        {
            "results": [
                {
                    "city_sym": str(row["sym"]),
                    "city": str(row["nazwa"]),
                    "voivodeship_code": str(row["woj"] or ""),
                    "county_code": str(row["pow"] or ""),
                    "commune_code": str(row["gmi"] or ""),
                }
                for row in rows
            ]
        }
    )


async def streets_handler(request):
    city_sym = request.match_info["city_sym"]
    query = request.query.get("q", "").strip()
    database_path = request.app[TERYT_DB_PATH_KEY]
    if not database_path.exists():
        return json_response({"error": "Brak lokalnego katalogu TERYT"}, status=503)

    with closing(open_teryt_database(database_path)) as connection:
        rows = connection.execute(
            """
            SELECT sym_ul, cecha, nazwa_1, nazwa_2
            FROM ulic
            WHERE sym = ? AND (nazwa_1 LIKE ? COLLATE NOCASE OR nazwa_2 LIKE ? COLLATE NOCASE)
            ORDER BY nazwa_1, nazwa_2
            LIMIT 40
            """,
            (city_sym, f"%{query}%", f"%{query}%"),
        ).fetchall()

    return json_response(
        {
            "results": [
                {
                    "street_sym": str(row["sym_ul"]),
                    "street": " ".join(
                        part
                        for part in (
                            str(row["cecha"] or "").strip(),
                            str(row["nazwa_1"] or "").strip(),
                            str(row["nazwa_2"] or "").strip(),
                        )
                        if part
                    ),
                }
                for row in rows
            ]
        }
    )


def next_object_id(objects):
    existing = {str(item["id"]) for item in objects}
    while True:
        candidate = str(uuid.uuid4())
        if candidate not in existing:
            return candidate


def save_catalog_change(request, before, after, action, object_id):
    expected = request.headers.get('X-Catalog-Revision')
    if expected and expected != catalog_revision(before):
        raise web.HTTPConflict(text='Katalog zmienił się w innej karcie. Wczytaj go ponownie przed zapisem.')
    reason = request.headers.get("X-Change-Reason", "Zmiana przez API; nie podano powodu").strip()
    if not reason or len(reason) > 500:
        raise ValueError("X-Change-Reason: podaj od 1 do 500 znaków")
    return request.app[CATALOG_JOURNAL_KEY].write(
        before, after, action, str(object_id), reason, save_json
    )


async def commit_catalog_change(request, before, after, action, object_id):
    """Zapis poza pętlą; wywołujący publikuje wynik pod blokadą katalogu."""
    task = asyncio.get_running_loop().run_in_executor(
        None, save_catalog_change, request, before, after, action, object_id)
    cancelled = False
    try:
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    finally:
        # Nie przerywaj między zatwierdzonym plikiem a aktualizacją pamięci.
        # Tak samo jak EngineStore.write_state: anulowanie wraca po publikacji.
        if cancelled:
            asyncio.current_task().cancel()


async def read_object_body(request):
    if request.content_type != "application/json":
        return None, json_response({"error": "Wymagany Content-Type application/json"}, status=415)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeError, LookupError):
        return None, json_response({"error": "Niepoprawny JSON"}, status=400)
    if not isinstance(body, dict):
        return None, json_response({"error": "Dane obiektu muszą być obiektem JSON"}, status=400)
    return body, None


async def create_object_handler(request):
    body, error = await read_object_body(request)
    if error is not None:
        return error

    if "address_audit" in body or "address_verified" in body:
        return json_response({"error": "Audyt adresu wymaga dowodu i osobnej weryfikacji"}, status=400)

    city_sym = str(body.get("city_sym") or "").strip()
    street_sym = str(body.get("street_sym") or "").strip()
    name = str(body.get("name") or "").strip()
    keyword = str(body.get("keyword") or "").strip()
    raw_latitude = body.get("latitude")
    raw_longitude = body.get("longitude")
    if not city_sym.isdigit():
        return json_response({"error": "Podaj prawidłowy kod TERYT"}, status=400)
    try:
        latitude, longitude = validated_coordinates(raw_latitude, raw_longitude)
        number = house_number(body.get("house_number"))
        power = normalize_power_supply(body.get("power_supply", {}))
    except ValueError as error:
        return json_response({"error": str(error)}, status=400)

    database_path = request.app[TERYT_DB_PATH_KEY]
    if not database_path.exists():
        return json_response({"error": "Brak lokalnego katalogu TERYT"}, status=503)
    with closing(open_teryt_database(database_path)) as connection:
        city = connection.execute(
            "SELECT nazwa FROM simc WHERE sym = ?",
            (city_sym,),
        ).fetchone()
        if city is None:
            return json_response({"error": "Nie znaleziono miejscowości w rejestrze TERYT"}, status=404)
        if street_sym:
            street = connection.execute(
                "SELECT sym_ul FROM ulic WHERE sym = ? AND sym_ul = ?",
                (city_sym, street_sym),
            ).fetchone()
            if street is None:
                return json_response({"error": "Ulica nie należy do tej miejscowości"}, status=400)

    async with request.app[OBJECTS_LOCK_KEY]:
        objects = request.app[CONFIG_KEY]["teryt_locations"]
        new_object = {
            "id": next_object_id(objects),
            "name": name or str(city["nazwa"]),
            "keyword": keyword,
            "city_sym": city_sym,
            "street_sym": street_sym,
            "latitude": latitude,
            "longitude": longitude,
        }
        new_objects = [*deepcopy(objects), new_object]
        if "house_number" in body:
            new_object["house_number"] = number
        if "power_supply" in body:
            new_object["power_supply"] = power
        new_profiles = build_location_profiles(
            new_objects, request.app[TERYT_DB_PATH_KEY]
        )
        try:
            audit = await commit_catalog_change(request, objects, new_objects, "create", new_object["id"])
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        except (OSError, sqlite3.Error) as error:
            return json_response({"error": f"Nie udało się zapisać objects.json: {error}"}, status=500)
        request.app[CONFIG_KEY]["teryt_locations"] = new_objects
        request.app[CONFIG_KEY]["location_profiles"] = new_profiles

    return json_response({"created": True, "object": new_object, **audit}, status=201)


async def delete_object_handler(request):
    object_id = request.match_info["object_id"]
    async with request.app[OBJECTS_LOCK_KEY]:
        objects = request.app[CONFIG_KEY]["teryt_locations"]
        remaining = [item for item in objects if str(item["id"]) != object_id]
        if len(remaining) == len(objects):
            return json_response({"error": "Nie znaleziono obiektu"}, status=404)
        new_profiles = build_location_profiles(
            remaining, request.app[TERYT_DB_PATH_KEY]
        )
        try:
            audit = await commit_catalog_change(request, objects, remaining, "delete", object_id)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        except (OSError, sqlite3.Error) as error:
            return json_response({"error": f"Nie udało się zapisać objects.json: {error}"}, status=500)
        request.app[CONFIG_KEY]["teryt_locations"] = remaining
        request.app[CONFIG_KEY]["location_profiles"] = new_profiles
    return json_response({"deleted": True, "id": object_id, **audit})


async def update_object_handler(request):
    body, error = await read_object_body(request)
    if error is not None:
        return error
    allowed = {"name", "keyword", "city_sym", "street_sym", "latitude", "longitude", "house_number", "power_supply"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        return json_response({"error": "Nieznane pola: " + ", ".join(unknown)}, status=400)
    if not body:
        return json_response({"error": "Podaj co najmniej jedno pole do zmiany"}, status=400)

    object_id = request.match_info["object_id"]
    async with request.app[OBJECTS_LOCK_KEY]:
        objects = request.app[CONFIG_KEY]["teryt_locations"]
        index = next((position for position, item in enumerate(objects) if str(item["id"]) == object_id), None)
        if index is None:
            return json_response({"error": "Nie znaleziono obiektu"}, status=404)
        current = deepcopy(objects[index])
        city_sym = str(body.get("city_sym", current.get("city_sym")) or "").strip()
        street_sym = str(body.get("street_sym", current.get("street_sym")) or "").strip()
        name = str(body.get("name", current.get("name")) or "").strip()
        keyword = str(body.get("keyword", current.get("keyword")) or "").strip()
        raw_latitude = body.get("latitude", current.get("latitude"))
        raw_longitude = body.get("longitude", current.get("longitude"))
        if not city_sym.isdigit():
            return json_response({"error": "Podaj prawidłowy kod TERYT"}, status=400)
        try:
            latitude, longitude = validated_coordinates(raw_latitude, raw_longitude)
            number = house_number(body.get("house_number", current.get("house_number")))
            power = merge_power_supply(current.get("power_supply", {}), body.get("power_supply", {}))
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)

        database_path = request.app[TERYT_DB_PATH_KEY]
        if not database_path.exists():
            return json_response({"error": "Brak lokalnego katalogu TERYT"}, status=503)
        with closing(open_teryt_database(database_path)) as connection:
            city = connection.execute("SELECT nazwa FROM simc WHERE sym = ?", (city_sym,)).fetchone()
            if city is None:
                return json_response({"error": "Nie znaleziono miejscowości w rejestrze TERYT"}, status=404)
            if street_sym:
                street = connection.execute(
                    "SELECT sym_ul FROM ulic WHERE sym = ? AND sym_ul = ?",
                    (city_sym, street_sym),
                ).fetchone()
                if street is None:
                    return json_response({"error": "Ulica nie należy do tej miejscowości"}, status=400)

        updated = deepcopy(current)
        if set(body) != {"power_supply"}:
            updated.update(
                name=name or str(city["nazwa"]),
                keyword=keyword,
                city_sym=city_sym,
                street_sym=street_sym,
                latitude=latitude,
                longitude=longitude,
            )
        location_changed = any(
            updated.get(key) != current.get(key)
            for key in ("city_sym", "street_sym", "latitude", "longitude")
        )
        if "house_number" in body:
            updated["house_number"] = number
        if "power_supply" in body:
            updated["power_supply"] = power
        if location_changed or number != current.get("house_number"):
            audit = deepcopy(current.get("address_audit") or {})
            audit.update(status="requires_review", review_reason="address_changed", invalidated_at=now_iso())
            updated["address_audit"] = audit
        if location_changed or number != current.get("house_number"):
            audit = deepcopy(current.get("location_audit") or {})
            audit.update(
                requires_review=True,
                review_reason="location_changed",
                invalidated_at=now_iso(),
            )
            updated["location_audit"] = audit
        new_objects = deepcopy(objects)
        new_objects[index] = updated
        new_profiles = build_location_profiles(new_objects, database_path)
        try:
            audit = await commit_catalog_change(request, objects, new_objects, "update", object_id)
        except ValueError as error:
            return json_response({"error": str(error)}, status=400)
        except (OSError, sqlite3.Error) as error:
            return json_response({"error": f"Nie udało się zapisać objects.json: {error}"}, status=500)
        request.app[CONFIG_KEY]["teryt_locations"] = new_objects
        request.app[CONFIG_KEY]["location_profiles"] = new_profiles
    return json_response({"updated": True, "object": updated, **audit})


async def index_handler(request):
    return await community.html_response(WEB_DIR / "index.html", request.app[CONFIG_KEY]['profile'])


async def readme_handler(request):
    return await community.html_response(WEB_DIR / "readme.html", request.app[CONFIG_KEY]['profile'])






async def readme_redirect_handler(request):
    raise web.HTTPPermanentRedirect("/readme")


async def snut_redirect_handler(request):
    raise web.HTTPPermanentRedirect("/snut/")


async def snut_handler(request):
    snut_dir = request.app[SNUT_DIR_KEY]
    relative_path = request.match_info.get("path") or "index.html"
    candidate = (snut_dir / relative_path).resolve()
    if not candidate.is_relative_to(snut_dir) or not candidate.is_file():
        if relative_path == "index.html":
            return web.Response(
                text="""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SNUT niedostępny</title>
  <link rel="stylesheet" href="/static/style.css?v=20260908-1">
  <link rel="stylesheet" href="/static/readme.css?v=20260908-1">
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand-mark" aria-hidden="true">OSD</div>
      <div>
        <p class="eyebrow">PULPIT SNUT</p>
        <h1>SNUT nie jest obecnie zainstalowany</h1>
      </div>
    </div>
  </header>
  <nav class="view-navigation" aria-label="Główne widoki">
    <div class="view-navigation-inner">
      <a class="view-tab" href="/"><span class="tab-icon" aria-hidden="true">M</span>Komunikaty bieżące</a>
      <a class="view-tab" href="/readme"><span class="tab-icon">R</span>README</a>
    </div>
  </nav>
  <main class="readme-main">
    <article class="readme-card">
      <h1>Brak zbudowanej wersji SNUT</h1>
      <p>Pulpit pod <code>/snut/</code> nie ma jeszcze aktywnych plików statycznych. Mapa, API i dokumentacja mogą działać dalej.</p>
      <p><a href="/">Przejdź do mapy OSD</a> albo <a href="/readme">otwórz README</a>.</p>
    </article>
  </main>
</body>
</html>""",
                status=503,
                content_type="text/html",
                charset="utf-8",
                headers={"Cache-Control": "no-store"},
            )
        raise web.HTTPNotFound()

    response = (await community.html_response(candidate, request.app[CONFIG_KEY]['profile'])
                if candidate.suffix == '.html' else web.FileResponse(candidate))
    if relative_path == "index.html" or relative_path in {
        "assets/config.yml",
        "release.json",
    }:
        response.headers["Cache-Control"] = "no-cache"
    elif relative_path.startswith("resources/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


async def events_handler(request):
    refresh_runtime_health(request.app)
    runtime = request.app[RUNTIME_KEY]
    include_raw = request.query.get("include_raw") == "1"
    visible, pending = event_visibility(request.app)
    events = sorted_public_events(visible, include_raw=include_raw)
    return json_response(
        {
            "generated_at": now_iso(),
            "last_success_at": runtime["last_success_at"],
            "error": runtime["error"],
            "count": len(events),
            "matching_pending": pending,
            "events": events,
        }
    )


async def liveness_handler(request):
    return json_response({'status':'ok'})


async def readiness_handler(request):
    ready = await request.app[READINESS_KEY].check()
    ready = ready and (publication.current() or request.app[publication.KEY].snapshot()).available
    return json_response({'status':'ready' if ready else 'unavailable'}, status=200 if ready else 503)


@web.middleware
async def request_metrics_middleware(request, handler):
    started, status = time.monotonic(), 500
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as error:
        status = error.status
        raise
    finally:
        if request.path.startswith('/api/'):
            request.app[METRICS_KEY].record(time.monotonic()-started, status)


async def operations_handler(request):
    app = request.app
    policy = publication.current() or app[publication.KEY].snapshot()
    jobs = {source:job.payload(enabled=app[START_SCANNER_KEY] and policy.allows(source, "collect")) for source,job in app[JOBS_KEY].items()}
    worker = app.get(NOTIFICATION_WORKER_KEY)
    for name, instance in (('notifications',worker),):
        if instance is None:
            continue
        instance.health.task = instance.task
        enabled = bool(app[CONFIG_KEY]['notifications_enabled'] and app[CONFIG_KEY]['our_api_url'])
        jobs[name] = instance.health.payload(enabled=enabled)
    disk = await asyncio.to_thread(shutil.disk_usage, app[CONFIG_PATH_KEY].parent)
    inodes = None
    if hasattr(os,'statvfs'):
        stats = await asyncio.to_thread(os.statvfs, app[CONFIG_PATH_KEY].parent)
        inodes = {'free':stats.f_favail,'total':stats.f_files}
    try:
        queue = await app[ENGINE_STORE_KEY].queue_operation('summary',destination_id(app[CONFIG_KEY]['our_api_url']),time.time())
        queue = {name:queue[name] for name in ('counts','oldest_pending_at','next_attempt_at','blocked_by_failure')}
    except Exception:
        queue = {'state':'storage_error'}
    queue['enabled'] = bool(app[CONFIG_KEY]['notifications_enabled'] and app[CONFIG_KEY]['our_api_url'])
    return json_response({'jobs':jobs, 'disk':{'free_bytes':disk.free,'total_bytes':disk.total,'inodes':inodes},
                          'api':app[METRICS_KEY].payload(), 'notifications':queue,
                          'backups':backup_health(),
                          'local_ready':policy.available and await app[READINESS_KEY].check(),
                          'sources':{source:{'last_success_at':health.get('last_success_at'),
                              'stale':policy.allows(source,'collect') and (bool(health.get('error')) or source_is_due(
                                  {'last_attempt_at':health.get('last_success_at')}, health['interval_seconds']*2, datetime.now().astimezone())),
                              'next_due_in_seconds':source_wait_seconds(source,health,health['interval_seconds'],datetime.now().astimezone()),
                              'error_category':health.get('error_category') if health.get('error') else None}
                              for source,health in app[RUNTIME_KEY]['sources'].items()}})


async def delivery_history_handler(request):
    try:
        query=request.query
        if set(query)-{'limit','offset','status'} or any(len(query.getall(key))>1 for key in query):
            raise ValueError('Niepoprawne lub powtórzone parametry historii powiadomień.')
        result=await request.app[ENGINE_STORE_KEY].queue_operation('history',int(query.get('limit',20)),
                                                                  int(query.get('offset',0)),query.get('status'))
        return json_response(result)
    except ValueError:
        return json_response({'error':'Sprawdź limit (od 1 do 100), offset i status dostawy.'},status=400)
    except (sqlite3.Error,EngineStoreError):
        return json_response({'error':'Historia powiadomień jest chwilowo niedostępna.'},status=503)


async def operations_token_handler(request):
    if not request.app[OPERATIONS_TOKEN_KEY].accepts(request.headers.get('Authorization','')):
        return json_response({'error':'Wymagany token odczytu stanu operacyjnego.'},status=401)
    return await operations_handler(request)


async def backup_snapshot_handler(request):
    staging = os.environ.get('PGE_BACKUP_STAGING')
    if not staging:
        return json_response({'error':'Tworzenie migawek nie jest skonfigurowane.'},status=503)
    stage_path = Path(staging).resolve()
    if stage_path.is_relative_to(BASE_DIR/'web') or stage_path.is_relative_to(request.app[SNUT_DIR_KEY]):
        return json_response({'error':'Katalog kopii nie może być publicznym katalogiem aplikacji.'},status=503)
    if request.query or request.can_read_body:
        return json_response({'error':'Migawka nie przyjmuje ścieżek ani parametrów.'},status=400)
    app = request.app
    # Nie kolejkuj wielu kosztownych kopii ani nie obchodź blokad osobnym procesem.
    lock = app[BACKUP_LOCK_KEY]
    if lock.locked():
        return json_response({'error':'Tworzenie migawki już trwa.'},status=409)
    async with lock, app[OBJECTS_LOCK_KEY], app[SCAN_LOCK_KEY]:
        root = app[CONFIG_PATH_KEY].parent
        policy = publication.current() or app[publication.KEY].snapshot()
        if policy.enforced and not (root/'data/publication-policy.sqlite3').is_file():
            return json_response({'error':'Brakuje wymaganego magazynu polityki publikacji.'},status=503)
        files = {'config.json':app[CONFIG_PATH_KEY], 'objects.json':app[OBJECTS_PATH_KEY]}
        files.update({'data/'+name:root/'data'/name for name in DATABASES})
        files['data/engine.sqlite3'] = app[ENGINE_STORE_KEY].database_path
        files['data/teryt.db'] = app[TERYT_DB_PATH_KEY]
        optional = ('data/geoportal-teryt-context.json','data/publication-policy.sqlite3')
        missing = [name for name in optional if not (root/name).is_file()]
        files.update({name:root/name for name in optional if name not in missing})
        try:
            managed, missing_folders = await asyncio.to_thread(private_files, root)
            files.update(managed)
            missing.extend(missing_folders)
        except (OSError,ValueError):
            return json_response({'error':'Nie udało się odczytać pełnego magazynu raportów i dowodów.'},status=503)
        identifier = str(uuid.uuid4())
        task = asyncio.get_running_loop().run_in_executor(None, lambda: create_snapshot(
            files, Path(staging)/identifier, missing_optional=missing,
            revision=app[STORAGE_STATUS_KEY]['revision']))
        cancelled = False
        try:
            while True:
                try:
                    manifest = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError()
            return json_response({'snapshot_id':identifier,'scope':manifest['scope'],
                                  'started_at':manifest['started_at'],'completed_at':manifest['completed_at']})
        except (OSError,ValueError,sqlite3.Error,TimeoutError):
            return json_response({'error':'Nie udało się utworzyć kompletnej migawki aplikacji.'},status=503)


async def backup_token_handler(request):
    if not request.app[BACKUP_TOKEN_KEY].accepts(request.headers.get('Authorization','')):
        return json_response({'error':'Wymagany token tworzenia kopii.'},status=401)
    return await backup_snapshot_handler(request)


async def status_handler(request):
    runtime = request.app[RUNTIME_KEY]
    config = request.app[CONFIG_KEY]
    refresh_runtime_health(request.app)
    worker = request.app.get(NOTIFICATION_WORKER_KEY)
    try:
        notifications = await request.app[ENGINE_STORE_KEY].queue_operation('summary', destination_id(config['our_api_url']), time.time())
        notifications.update(worker_state=worker.state if worker else 'stopped', worker_error=worker.error if worker else None)
    except Exception as error:
        notifications = {'worker_state':'storage_error', 'worker_error':type(error).__name__, 'counts':{}}
    notifications['enabled'] = bool(config['notifications_enabled'] and config['our_api_url'])
    notifications['destination_configured'] = bool(config['our_api_url'])
    policy = publication.current()
    sources = {source: deepcopy(runtime["sources"][source]) for source in config["enabled_sources"] if policy.allows(source)}
    for health in sources.values():
        health["stale"] = bool(health.get("error")) or source_is_due(
            {"last_attempt_at": health.get("last_success_at")},
            health["interval_seconds"] * 2,
            datetime.now().astimezone(),
        )
    return json_response(
        {
            "ok": not any(health["stale"] or health.get("matching_pending_count") for health in sources.values()),
            "scan_running": runtime["scan_running"],
            "last_scan_started_at": runtime["last_scan_started_at"],
            "last_scan_at": runtime["last_scan_at"],
            "last_success_at": runtime["last_success_at"],
            "error": runtime["error"],
            "source_event_count": sum(item.get("source_event_count", 0) for item in sources.values()),
            "matched_event_count": len(event_visibility(request.app)[0]),
            "last_change_count": 0,
            "configured_locations": len(policy.objects(config["teryt_locations"])),
            "scan_interval_seconds": config["scan_interval_seconds"],
            "enabled_sources": list(sources),
            "publication": {"revision": policy.token, "sources": policy.public_sources()},
            "sources": sources,
            "notifications_enabled": bool(config.get("notifications_enabled", False) and config["our_api_url"]),
            "last_notifications": notifications.pop('recent', []),
            "notifications": notifications,
            "diagnostics": {"degraded": request.app[DIAGNOSTICS_KEY].error is not None, "error_code": request.app[DIAGNOSTICS_KEY].error},
            "storage": {
                "backend": "sqlite",
                "schema_version": SCHEMA_VERSION,
                "revision": request.app[STORAGE_STATUS_KEY]["revision"],
                "state_json_export_ok": request.app[STORAGE_STATUS_KEY]["state_json_export_error"] is None,
                "state_json_export_error": request.app[STORAGE_STATUS_KEY]["state_json_export_error"],
            },
        }
    )


async def startup(app):
    await app[DIAGNOSTICS_KEY].start()
    if app[CONFIG_KEY]["profile"] == "demo":
        from tools.init_demo import refresh
        await refresh(app[CONFIG_PATH_KEY].parent, application=app)
        async def scenario_clock():
            while True:
                await asyncio.sleep(3600)
                async with app[OBJECTS_LOCK_KEY]:
                    await refresh(app[CONFIG_PATH_KEY].parent, application=app)
        app[SCANNER_TASK_KEY] = asyncio.create_task(scenario_clock())
        return
    timeout = aiohttp.ClientTimeout(total=app[CONFIG_KEY]["request_timeout_seconds"])
    app[SESSION_KEY] = aiohttp.ClientSession(
        timeout=timeout,
        trace_configs=[source_trace()],
        headers={"User-Agent": "Monitor-OSD-Community/0.1 (+https://github.com/Ankudev/Monitor_OSD_Community)"},
    )
    config = app[CONFIG_KEY]
    app[GEOCODER_KEY] = Geocoder(
        app[SESSION_KEY],
        app[CONFIG_PATH_KEY].parent / "data" / "geocoding-cache.sqlite3",
        provider_url=config["geocoding_provider_url"],
        user_agent=config["geocoding_user_agent"],
        timeout_seconds=config["geocoding_timeout_seconds"],
        success_ttl_seconds=config["geocoding_success_ttl_seconds"],
        empty_ttl_seconds=config["geocoding_empty_ttl_seconds"],
        max_entries=config["geocoding_cache_max_entries"],
        max_pending=config["geocoding_max_pending"],
    )
    await app[GEOCODER_KEY].start()
    if config["geoportal_enabled"]:
        app[GEOPORTAL_KEY] = Geoportal(app[SESSION_KEY], app[CONFIG_PATH_KEY].parent / "data" / "geocoding-cache.sqlite3", config["geocoding_user_agent"])
        await app[GEOPORTAL_KEY].start()
    async def eligibility(job):
        async with app[OBJECTS_LOCK_KEY]:
            current = app[CONFIG_KEY]
            if job['source'] not in current['enabled_sources']:
                return 'Źródło wyłączone'
            location = next((x for x in current['teryt_locations'] if str(x['id']) == job['object_id']), None)
            if location is None:
                return 'Obiekt usunięty'
            view = app[publication.KEY].snapshot()
            if not view.allows(job["source"], "notify") or view.public_object(location) is None:
                return "Powiadomienia ograniczone decyzją publikacji"
            if (location.get('location_audit') or {}).get('requires_review'):
                return 'Lokalizacja wymaga przeglądu'
            if object_fingerprints(current, job['source']).get(job['object_id']) != job['object_fingerprint']:
                return 'Adres lub GPS obiektu zmieniony'
            pilot = current['notification_object_ids']
            if pilot and job['object_id'] not in pilot:
                return 'Obiekt poza pilotem'
        return None
    worker = NotificationWorker(app[ENGINE_STORE_KEY], lambda: app[CONFIG_KEY], eligibility)
    app[NOTIFICATION_WORKER_KEY] = worker
    await worker.start()
    if app[START_SCANNER_KEY]:
        app[SCANNER_TASK_KEY] = asyncio.create_task(scanner_loop(app))


async def cleanup(app):
    task = app.get(SCANNER_TASK_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    worker = app.get(NOTIFICATION_WORKER_KEY)
    if worker is not None:
        await worker.close()
    geocoder = app.get(GEOCODER_KEY)
    if geocoder is not None:
        await geocoder.close()
    session = app.get(SESSION_KEY)
    geoportal = app.get(GEOPORTAL_KEY)
    if geoportal is not None:
        await geoportal.close()
    if session is not None:
        await session.close()
    engine_store = app.get(ENGINE_STORE_KEY)
    if engine_store is not None:
        await engine_store.close()


def create_app(
    config_path=CONFIG_PATH,
    state_path=STATE_PATH,
    objects_path=None,
    teryt_db_path=None,
    snut_dir=None,
    *,
    start_scanner=True,
    engine_db_path=None,
    publication_policy=None,
):
    config = load_config(config_path)
    if config["profile"] == "demo":
        community.validate_demo_config(config)
        start_scanner = False
    policy = publication_policy or publication.PublicationPolicy(config_path.parent / "data/publication-policy.sqlite3")
    actual_objects_path = objects_path or config_path.parent / "objects.json"
    actual_teryt_db_path = teryt_db_path or config_path.parent / "data" / "teryt.db"
    actual_snut_dir = Path(snut_dir or SNUT_DIR).resolve()
    catalog_journal = CatalogJournal(config_path.parent / "data/catalog-journal.sqlite3", actual_objects_path)
    config["teryt_locations"] = load_objects(
        actual_objects_path,
        config["teryt_locations"],
    )
    config["location_profiles"] = build_location_profiles(
        config["teryt_locations"], actual_teryt_db_path
    )
    actual_engine_db_path = Path(engine_db_path or state_path.parent / "data" / "engine.sqlite3")
    engine_store = EngineStore(actual_engine_db_path, state_path)
    state, state_revision = engine_store.load_or_migrate(lambda: load_state(state_path))
    if engine_store.export_error:
        logger.error("Nie udało się odświeżyć eksportu state.json: %s", engine_store.export_error)
    initial_events = state.get("events", {})

    app = web.Application(middlewares=[community.guard(config["profile"]), public_origin.redirect_middleware(), admin_auth.security_middleware, request_metrics_middleware, publication.middleware])
    admin_auth.register(app)
    app[publication.KEY] = policy
    app[CONFIG_KEY] = config
    app[CONFIG_PATH_KEY] = config_path
    app[STATE_PATH_KEY] = state_path
    app[ENGINE_STORE_KEY] = engine_store
    app[READINESS_KEY] = Readiness(engine_store.load_current)
    app[METRICS_KEY] = RequestMetrics()
    app[OPERATIONS_TOKEN_KEY] = OperationsToken()
    app[BACKUP_TOKEN_KEY] = OperationsToken('PGE_BACKUP_TOKEN_FILE')
    app[DIAGNOSTICS_KEY] = Diagnostics(actual_engine_db_path.parent / "diagnostics.sqlite3")
    app[QUALITY_KEY] = QualityCache(actual_teryt_db_path)
    app[STORAGE_STATUS_KEY] = {
        "revision": state_revision,
        "state_json_export_error": engine_store.export_error,
    }
    app[OBJECTS_PATH_KEY] = actual_objects_path
    app[CATALOG_JOURNAL_KEY] = catalog_journal
    app[TERYT_DB_PATH_KEY] = actual_teryt_db_path
    app[REVIEW_STORE_KEY] = ReviewStore(config_path.parent / "data")
    app[SNUT_DIR_KEY] = actual_snut_dir
    app[STATE_KEY] = state
    app[START_SCANNER_KEY] = start_scanner
    app[SCAN_LOCK_KEY] = asyncio.Lock()
    app[BACKUP_LOCK_KEY] = asyncio.Lock()
    app[SOURCE_LOCKS_KEY] = {source: asyncio.Lock() for source in config["enabled_sources"]}
    app[JOBS_KEY] = {source:JobHealth() for source in config['enabled_sources']}
    app[OBJECTS_LOCK_KEY] = asyncio.Lock()
    app[RUNTIME_KEY] = {
        "events": initial_events,
        "scan_running": False,
        "last_scan_started_at": None,
        "last_scan_at": state.get("saved_at"),
        "last_success_at": state.get("saved_at"),
        "error": None,
        "source_event_count": 0,
        "matched_event_count": len(initial_events),
        "last_change_count": 0,
        "last_notifications": [],
        "sources": {
            source: {
                "source": source,
                "label": SOURCE_LABELS[source],
                "interval_seconds": config["source_scan_intervals_seconds"][source],
                "last_attempt_at": state.get("sources", {}).get(source, {}).get("last_attempt_at"),
                "last_success_at": state.get("sources", {}).get(source, {}).get("last_success_at"),
                **{key: state.get('sources', {}).get(source, {}).get(key) for key in PUBLICATION_FIELDS},
                "error": state.get("sources", {}).get(source, {}).get("error"),
                "error_category": state.get("sources", {}).get(source, {}).get("error_category"),
                "last_error": state.get("sources", {}).get(source, {}).get("last_error"),
                "last_error_at": state.get("sources", {}).get(source, {}).get("last_error_at"),
                "recovered_at": state.get("sources", {}).get(source, {}).get("recovered_at"),
                "blocked_until": state.get("sources", {}).get(source, {}).get("blocked_until"),
                "consecutive_errors": int(
                    state.get("sources", {}).get(source, {}).get("consecutive_errors") or 0
                ),
                "source_event_count": int(
                    state.get("sources", {}).get(source, {}).get("source_event_count") or 0
                ),
                "matched_event_count": sum(
                    1
                    for event in initial_events.values()
                    if event_source(event) == source
                ),
                "running": False,
            }
            for source in config["enabled_sources"]
        },
    }

    refresh_runtime_health(app)
    app.router.add_get("/", index_handler)
    app.router.add_get("/readme", readme_handler)
    app.router.add_get("/readme/", readme_redirect_handler)
    app.router.add_get("/snut", snut_redirect_handler)
    app.router.add_get("/SNUT", snut_redirect_handler)
    app.router.add_get("/SNUT/", snut_redirect_handler)
    app.router.add_get("/snut/", snut_handler)
    app.router.add_get("/snut/{path:.*}", snut_handler)
    app.router.add_get("/api/events", events_handler)
    app.router.add_get("/api/status", status_handler)
    app.router.add_get("/health/live", liveness_handler)
    app.router.add_get("/health/ready", readiness_handler)
    app.router.add_get("/api/admin/operations", operations_handler)
    app.router.add_get("/api/admin/notifications", delivery_history_handler)
    from onesignal_push import register as register_push
    register_push(app,BASE_DIR)
    app.router.add_get("/api/operations", operations_token_handler)
    app.router.add_get("/api/admin/check", admin_check_handler)
    app.router.add_post("/api/admin/backups/snapshot", backup_snapshot_handler)
    app.router.add_post("/api/operations/snapshot", backup_token_handler)
    app.router.add_get("/api/admin/publication", publication_handler)
    app.router.add_get("/api/admin/publication/setup", publication_handler)
    app.router.add_put("/api/admin/publication/{kind}/{identifier}", publication_handler)
    app.router.add_get("/api/publication", publication_public_handler)
    app.router.add_get("/api/admin/objects", objects_handler)
    app.router.add_post("/api/admin/objects", create_object_handler)
    app.router.add_patch("/api/admin/objects/{object_id}", update_object_handler)
    app.router.add_delete("/api/admin/objects/{object_id}", delete_object_handler)
    app.router.add_get("/api/objects", objects_handler)
    app.router.add_post("/api/objects", create_object_handler)
    app.router.add_delete("/api/objects/{object_id}", delete_object_handler)
    app.router.add_get("/api/location/resolve", resolve_location_handler)
    app.router.add_get("/api/location/reverse", reverse_location_handler)
    app.router.add_post("/api/location/check", location_check_handler)
    app.router.add_route("OPTIONS", "/api/location/check", location_check_handler)
    app.router.add_get("/api/teryt/search", teryt_search_handler)
    app.router.add_get("/api/teryt/{city_sym:\\d+}/streets", streets_handler)
    register_api_v1(
        app,
        {
            "root": BASE_DIR,
            "location_review": lambda obj: app[REVIEW_STORE_KEY].read(obj),
            "config": lambda: app[CONFIG_KEY],
            "runtime": lambda: app[RUNTIME_KEY],
            "state": lambda: app[STATE_KEY],
            "engine_store": app[ENGINE_STORE_KEY],
            "diagnostics": app[DIAGNOSTICS_KEY],
            "quality": app[QUALITY_KEY],
            "capture_export": lambda: capture_export_context(app),
            "visibility": lambda: event_visibility(app),
            "visible_events": lambda: event_visibility(app)[0],
            "matching_pending": lambda: event_visibility(app)[1],
            "policy": policy,
            "private_objects": lambda: deepcopy(app[CONFIG_KEY]["teryt_locations"]),
            "objects": lambda: (publication.current() or policy.snapshot()).objects(enriched_objects(
                app[CONFIG_KEY]["teryt_locations"], app[TERYT_DB_PATH_KEY]
            )),
            "locality_details": lambda city_syms: teryt_locality_details(
                city_syms, app[TERYT_DB_PATH_KEY]
            ),
            "event_time_phase": event_time_phase,
            "event_time_windows": event_time_windows,
            "source_wait_seconds": source_wait_seconds,
        },
    )
    app.router.add_static("/static", WEB_DIR)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


async def scan_once():
    config = load_config()
    config["teryt_locations"] = load_objects(OBJECTS_PATH, config["teryt_locations"])
    config["location_profiles"] = build_location_profiles(
        config["teryt_locations"], TERYT_DB_PATH
    )
    timeout = aiohttp.ClientTimeout(total=config["request_timeout_seconds"])
    async with aiohttp.ClientSession(
        timeout=timeout,
        trace_configs=[source_trace()],
        headers={"User-Agent": "Monitor-OSD-Community/0.1 (+https://github.com/Ankudev/Monitor_OSD_Community)"},
    ) as session:
        results = await asyncio.gather(
            *(
                fetch_source(session, source, config)
                for source in config["enabled_sources"]
            ),
            return_exceptions=True,
        )
        summary = {}
        total_matched = 0
        for source, result in zip(config["enabled_sources"], results):
            if isinstance(result, Exception):
                summary[source] = {"ok": False, "error": str(result)}
                continue
            matched = normalize_source_events(result, config)
            total_matched += len(matched)
            summary[source] = {
                "ok": True,
                "source_events": len(result),
                "matched_events": len(matched),
            }
        print(
            json.dumps(
                {
                    "sources": summary,
                    "matched_events": total_matched,
                    "notifications_enabled": bool(config.get("notifications_enabled", False) and config["our_api_url"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if all(item.get("ok") for item in summary.values()) else 1


def main():
    parser = argparse.ArgumentParser(description="Monitor OSD Community")
    parser.add_argument('--profile', choices=community.PROFILES, default='demo')
    parser.add_argument('--data-dir', type=Path, default=BASE_DIR/'.runtime/demo')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--snut-dir', type=Path,
                        default=BASE_DIR/'snut' if (BASE_DIR/'snut/index.html').is_file()
                        else BASE_DIR/'.deployment/snut-build')
    args = parser.parse_args()
    data = args.data_dir.resolve()
    if not (data/'config.json').is_file():
        parser.error('Brak konfiguracji. Uruchom tools/init_demo.py albo przygotuj własny katalog danych.')
    config = load_config(data/'config.json')
    if config['profile'] != args.profile:
        parser.error('Profil nie zgadza się z katalogiem danych. Użyj osobnego katalogu dla każdego profilu.')
    if args.profile == 'live':
        objects = load_objects(data/'objects.json', config['teryt_locations'])
        if any(str(o['id']).startswith('demo-') or str(o['city_sym']).startswith('990') for o in objects):
            parser.error('Danych demonstracyjnych nie wolno wysyłać do rzeczywistych źródeł.')
    if args.profile == 'demo':
        from tools.init_demo import refresh
        asyncio.run(refresh(data))
    web.run_app(create_app(data/'config.json', data/'state.json', snut_dir=args.snut_dir,
                           start_scanner=args.profile == 'live'),
                host=public_origin.bind_hosts(args.host), port=args.port)


if __name__ == "__main__":
    main()
