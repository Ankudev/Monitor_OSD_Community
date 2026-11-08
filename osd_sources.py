"""Publiczne źródła wyłączeń pięciu głównych OSD.

Moduł nie zakłada stabilnego, zakontraktowanego API. Każdy adapter zwraca ten
sam, niewielki model zdarzenia, a surowy rekord zachowuje w polu ``raw``.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from zoneinfo import ZoneInfo

import aiohttp

from geometry import normalize_geometry
import pge_credentials
from pge_request_budget import RequestBudget
from event_history import parse_event_datetime, event_time_warning
from scan_diagnostics import error_details, response_metadata, publication_metadata, warn
from scan_diagnostics import SourceMaintenanceError
from scan_diagnostics import measured, parser, request_attempt, request_metrics, response_redirects, uncertain_request_count, source_trace, completed, SourceContractError, SourceRequestBudgetError

POLAND_TIMEZONE = ZoneInfo("Europe/Warsaw")

PGE_URL = "https://power-outage.gkpge.pl/api/power-outage"
TAURON_URL = "https://www.tauron-dystrybucja.pl/waapi/outages/items"
ENERGA_PAGE_URL = (
    "https://energa-operator.pl/uslugi/awarie-i-wylaczenia/wylaczenia-biezace"
)
STOEN_BASE_URL = "https://awaria.stoen.pl"
ENEA_BASE_URL = "https://wylaczenia.operator.enea.pl/"

SOURCE_LABELS = {
    "pge": "PGE Dystrybucja",
    "tauron": "TAURON Dystrybucja",
    "energa": "ENERGA-OPERATOR",
    "enea": "ENEA Operator",
    "stoen": "Stoen Operator",
}
DEFAULT_SOURCES = tuple(SOURCE_LABELS)
DEFAULT_SOURCE_INTERVALS = {
    "pge": 3600,
    "tauron": 300,
    "energa": 300,
    "stoen": 600,
    "enea": 1800,
}

ENEA_MAX_CONCURRENT_REQUESTS = 4


class SourceHttpError(RuntimeError):
    """Błąd HTTP zachowujący status i termin wskazany przez operatora."""

    def __init__(self, message, *, status, retry_after=None):
        super().__init__(message)
        self.status = int(status)
        self.retry_after = retry_after


def parse_retry_after(value, *, now=None):
    text = str(value or "").strip()
    if not text:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        seconds = int(text)
    except ValueError:
        try:
            result = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    return current.astimezone(timezone.utc) + timedelta(seconds=max(0, seconds))
PLANNED_LOOKAHEAD_DAYS = 14


def _clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _safe_url_label(url):
    parsed = urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").split("?", 1)[0][:120]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _error_message(error):
    name = type(error).__name__
    text = str(error).strip()
    if not text and getattr(error, "__cause__", None) is not None:
        text = _error_message(error.__cause__)
    return f"{name}: {text}" if text and text != name else name


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _point(lat, lon):
    latitude = _number(lat)
    longitude = _number(lon)
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _dedupe_points(points):
    result = []
    seen = set()
    for point in points:
        if not point:
            continue
        key = (round(point["latitude"], 7), round(point["longitude"], 7))
        if key in seen:
            continue
        seen.add(key)
        result.append(point)
    return result


def _require_dict(value, source, context):
    if not isinstance(value, dict):
        raise SourceContractError(f"{source}: niepoprawny format odpowiedzi ({context})")
    return value


def _require_list(value, source, context):
    if not isinstance(value, list):
        raise SourceContractError(f"{source}: niepoprawny format odpowiedzi ({context})")
    return value


def _require_list_field(value, source, context):
    if value is None:
        raise SourceContractError(f"{source}: brak wymaganej listy ({context})", code='incomplete')
    return _require_list(value, source, context)


def _periods(raw_periods, start_at=None, stop_at=None):
    result = []
    for period in raw_periods or []:
        if not isinstance(period, dict):
            continue
        start = period.get("startAt") or period.get("fromDate") or period.get("start")
        stop = period.get("stopAt") or period.get("toDate") or period.get("end")
        if start or stop:
            result.append({"startAt": start, "stopAt": stop})
    if not result and (start_at or stop_at):
        result.append({"startAt": start_at, "stopAt": stop_at})
    return result


def canonical_event(
    source,
    source_event_id,
    kind,
    *,
    status="active",
    description="",
    start_at=None,
    stop_at=None,
    periods=None,
    updated_at=None,
    addresses=None,
    coordinates=None,
    polygons=None,
    geometry=None,
    center=None,
    radius_m=None,
    county_codes=None,
    affected_customers=None,
    source_url="",
    raw=None,
):
    identifier = str(source_event_id or "").strip().lower()
    if not identifier:
        stable = json.dumps(raw or {}, ensure_ascii=False, sort_keys=True)
        identifier = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
    return {
        "source": source,
        "operator": SOURCE_LABELS[source],
        "source_event_id": identifier,
        "type": kind,
        "status": status,
        "description": _clean_text(description),
        "start_at": start_at,
        "stop_at": stop_at,
        "periods": _periods(periods, start_at, stop_at),
        "updated_at": updated_at,
        "addresses": deepcopy(addresses or []),
        "coordinates": _dedupe_points(coordinates or []),
        "polygons": deepcopy(polygons or []),
        **({"geometry": deepcopy(geometry)} if geometry is not None else {}),
        "center": deepcopy(center),
        "radius_m": _number(radius_m),
        "county_codes": [str(value).zfill(4) for value in county_codes or []],
        "affected_customers": affected_customers,
        "source_url": source_url,
        "raw": deepcopy(raw or {}),
    }


async def _request_text(session, method, url, *, retries=3, sensitive_response=False, **kwargs):
    last_error = None
    safe_url = _safe_url_label(url)
    for attempt in range(1, retries + 1):
        try:
            request_attempt(attempt, url)
            metrics = request_metrics(session)
            request_kwargs = dict(kwargs)
            if metrics is not None: request_kwargs['trace_request_ctx'] = metrics
            with measured("fetch"):
                async with session.request(method, url, **request_kwargs) as response:
                    response_redirects(response, metrics)
                    completed('http_responses')
                    body = None
                    try:
                        if isinstance(response, aiohttp.ClientResponse):
                            body = await response.read()
                        text = await response.text()
                    finally:
                        # text() używa ciała już odczytanego przez read(). Starsze
                        # atrapy z samym text() nie dostarczają dowodu bajtowego.
                        content_type = getattr(response, 'headers', {}).get('Content-Type')
                        if sensitive_response:
                            # Odpowiedź może odbić sekret również w nagłówku.
                            content_type = 'application/json' if str(content_type).split(';')[0].lower() == 'application/json' else None
                        response_metadata(response.status, content_type, body,
                                          omit_hash=sensitive_response)
                    if response.status != 200:
                        message = f"{method} {safe_url} zwrócił HTTP {response.status}"
                        retry_after = parse_retry_after(
                            getattr(response, "headers", {}).get("Retry-After")
                        )
                        http_error = SourceHttpError(
                            message,
                            status=response.status,
                            retry_after=retry_after,
                        )
                        # Ponawianie 4xx, zwłaszcza 403/429, zwiększa ruch mimo
                        # jawnej odmowy serwera. Następna próba należy do
                        # harmonogramu źródła z dłuższym odstępem.
                        if 400 <= response.status < 500 or response.status == 503:
                            raise http_error
                        raise http_error
                    return text
        except SourceHttpError as error:
            last_error = error
            if 400 <= error.status < 500 or error.status == 503 or (sensitive_response and attempt == retries):
                raise
            if attempt < retries:
                await asyncio.sleep(attempt * 1.5)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            if not isinstance(error, SourceRequestBudgetError): uncertain_request_count()
            last_error = error
            if attempt < retries:
                await asyncio.sleep(attempt * 1.5)
        except asyncio.CancelledError:
            uncertain_request_count()
            raise
    raise RuntimeError(
        f"{method} {safe_url} nie powiodło się po {retries} próbach: {_error_message(last_error)}"
    ) from last_error


async def _request_json(session, method, url, **kwargs):
    text = await _request_text(session, method, url, **kwargs)
    try:
        with measured("parse"):
            return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Niepoprawny JSON z {_safe_url_label(url)}") from error


def _require_boolean(raw, field, source):
    value = raw.get(field)
    if type(value) is not bool:
        raise SourceContractError(f"{source}: pole {field} musi być wartością true albo false")
    return value


def _validate_pge_addresses(raw):
    addresses = raw.get("addresses")
    if not isinstance(addresses, list):
        raise SourceContractError("PGE: rekord bez pełnej listy adresów")
    for index, address in enumerate(addresses, 1):
        if not isinstance(address, dict):
            raise SourceContractError(f"PGE: adres {index} nie jest obiektem")
        teryt = address.get("teryt")
        if teryt is None:
            continue
        if not isinstance(teryt, dict):
            raise SourceContractError(f"PGE: adres {index}, TERYT nie jest obiektem ani null")
        for field in ("citySym", "streetSymUl"):
            code = teryt.get(field)
            if code is None:
                continue
            if not isinstance(code, str) or (code.strip() and not re.fullmatch(r"[0-9]+", code.strip())):
                raise SourceContractError(f"PGE: adres {index}, kod {field} musi być tekstem cyfr lub pusty")
    return addresses


@parser
def normalize_pge(raw):
    revoked = _require_boolean(raw, "revoked", "PGE")
    addresses = deepcopy(_validate_pge_addresses(raw))
    value = str(raw.get("type") or "")
    kind = {"1": "outage", "2": "planned_outage", "9": "mass_outage"}.get(
        value, "unknown"
    )
    coordinates = []
    for point in raw.get("coordinates") or []:
        if isinstance(point, dict):
            coordinates.append(_point(point.get("latitude"), point.get("longitude")))
    status = "cancelled" if revoked else "active"
    return canonical_event(
        "pge",
        raw.get("uuid") or raw.get("id"),
        kind,
        status=status,
        description=raw.get("description"),
        start_at=raw.get("startAt"),
        stop_at=raw.get("stopAt"),
        periods=raw.get("dateRanges"),
        updated_at=raw.get("updatedAt"),
        addresses=addresses,
        coordinates=coordinates,
        source_url=PGE_URL,
        raw=raw,
    )


async def _fetch_pge_response(session, _config, *, token=None):
    now = datetime.now(POLAND_TIMEZONE)
    params = {
        "startAtTo": (now + timedelta(days=PLANNED_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
        "stopAtFrom": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if token is None:
        all_data = await _request_json(session, "GET", PGE_URL, params=params, retries=1, allow_redirects=False)
    else:
        all_data = await _request_json(session, "GET", PGE_URL, params=params, retries=1, allow_redirects=False,
                                       sensitive_response=True,
                                       headers={'X-Auth-Token': token, 'Content-Type': 'application/json'})
        if _contains_secret(all_data, token):
            raise pge_credentials.PgeAccessError('PGE: odpowiedź zawiera poświadczenie; odrzucono ją bez publikacji.')
    if not isinstance(all_data, list):
        raise SourceContractError("PGE: zamiast pełnej listy otrzymano inny format")
    by_id = {}
    for index, raw in enumerate(all_data, 1):
        if not isinstance(raw, dict) or not (raw.get("uuid") or raw.get("id")):
            raise SourceContractError("PGE: rekord bez identyfikatora")
        try:
            event = normalize_pge(raw)
        except ValueError as error:
            raise SourceContractError(f"PGE: rekord {index}: {error}") from error
        previous = by_id.get(event["source_event_id"])
        if previous is not None and previous != event:
            raise SourceContractError("PGE: sprzeczne wersje tego samego zdarzenia, ponowię odczyt", code='conflicting_id')
        by_id[event["source_event_id"]] = event
    return list(by_id.values())


def _contains_secret(value, token):
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(_contains_secret(key, token) or _contains_secret(item, token) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, token) for item in value)
    return False


async def fetch_pge(session, config):
    if config.get('profile') == 'demo':
        raise SourceContractError('Profil demonstracyjny nie pobiera danych PGE.')
    if pge_credentials.enabled(config.get('profile')):
        token = pge_credentials.read_token()
        # Adres nie pochodzi z konfiguracji ani żądania użytkownika.
        if PGE_URL != 'https://power-outage.gkpge.pl/api/power-outage':
            raise pge_credentials.PgeAccessError('PGE: odmowa wysłania poświadczenia pod inny adres.')
        try:
            with RequestBudget().attempt():
                # Nie kopiujemy nagłówków, proxy ani cookies ze wspólnej sesji.
                timeout = aiohttp.ClientTimeout(total=60)
                try:
                    async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                            cookie_jar=aiohttp.DummyCookieJar(),
                            trace_configs=[source_trace(single_request=True)]) as controlled:
                        return await _fetch_pge_response(controlled, config, token=token)
                except (SourceHttpError, pge_credentials.PgeAccessError):
                    raise
                except Exception as error:
                    # Klasyfikacja musi powstać przed obsługą błędów magazynu budżetu.
                    details = error_details(error)
                    raise pge_credentials.PgeAccessError(
                        f"PGE: {details['message']} Zachowano poprzedni wynik.", details['error_code']
                    ) from None
        except SourceHttpError as error:
            raise SourceHttpError(f'PGE: serwer zwrócił HTTP {error.status}. Zachowano poprzednie dane.',
                                  status=error.status, retry_after=error.retry_after) from None
    if not isinstance(session, aiohttp.ClientSession):
        return await _fetch_pge_response(session, config)
    async with aiohttp.ClientSession(timeout=session.timeout, headers=dict(session.headers),
                                   cookie_jar=aiohttp.DummyCookieJar(),
                                   trace_configs=[source_trace(single_request=True)]) as controlled:
        return await _fetch_pge_response(controlled, config)


@parser
def normalize_tauron(raw):
    active = _require_boolean(raw, "IsActive", "TAURON")
    kind = {1: "planned_outage", 2: "outage"}.get(raw.get("TypeId"), "unknown")
    coordinates = []
    for value in raw.get("Coordinates") or []:
        if isinstance(value, dict):
            coordinates.append(_point(value.get("lat"), value.get("lng")))
    center_raw = raw.get("Center") or {}
    center = _point(center_raw.get("lat"), center_raw.get("lng"))
    return canonical_event(
        "tauron",
        raw.get("OutageId"),
        kind,
        status="active" if active else "resolved",
        description=raw.get("Message"),
        start_at=raw.get("StartDate"),
        stop_at=raw.get("EndDate"),
        updated_at=raw.get("Modified"),
        addresses=[{"text": _clean_text(raw.get("Message"))}],
        coordinates=coordinates,
        center=center,
        # Klient TAURON v5.6.32 rysuje zastępcze okręgi stałym 1000 m;
        # nie używa Radius rekordu. Brak kontraktu zasięgu sieci tego pola.
        # Zachowujemy oryginał w raw, ale nie przypisujemy całego promienia do obiektu.
        radius_m=None,
        source_url="https://www.tauron-dystrybucja.pl/wylaczenia",
        raw=raw,
    )


async def fetch_tauron(session, _config):
    now = datetime.now(timezone.utc)
    data = await _request_json(
        session,
        "GET",
        TAURON_URL,
        params={
            "fromDate": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "toDate": (now + timedelta(days=PLANNED_LOOKAHEAD_DAYS)).isoformat().replace("+00:00", "Z"),
        },
    )
    events = []
    for index, raw in enumerate(_require_list(data, "TAURON", "lista wyłączeń"), 1):
        if not isinstance(raw, dict):
            raise SourceContractError("TAURON: rekord nie jest obiektem")
        if not raw.get("OutageId"):
            raise SourceContractError("TAURON: rekord bez identyfikatora")
        try:
            events.append(normalize_tauron(raw))
        except ValueError as error:
            raise SourceContractError(f"TAURON: rekord {index}: {error}") from error
    return events


@parser
def normalize_energa(raw):
    polygon = raw.get("polygon")
    if polygon is None:
        polygon = {}
    if not isinstance(polygon, dict):
        raise SourceContractError("ENERGA: niepoprawna geometria obszaru")
    poly = polygon.get("poly")
    centroid = polygon.get("centroid") or {}
    centroid_coordinates = centroid.get("coordinates") or []
    center = (
        _point(centroid_coordinates[1], centroid_coordinates[0])
        if len(centroid_coordinates) >= 2
        else None
    )
    geometry = normalize_geometry(poly, legacy_energa=True) if poly is not None else None
    addresses = [{"text": _clean_text(value)} for value in raw.get("areas") or []]
    if raw.get("message"):
        addresses.append({"text": _clean_text(raw.get("message"))})
    return canonical_event(
        "energa",
        raw.get("guid"),
        {1: "outage", 2: "planned_outage", 3: "mass_outage"}.get(
            raw.get("shutdownType"), "unknown"
        ),
        description=raw.get("message"),
        start_at=raw.get("startDate"),
        stop_at=raw.get("endDate"),
        periods=raw.get("hours"),
        addresses=addresses,
        coordinates=[center],
        geometry=geometry,
        center=center,
        county_codes=raw.get("counties"),
        source_url=ENERGA_PAGE_URL,
        raw=raw,
    )


@parser
def normalize_energa_mass(document):
    payload = document.get("payload") or {}
    if 'active' in payload and type(payload['active']) is not bool:
        raise SourceContractError('ENERGA: active awarii masowej nie jest wartością logiczną')
    if 'active' not in payload:
        warn('energa_mass_active_missing')
    status = "active" if payload.get("active", True) else "cancelled"
    return canonical_event(
        "energa",
        payload.get("guid") or document.get("name") or document.get("id"),
        "mass_outage",
        status=status,
        description=payload.get("message"),
        start_at=document.get("creationDate") or document.get("publishedFrom"),
        stop_at=payload.get("cancelDate") or None,
        updated_at=document.get("lastUpdate") or document.get("lastSync"),
        addresses=[{"text": _clean_text(payload.get("message"))}],
        county_codes=payload.get("counties"),
        affected_customers=payload.get("customer"),
        source_url=ENERGA_PAGE_URL,
        raw=document,
    )


async def fetch_energa(session, _config):
    page = await _request_text(session, "GET", ENERGA_PAGE_URL)
    shutdown_match = re.search(r'data-shutdowns=["\']([^"\']+)', page)
    mass_match = re.search(r'data-mass=["\']([^"\']+)', page)
    if not shutdown_match:
        error = SourceContractError("ENERGA: nie znaleziono dynamicznego źródła wyłączeń", code='incomplete')
        raise RuntimeError(str(error)) from error
    shutdown_url = urljoin(ENERGA_PAGE_URL, html.unescape(shutdown_match.group(1)))
    mass_url = (
        urljoin(ENERGA_PAGE_URL, html.unescape(mass_match.group(1)))
        if mass_match
        else ""
    )
    shutdown_data = await _request_json(session, "GET", shutdown_url)
    shutdown_root = _require_dict(shutdown_data, "ENERGA", "źródło planowanych i awaryjnych wyłączeń")
    document = _require_dict(shutdown_root.get("document"), "ENERGA", "document")
    publication_metadata(document)
    payload = _require_dict(document.get("payload"), "ENERGA", "document.payload")
    shutdowns = _require_list_field(payload.get("shutdowns"), "ENERGA", "document.payload.shutdowns")
    events = []
    for raw in shutdowns:
        if not isinstance(raw, dict):
            raise SourceContractError("ENERGA: rekord wyłączenia nie jest obiektem")
        if not raw.get("guid"):
            raise SourceContractError("ENERGA: rekord wyłączenia bez identyfikatora")
        events.append(normalize_energa(raw))
    if mass_url:
        mass_data = await _request_json(session, "GET", mass_url)
        mass_root = _require_dict(mass_data, "ENERGA", "źródło awarii masowych")
        mass_items = _require_list_field(mass_root.get("data"), "ENERGA", "data")
        for raw in mass_items:
            if not isinstance(raw, dict):
                raise SourceContractError("ENERGA: rekord awarii masowej nie jest obiektem")
            payload = _require_dict(raw.get("payload"), "ENERGA", "payload awarii masowej")
            if not (payload.get("guid") or raw.get("name") or raw.get("id")):
                raise SourceContractError("ENERGA: rekord awarii masowej bez identyfikatora")
            events.append(normalize_energa_mass(raw))
    return events


def _recursive_points(value):
    points = []
    if isinstance(value, dict):
        point = _point(
            value.get("lat", value.get("latitude")),
            value.get("lng", value.get("longitude")),
        )
        if point:
            points.append(point)
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                points.extend(_recursive_points(child))
    elif isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            # Stoen/GeoJSON usually uses [lng, lat].
            point = _point(value[1], value[0])
            if point:
                points.append(point)
        else:
            for child in value:
                points.extend(_recursive_points(child))
    return _dedupe_points(points)


@parser
def normalize_stoen_planned(raw):
    addresses = []
    for address in raw.get("addresses") or []:
        if not isinstance(address, dict):
            continue
        street = _clean_text(address.get("streetName"))
        numbers = _clean_text(address.get("houseNumbers"))
        addresses.append(
            {
                "text": " ".join(value for value in (street, numbers) if value),
                "streetName": street,
                "houseNumbers": numbers,
            }
        )
    return canonical_event(
        "stoen",
        raw.get("id"),
        "planned_outage",
        description=raw.get("comment"),
        start_at=raw.get("outageStart"),
        stop_at=raw.get("outageEnd"),
        addresses=addresses,
        coordinates=_recursive_points(raw.get("points")),
        source_url=f"{STOEN_BASE_URL}/public/planned",
        raw=raw,
    )


@parser
def normalize_stoen_failure(raw, channel):
    extensive = channel == "extensive"
    addresses = []
    for address in raw.get("addressList") or []:
        if isinstance(address, dict):
            text = " ".join(
                _clean_text(address.get(field))
                for field in ("city", "street", "houseNumber", "address")
                if address.get(field)
            )
            addresses.append({"text": text, **deepcopy(address)})
        elif address:
            addresses.append({"text": _clean_text(address)})
    points = _recursive_points(raw.get("area"))
    return canonical_event(
        "stoen",
        raw.get("reportId") or raw.get("id"),
        "mass_outage" if extensive else "outage",
        description=raw.get("description") or raw.get("message") or "Awaria sieci",
        start_at=raw.get("outageStart"),
        stop_at=raw.get("outageEnd") or raw.get("repairTime"),
        addresses=addresses,
        coordinates=points,
        polygons=[points] if len(points) >= 3 else [],
        source_url=f"{STOEN_BASE_URL}/public/failures",
        raw=raw,
    )


def _class_tokens(attrs):
    value = dict(attrs).get("class", "")
    return {item.lower() for item in re.split(r"\s+", value.strip()) if item}


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag.lower() in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)

    @property
    def text(self):
        return _clean_text(" ".join(self.parts))


class _EneaIndexParser(_VisibleTextParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._current_link = None

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if self.hidden_depth:
            return
        if tag.lower() == "a":
            if self._current_link is not None and re.search(r"[?&]rejon=", self._current_link["href"]):
                raise SourceContractError("ENEA: niedomknięty link rejonu", code='incomplete')
            values = dict(attrs)
            self._current_link = {
                "href": values.get("href", ""),
                "title": values.get("title", ""),
                "text": [],
            }

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._current_link is not None:
            self.links.append(self._current_link)
            self._current_link = None
        super().handle_endtag(tag)

    def handle_data(self, data):
        super().handle_data(data)
        if not self.hidden_depth and self._current_link is not None:
            self._current_link["text"].append(data)

    def close(self):
        # HTMLParser traktuje urwany znacznik otwierający jako zwykły tekst.
        if re.search(r"<a\b[^>]*[?&]rejon=[^>]*$", self.rawdata, re.IGNORECASE):
            raise SourceContractError("ENEA: niedomknięty znacznik linku rejonu", code='incomplete')
        super().close()
        if self._current_link is not None and re.search(r"[?&]rejon=", self._current_link["href"]):
            raise SourceContractError("ENEA: niedomknięty link rejonu", code='incomplete')


class _EneaEventParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._block = None
        self._block_depth = 0
        self._capture = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        tokens = _class_tokens(attrs)
        if tag == "div" and {"unpl", "block", "info"}.issubset(tokens):
            if self._block is not None:
                raise SourceContractError("ENEA: nowy blok zdarzenia w niedomkniętym poprzednim bloku", code='incomplete')
            self._block = {"title": [], "description": [], "comments": [], "subtext": []}
            self._block_depth = 1
            return
        if self._block is None:
            return
        if tag == "div":
            self._block_depth += 1
        if tag == "h4" and any(token.startswith("title_") for token in tokens):
            self._capture = "title"
        elif tag == "p" and "description" in tokens:
            self._capture = "description"
        elif tag == "p" and "subtext" in tokens:
            self._capture = "subtext"

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._block is None:
            return
        if (tag == "h4" and self._capture == "title") or (
            tag == "p" and self._capture in ("description", "subtext")
        ):
            self._capture = None
        if tag == "div":
            self._block_depth -= 1
            if self._block_depth <= 0:
                self.blocks.append(self._block)
                self._block = None
                self._capture = None

    def handle_data(self, data):
        if self._block is not None and self._capture:
            self._block[self._capture].append(data)

    def handle_comment(self, data):
        if self._block is not None:
            self._block["comments"].append(data)

    def close(self):
        super().close()
        if self._block is not None:
            raise SourceContractError("ENEA: niedomknięty blok zdarzenia", code='incomplete')


def _visible_text(page):
    parser = _VisibleTextParser()
    parser.feed(page or "")
    return parser.text


def _check_enea_maintenance(page, context):
    """Strona zastępcza nie jest poprawnym pustym indeksem ani odczytem rejonu."""
    index = _EneaIndexParser()
    index.feed(page or "")
    index.close()
    text = index.text.casefold()
    administrative = "prowadzone są prace administracyjne" in text
    service = "prace serwisowe" in text and (
        "strona zablokowana" in text or "strona niebawem będzie dostępna" in text
    )
    if not (administrative or service):
        return
    # Zwykły komunikat o pracach lub stopka nie zastępują poprawnych danych.
    if any(re.search(r"[?&]rejon=\d+", link['href']) for link in index.links):
        return
    events = _EneaEventParser()
    events.feed(page or "")
    events.close()
    if events.blocks:
        return
    raise SourceMaintenanceError(
        f"ENEA: strona {context} jest niedostępna z powodu prac serwisowych. "
        f"Komunikat operatora: {index.text[:600]}"
    )


def _stoen_planned_items(data):
    if isinstance(data, dict):
        if "items" in data:
            return _require_list(data["items"], "Stoen", "items")
        if "content" in data:
            return _require_list(data["content"], "Stoen", "content")
        raise SourceContractError("Stoen: brak listy planowanych wyłączeń", code='incomplete')
    if isinstance(data, list):
        return data
    raise SourceContractError("Stoen: niepoprawny format planowanych wyłączeń")


async def _fetch_stoen_planned(session, url, page_size=1000, max_pages=100):
    by_id = {}
    offset = 0
    for _page_number in range(max_pages):
        payload = {
            "id": None,
            "area": None,
            "outageStart": None,
            "outageEnd": None,
            "page": {
                "limit": page_size,
                "offset": offset,
                "sortField": None,
                "sortOrder": None,
            },
        }
        data = await _request_json(session, "POST", url, json=payload)
        items = _stoen_planned_items(data)
        previous_count = len(by_id)
        for raw in items:
            if not isinstance(raw, dict):
                raise SourceContractError("Stoen: rekord planowany nie jest obiektem")
            event_id = raw.get("id")
            if not event_id:
                raise SourceContractError("Stoen: rekord planowany bez identyfikatora")
            key = str(event_id)
            if key in by_id and by_id[key] != raw:
                raise SourceContractError(f"Stoen: sprzeczne rekordy tego samego ID na stronie {_page_number + 1}", code='conflicting_id')
            by_id[key] = raw
        completed('stoen_planned_pages')
        if len(items) < page_size:
            return list(by_id.values())
        if len(by_id) == previous_count:
            raise SourceContractError("Stoen: paginacja nie zwraca nowych rekordów", code='incomplete')
        offset += len(items)
    raise SourceContractError("Stoen: przekroczono limit stron planowanych wyłączeń", code='incomplete')


async def fetch_stoen(session, _config):
    planned_url = f"{STOEN_BASE_URL}/public/api/planned-outage/search/compressed-report"
    planned_task = _fetch_stoen_planned(session, planned_url)
    channels = ("lv", "hv", "extensive")
    failure_tasks = [
        _request_json(
            session,
            "GET",
            f"{STOEN_BASE_URL}/public/api/call-net/failures-{channel}",
        )
        for channel in channels
    ]
    planned_items, *failures = await asyncio.gather(planned_task, *failure_tasks)
    events = []
    for raw in planned_items:
        events.append(normalize_stoen_planned(raw))
    for channel, data in zip(channels, failures):
        root = _require_dict(data, "Stoen", f"awarie {channel}")
        for raw in _require_list_field(root.get("items"), "Stoen", f"awarie {channel}.items"):
            if not isinstance(raw, dict):
                raise SourceContractError(f"Stoen: rekord awarii {channel} nie jest obiektem")
            if not (raw.get("reportId") or raw.get("id")):
                raise SourceContractError(f"Stoen: rekord awarii {channel} bez identyfikatora")
            events.append(normalize_stoen_failure(raw, channel))
        completed('stoen_failure_channels')
    return events


def _enea_end_candidates(subtext, comments):
    months = ('stycznia', 'lutego', 'marca', 'kwietnia', 'maja', 'czerwca',
              'lipca', 'sierpnia', 'września', 'października', 'listopada', 'grudnia')
    values = []
    for day, month, year, clock in re.findall(
        r'(\d{1,2})\s+(' + '|'.join(months) + r')\s+(\d{4})\s+r\.?\s+do godziny\s+(\d{1,2}:\d{2})', subtext, re.I
    ):
        values.append(f'{year}-{months.index(month.lower()) + 1:02d}-{int(day):02d}T{clock.zfill(5)}')
    for comment in comments:
        values.extend(f'{date}T{clock.zfill(5)}' for date, clock in re.findall(
            r'(\d{4}-\d{2}-\d{2})\s+do godziny\s+(\d{1,2}:\d{2})', comment))
    return values


@parser
def parse_enea_events(page, kind, source_url):
    events = []
    parser = _EneaEventParser()
    parser.feed(page or "")
    parser.close()
    for block in parser.blocks:
        title = _clean_text(" ".join(block["title"]))
        description = _clean_text(" ".join(block["description"]))
        comments = block["comments"]
        if not title or not description:
            raise SourceContractError(f"ENEA: niekompletny blok zdarzenia {source_url}", code='incomplete')
        uuid = ""
        start_at = None
        stop_at = None
        for comment in comments:
            uuid_match = re.search(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                comment,
                re.IGNORECASE,
            )
            if uuid_match:
                uuid = uuid_match.group(0)
            dates = re.findall(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", comment)
            if dates:
                start_at = dates[0].replace(" ", "T")
                stop_at = dates[-1].replace(" ", "T")
        raw = {
            "title": title,
            "description": description,
            "comments": comments,
            "source_url": source_url,
        }
        if not uuid:
            uuid = hashlib.sha256(
                json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:24]
        # Hash zastępczy pozostaje oparty na starym raw, przed dodaniem subtext.
        subtext = _clean_text(' '.join(block['subtext']))
        ends = _enea_end_candidates(subtext, comments)
        warnings = []
        if ends:
            known = [*ends, *([stop_at] if stop_at else [])]
            parsed = [parse_event_datetime(value) for value in known]
            if any(value is None for value in parsed) or len(set(parsed)) != 1:
                # Zachowujemy niepoprawny czas DST do wspólnej walidacji, lecz
                # rozbieżne terminy pozostawiamy nieznane.
                if len(set(known)) == 1:
                    stop_at = known[0]
                else:
                    stop_at = None
                    warnings.append('enea_time_conflict')
            else:
                stop_at = ends[0] if event_time_warning(ends[0]) else parsed[0].isoformat(timespec='seconds')
        raw['subtext'] = subtext
        events.append(
            canonical_event(
                "enea",
                uuid,
                kind,
                description=" — ".join(value for value in (title, description) if value),
                start_at=start_at,
                stop_at=stop_at,
                addresses=[{"text": description or title}],
                source_url=source_url,
                raw=raw,
            )
        )
        if warnings:
            events[-1]['parser_warnings'] = warnings
            for code in warnings:
                warn(code)
    return events


@parser
def enea_active_regions(index_page, allowed_regions=None):
    allowed = {int(value) for value in allowed_regions or []}
    active = set()
    states = {}
    parser = _EneaIndexParser()
    parser.feed(index_page or "")
    parser.close()
    for link in parser.links:
        match = re.search(r"[?&]rejon=(\d+)", link["href"])
        if not match:
            if re.search(r"[?&]rejon=", link["href"]):
                raise SourceContractError("ENEA: nierozpoznany identyfikator rejonu")
            continue
        region = int(match.group(1))
        title = _clean_text(" ".join([link["title"], *link["text"]])).lower()
        if allowed and region not in allowed:
            continue
        counts = re.findall(r"\b([0-9]+)\s+(?:planowan\w+\s+)?(?:wyłącze\w*|wylacze\w*|awari\w*)\b", title)
        empty = _enea_explicit_empty(title)
        if len(set(counts)) > 1 or (empty and any(int(count) for count in counts)):
            raise SourceContractError(f"ENEA: sprzeczny stan rejonu {region}")
        if not counts and not empty:
            raise SourceContractError(f"ENEA: nierozpoznany stan rejonu {region}")
        is_active = bool(counts and int(counts[0]) > 0)
        if region in states and states[region] != is_active:
            raise SourceContractError(f"ENEA: sprzeczny stan rejonu {region}")
        states[region] = is_active
        if is_active:
            active.add(region)
    if active and _enea_explicit_empty(parser.text):
        raise SourceContractError("ENEA: sprzeczny komunikat pustego indeksu")
    return sorted(active)


def _enea_explicit_empty(text):
    return bool(re.search(
        r"\b(?:brak\s+(?:planowan\w+\s+)?(?:wyłącze\w*|wylacze\w*|awarii)"
        r"|nie ma planowanych\s+(?:wyłącze\w*|wylacze\w*)"
        r"|nie odnotowano\s+(?:żadnych\s+)?(?:awari\w*|wyłącze\w*|wylacze\w*))\b",
        text.lower(),
    ))


@parser
def enea_index_is_recognized(page):
    parser = _EneaIndexParser()
    parser.feed(page or "")
    parser.close()
    text = parser.text.lower()
    return bool(
        any(re.search(r"[?&]rejon=\d+", link["href"]) for link in parser.links)
        or _enea_explicit_empty(text)
    )


@parser
def enea_region_page_is_recognized(page):
    parser = _EneaEventParser()
    parser.feed(page or "")
    parser.close()
    text = _visible_text(page).lower()
    return bool(
        parser.blocks
        or _enea_explicit_empty(text)
    )


async def _fetch_enea_mode(session, index_url, kind, allowed_regions):
    index_page = await _request_text(session, "GET", index_url)
    mode = "planowanych wyłączeń" if kind == "planned_outage" else "awarii"
    _check_enea_maintenance(index_page, mode)
    if not enea_index_is_recognized(index_page):
        raise SourceContractError(f"ENEA: nierozpoznana strona indeksu {mode}")
    targets = []
    for region in enea_active_regions(index_page, allowed_regions):
        if kind == "planned_outage":
            url = urljoin(ENEA_BASE_URL, f"index.php?rejon={region}")
        else:
            url = urljoin(ENEA_BASE_URL, f"index.php?page=awarie&rejon={region}")
        targets.append(url)
    semaphore = asyncio.Semaphore(ENEA_MAX_CONCURRENT_REQUESTS)

    async def fetch_page(url):
        async with semaphore:
            return await _request_text(session, "GET", url)

    tasks = [asyncio.create_task(fetch_page(url)) for url in targets]
    try:
        pages = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    events = []
    for url, page in zip(targets, pages):
        _check_enea_maintenance(page, mode)
        parsed = parse_enea_events(page, kind, url)
        if not parsed and not enea_region_page_is_recognized(page):
            raise SourceContractError(f"ENEA: nierozpoznana strona rejonu {url}")
        events.extend(parsed)
        completed('enea_region_pages')
    completed('enea_modes')
    return events


async def _fetch_enea_with_session(session, config):
    allowed_regions = config.get("enea_regions") or []
    planned_events = await _fetch_enea_mode(
        session, ENEA_BASE_URL, "planned_outage", allowed_regions
    )
    outage_events = await _fetch_enea_mode(
        session,
        urljoin(ENEA_BASE_URL, "awarie/"),
        "outage",
        allowed_regions,
    )
    events = planned_events + outage_events
    by_id = {f"{event['type']}:{event['source_event_id']}": event for event in events}
    return list(by_id.values())


async def fetch_enea(session, config):
    timeout = getattr(session, "timeout", None)
    if not isinstance(timeout, aiohttp.ClientTimeout):
        timeout = aiohttp.ClientTimeout(total=30)
    headers = dict(getattr(session, "headers", {}) or {})
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        cookie_jar=aiohttp.DummyCookieJar(),
        trace_configs=[source_trace()],
    ) as enea_session:
        return await _fetch_enea_with_session(enea_session, config)


FETCHERS = {
    "pge": fetch_pge,
    "tauron": fetch_tauron,
    "energa": fetch_energa,
    "enea": fetch_enea,
    "stoen": fetch_stoen,
}


async def fetch_source(session, source, config):
    try:
        fetcher = FETCHERS[source]
    except KeyError as error:
        raise SourceContractError(f"Nieznane źródło OSD: {source}") from error
    return await fetcher(session, config)
