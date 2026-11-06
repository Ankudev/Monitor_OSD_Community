"""Publiczne, tylko do odczytu API monitora OSD."""

from __future__ import annotations

from event_history import project_event
from public_projection import public_data, mark_public_response
import publication_policy as publication
import community
from address_matching import public_address_audit
from power_supply import normalize_power_supply
from engine_store import EngineStoreError, history_time

import csv
import asyncio
import hashlib
import io
import sqlite3
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from aiohttp import web
from matching_evidence import point_confirmed
from scan_diagnostics import PUBLICATION_FIELDS
from polish_text import count as polish_count


API_VERSION = "1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DISPLAY_MAX_BYTES = 2048
API_CONTEXT_KEY = web.AppKey("api_v1_context", dict)

COMMON_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "If-None-Match",
    "Access-Control-Expose-Headers": "ETag, Date, Retry-After, X-History-Total, X-History-Truncated, X-Publication-Revision, Link",
    "Cache-Control": "public, no-cache",
}


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _etag(body):
    return f'W/"{hashlib.sha256(body).hexdigest()}"'


def _etag_matches(request, etag):
    expected = etag.removeprefix("W/")
    for value in request.headers.get("If-None-Match", "").split(","):
        candidate = value.strip()
        if candidate == "*" or candidate.removeprefix("W/") == expected:
            return True
    return False


def _response(request, payload, *, status=200, document=False):
    policy = publication.current()
    if not document and policy is not None and policy.enforced and isinstance(payload,dict):
        metadata = publication.public_metadata(policy) if request.path.endswith(('/map','/export.json')) else {'revision':policy.token}
        payload = dict(payload, publication=metadata)
    if not document and request.query.get('include_raw') != '1':
        payload = public_data(payload)
    body = _json_bytes(payload)
    etag = _etag(body)
    headers = dict(COMMON_HEADERS, ETag=etag)
    if status == 200 and _etag_matches(request, etag):
        return web.Response(status=304, headers=headers)
    response = web.Response(
        body=body,
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers=headers,
    )
    return mark_public_response(response) if request.query.get('include_raw') != '1' else response


def _error(request, status, code, message, **details):
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return _response(request, {"api_version": API_VERSION, "error": error}, status=status)


def _utc(value):
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_filter_time(value, field):
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"Parametr {field} nie jest datą RFC 3339") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Parametr {field} musi zawierać strefę czasową")
    return parsed.astimezone(timezone.utc)


def _params(request, allowed):
    unknown = sorted(set(request.query) - set(allowed))
    duplicates = sorted(key for key in request.query if len(request.query.getall(key)) > 1)
    if unknown:
        raise ValueError("Nieznane parametry: " + ", ".join(unknown))
    if duplicates:
        raise ValueError("Powtórzone parametry: " + ", ".join(duplicates))
    return request.query


def _limit_offset(query):
    try:
        limit = int(query.get("limit", DEFAULT_LIMIT))
        offset = int(query.get("offset", 0))
    except ValueError as error:
        raise ValueError("limit i offset muszą być liczbami całkowitymi") from error
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit musi mieścić się w zakresie 1-{MAX_LIMIT}")
    if offset < 0:
        raise ValueError("offset nie może być ujemny")
    return limit, offset


def _view(query):
    value = query.get("view", "compact")
    if value not in {"compact", "full"}:
        raise ValueError("view musi mieć wartość compact albo full")
    return value


def _fields(query):
    raw = query.get("fields")
    if not raw:
        return None
    result = [item.strip() for item in raw.split(",") if item.strip()]
    if not result:
        raise ValueError("fields nie może być puste")
    return result


def _project(item, fields, required):
    if fields is None:
        return item
    unknown = sorted(set(fields) - set(item))
    if unknown:
        raise ValueError("Nieznane pola: " + ", ".join(unknown))
    selected = set(fields) | set(required)
    return {key: value for key, value in item.items() if key in selected}


def _snapshot(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()[:24]


def _freshness(context, *, visible=None, pending=None):
    config = context["config"]()
    runtime = context["runtime"]()
    visible = _events(context) if visible is None else visible
    pending = context["matching_pending"]() if pending is None else pending
    now = datetime.now(timezone.utc)
    sources = []
    for source in config.get("enabled_sources", []):
        if publication.current() and not publication.current().allows(source):
            continue
        health = deepcopy(runtime.get("sources", {}).get(source, {}))
        interval = int(health.get("interval_seconds") or config["scan_interval_seconds"])
        last_success = _utc(health.get("last_success_at"))
        parsed = _parse_filter_time(last_success, "last_success_at") if last_success else None
        missing = parsed is None
        stale = bool(health.get("error")) or missing or (now - parsed).total_seconds() > interval * 2
        next_scan_at = None
        if health.get("last_attempt_at"):
            wait = context["source_wait_seconds"](source, health, interval, now.astimezone())
            next_scan_at = datetime.fromtimestamp(
                now.timestamp() + max(0, wait), timezone.utc
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        sources.append(
            {
                "source": source,
                "label": health.get("label") or source.upper(),
                "status": "missing" if missing else ("stale" if stale else "fresh"),
                "last_success_at": last_success,
                **{key: health.get(key) for key in PUBLICATION_FIELDS},
                "last_attempt_at": _utc(health.get("last_attempt_at")),
                "next_scan_at": next_scan_at,
                "blocked_until": _utc(health.get("blocked_until")),
                "interval_seconds": interval,
                "running": bool(health.get("running")),
                "error": health.get("error"),
                "error_category": health.get("error_category") if health.get("error") else None,
                "last_error": health.get("last_error"),
                "last_error_at": _utc(health.get("last_error_at")),
                "recovered_at": _utc(health.get("recovered_at")),
                "consecutive_errors": int(health.get("consecutive_errors") or 0),
                "source_event_count": int(health.get("source_event_count") or 0),
                "matched_event_count": sum(event.get("source", "pge") == source for event in visible),
                "matching_pending_count": sum(source in values for values in pending.values()),
            }
        )
    missing_sources = [item["source"] for item in sources if item["status"] == "missing"]
    stale_sources = [item["source"] for item in sources if item["status"] == "stale"]
    if not sources:
        status = "unavailable"
    elif len(missing_sources) == len(sources):
        status = "unavailable"
    elif missing_sources or pending:
        status = "partial"
    elif stale_sources:
        status = "stale"
    else:
        status = "fresh"
    successful = [item["last_success_at"] for item in sources if item["last_success_at"]]
    return {
        "data_status": status,
        "data_updated_at": max(successful) if successful else None,
        "stale_sources": stale_sources,
        "missing_sources": missing_sources,
        "sources": sources,
    }


def _event(context, event, view="compact", include_raw=False):
    return project_event(context, event, view, include_raw)


def _object(item, view="compact"):
    power = normalize_power_supply(item.get("power_supply", {}))
    if view != "full":
        power.pop("notes")
    result = {
        "object_id": str(item.get("id") or ""),
        "name": str(item.get("name") or item.get("city") or ""),
        "city": str(item.get("city") or ""),
        "street": str(item.get("street") or ""),
        "city_sym": str(item.get("city_sym") or ""),
        "street_sym": str(item.get("street_sym") or ""),
        "house_number": item.get("house_number"),
        "power_supply": power,
        "address_audit": public_address_audit(item),
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
    }
    if item.get("provenance"):
        result["provenance"] = deepcopy(item["provenance"])
    if view == "full":
        result.update(
            keyword=str(item.get("keyword") or ""),
            location_audit=deepcopy(item.get("location_audit") or {}),
        )
    return result


def _location(context, item, related, pending):
    result = _object(item, "full")
    object_id = result["object_id"]
    phases = [(event, context["event_time_phase"](event)) for event in related]
    latitude = result.get("latitude")
    longitude = result.get("longitude")
    has_coordinates = latitude not in (None, "") and longitude not in (None, "")
    result.update(
        matching_pending_sources=pending.get(object_id, []),
        monitoring_scope="point_or_address",
        confirmed_event_count=sum(point_confirmed(event) for event in related),
        candidate_event_count=sum(not point_confirmed(event) for event in related),
        has_coordinates=has_coordinates,
        map_url=(
            f"https://www.google.com/maps/dir/?api=1&destination={latitude},{longitude}"
            if has_coordinates
            else None
        ),
        active_event_count=sum(1 for _event_value, phase in phases if phase == "ongoing"),
        upcoming_event_count=sum(1 for _event_value, phase in phases if phase == "upcoming"),
        attention_event_count=sum(
            1
            for event, phase in phases
            if event.get("verification_pending") or phase in {"overdue", "unknown"}
        ),
        events_url=f"/api/v1/events?object_id={quote(object_id, safe='')}",
    )
    return result


def _locations(context, *, events=None, pending=None):
    events = _events(context) if events is None else events
    pending = context["matching_pending"]() if pending is None else pending
    by_object = {}
    for event in events:
        by_object.setdefault(str(event.get("object_id") or ""), []).append(event)
    items = [_location(context, item, by_object.get(str(item.get("id") or ""), []), pending)
             for item in context["objects"]()]
    items.sort(key=lambda item: (item["name"].casefold(), item["object_id"]))
    return items


def _localities(context, locations=None):
    locations = locations if locations is not None else _locations(context)
    city_syms = sorted({item["city_sym"] for item in locations if item["city_sym"]})
    details = context.get("locality_details", lambda _values: {})(city_syms)
    grouped = {}
    for item in locations:
        city_sym = item["city_sym"]
        if not city_sym:
            continue
        locality = grouped.setdefault(
            city_sym,
            {
                "city_sym": city_sym,
                "city": item["city"],
                "voivodeship_code": str(details.get(city_sym, {}).get("voivodeship_code") or ""),
                "county_code": str(details.get(city_sym, {}).get("county_code") or ""),
                "commune_code": str(details.get(city_sym, {}).get("commune_code") or ""),
                "object_ids": [],
                "object_count": 0,
                "active_event_count": 0,
                "upcoming_event_count": 0,
                "attention_event_count": 0,
            },
        )
        locality["object_ids"].append(item["object_id"])
        locality["active_event_count"] += item["active_event_count"]
        locality["upcoming_event_count"] += item["upcoming_event_count"]
        locality["attention_event_count"] += item["attention_event_count"]
    for locality in grouped.values():
        locality["object_ids"].sort()
        locality["object_count"] = len(locality["object_ids"])
    return sorted(grouped.values(), key=lambda item: (item["city"].casefold(), item["city_sym"]))


def _envelope(context, items, request, limit, offset, *, snapshot_seed=None):
    freshness = _freshness(context)
    seed = snapshot_seed if snapshot_seed is not None else items
    snapshot_id = _snapshot({"items": seed, "freshness": freshness, "policy": publication.current().token if publication.current() else None})
    requested = request.query.get("snapshot_id")
    if requested and requested != snapshot_id:
        raise RuntimeError(snapshot_id)
    page = items[offset : offset + limit]
    next_url = None
    if offset + limit < len(items):
        query = [(key, value) for key, value in request.query.items() if key not in {"offset", "snapshot_id"}]
        query.extend((("offset", str(offset + limit)), ("snapshot_id", snapshot_id)))
        next_url = f"{request.path}?{urlencode(query)}"
    return {
        "api_version": API_VERSION,
        "snapshot_id": snapshot_id,
        "data_updated_at": freshness["data_updated_at"],
        "data_status": freshness["data_status"],
        "stale_sources": freshness["stale_sources"],
        "missing_sources": freshness["missing_sources"],
        "count": len(page),
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "next": next_url,
        "items": page,
    }


def _events(context):
    # Projekcje tylko czytają rekordy i kończą się przed kolejnym await.
    # Kopiowanie surowych danych operatora jest zbędne; wynik buduje _event.
    return list(context["visible_events"]().values())


async def root_handler(request):
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "name": "Monitor OSD Polska API",
            "read_only": True,
            "documentation": "/api-docs/",
            "openapi": "/api/v1/openapi.json",
            "endpoints": [
                "summary",
                "sources",
                "locations",
                "localities",
                "map",
                "objects",
                "objects/{object_id}/location-review",
                "events",
                "history",
                "export.json",
                "export.csv",
            ],
        },
    )










async def summary_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    freshness = _freshness(context)
    events = _events(context)
    objects = context["objects"]()
    phases = {}
    types = {}
    for event in events:
        phase = context["event_time_phase"](event)
        phases[phase] = phases.get(phase, 0) + 1
        kind = str(event.get("type") or "unknown")
        types[kind] = types.get(kind, 0) + 1
    metrics = {
        "object_count": len(objects),
        "event_count": len(events),
        "confirmed_event_count": sum(point_confirmed(event) for event in events),
        "candidate_event_count": sum(not point_confirmed(event) for event in events),
        "events_by_type": dict(sorted(types.items())),
        "events_by_time_phase": dict(sorted(phases.items())),
    }
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "snapshot_id": _snapshot({"metrics": metrics, "freshness": freshness}),
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "stale_sources": freshness["stale_sources"],
            "missing_sources": freshness["missing_sources"],
            **metrics,
        },
    )


async def sources_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    freshness = _freshness(context)
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "stale_sources": freshness["stale_sources"],
            "missing_sources": freshness["missing_sources"],
            "count": len(freshness["sources"]),
            "items": freshness["sources"],
        },
    )


async def objects_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"q", "view", "fields", "limit", "offset", "snapshot_id"})
        limit, offset = _limit_offset(query)
        view = _view(query)
        fields = _fields(query)
        items = [_object(item, view) for item in context["objects"]()]
        needle = query.get("q", "").strip().casefold()
        if needle:
            items = [item for item in items if needle in " ".join(str(value) for value in item.values()).casefold()]
        items.sort(key=lambda item: (item["name"].casefold(), item["object_id"]))
        items = [_project(item, fields, {"object_id"}) for item in items]
        return _response(request, _envelope(context, items, request, limit, offset))
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    except RuntimeError as error:
        return _error(request, 409, "snapshot_changed", "Dane zmieniły się między stronami", current_snapshot_id=str(error))


async def object_detail_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"view", "fields"})
        view = _view(query)
        fields = _fields(query)
        object_id = request.match_info["object_id"]
        for value in context["objects"]():
            if str(value.get("id")) == object_id:
                item = _project(_object(value, view), fields, {"object_id"})
                freshness = _freshness(context)
                return _response(request, {"api_version": API_VERSION, "data_status": freshness["data_status"], "item": item})
        return _error(request, 404, "not_found", "Nie znaleziono obiektu")
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))


async def location_review_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
        obj = next((row for row in context.get("private_objects", context["objects"])() if str(row.get("id")) == request.match_info["object_id"]), None)
        if obj is None:
            return _error(request, 404, "not_found", "Nie znaleziono obiektu")
        review = await asyncio.to_thread(context["location_review"], obj)
        return _response(request, {"api_version": API_VERSION, "item": review})
    except json.JSONDecodeError:
        return _error(request, 503, "review_unavailable", "Nie udało się odczytać zapisanego porównania")
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    except (OSError, sqlite3.Error, TypeError, KeyError):
        return _error(request, 503, "review_unavailable", "Nie udało się odczytać zapisanego porównania")


async def locations_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"q"})
        items = _locations(context)
        needle = query.get("q", "").strip().casefold()
        if needle:
            items = [
                item
                for item in items
                if needle
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("name", "keyword", "city", "city_sym", "street", "street_sym")
                ).casefold()
            ]
        freshness = _freshness(context)
        return _response(
            request,
            {
                "api_version": API_VERSION,
                "snapshot_id": _snapshot({"locations": items, "freshness": freshness}),
                "data_updated_at": freshness["data_updated_at"],
                "data_status": freshness["data_status"],
                "stale_sources": freshness["stale_sources"],
                "missing_sources": freshness["missing_sources"],
                "count": len(items),
                "items": items,
            },
        )
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))


async def location_detail_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    object_id = request.match_info["object_id"]
    item = next((item for item in _locations(context) if item["object_id"] == object_id), None)
    if item is None:
        return _error(request, 404, "not_found", "Nie znaleziono lokalizacji")
    freshness = _freshness(context)
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "item": item,
        },
    )


async def localities_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    items = _localities(context)
    freshness = _freshness(context)
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "snapshot_id": _snapshot({"localities": items, "freshness": freshness}),
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "count": len(items),
            "items": items,
        },
    )


async def locality_detail_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    city_sym = request.match_info["city_sym"]
    locations = _locations(context)
    item = next((item for item in _localities(context, locations) if item["city_sym"] == city_sym), None)
    if item is None:
        return _error(request, 404, "not_found", "Nie znaleziono miejscowości")
    item = dict(item, locations=[value for value in locations if value["city_sym"] == city_sym])
    freshness = _freshness(context)
    return _response(
        request,
        {
            "api_version": API_VERSION,
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "item": item,
        },
    )


async def map_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    # Do zbudowania odpowiedzi nie ma await ani modyfikacji rekordów silnika.
    # Projekcja tworzy własny wynik bez kopiowania nieużywanych danych raw.
    visible, pending = context["visibility"]()
    raw_events = list(visible.values())
    locations = _locations(context, events=raw_events, pending=pending)
    policy = publication.current()
    if policy is not None and policy.enforced:
        # Nazwa wymaga dopuszczonego źródła katalogowego albo jawnej decyzji właściciela.
        # Każde pole nadal przechodzi politykę publikacji i kontrolę skrótu wartości.
        locations = [item for item in locations
                     if item.get("provenance", {}).get("name", {}).get("source_id") in
                     {"demo_locations", "project", "owner_locations"}]
        object_ids = {item["object_id"] for item in locations}
        raw_events = [event for event in raw_events if str(event.get("object_id")) in object_ids]
        pending = {key: value for key, value in pending.items() if str(key) in object_ids}
    freshness = _freshness(context, visible=raw_events, pending=pending)
    events = [_event(context, event, "full") for event in raw_events]
    events.sort(key=lambda item: (item.get("start_at") or "", item["source"], item["event_id"]))
    payload = {
        "api_version": API_VERSION,
        "data_updated_at": freshness["data_updated_at"],
        "data_status": freshness["data_status"],
        "stale_sources": freshness["stale_sources"],
        "missing_sources": freshness["missing_sources"],
        "locations": locations,
        "events": events,
        "geometries": _map_geometries(context, events),
        "sources": freshness["sources"],
        "counts": {
            "locations": len(locations),
            "events": len(events),
            "confirmed_points": sum(point_confirmed(event) for event in events),
            "candidates": sum(not point_confirmed(event) for event in events),
            "requires_review": sum(
                1 for item in locations if item.get("location_audit", {}).get("requires_review")
            ),
        },
    }
    payload["snapshot_id"] = _snapshot(payload)
    return _response(request, payload)


def _map_geometries(context, events):
    references = {event.get("geometry_ref") for event in events}
    stored = context["state"]().get("source_geometries", {})
    return {key: deepcopy(value) for key, value in stored.items() if key in references}


def _event_intersects(context, event, start, stop):
    for window in context["event_time_windows"](event):
        if not window.get("valid"):
            continue
        window_start = _parse_filter_time(_utc(window.get("start_at")), "start_at") if window.get("start_at") else None
        window_stop = _parse_filter_time(_utc(window.get("stop_at")), "stop_at") if window.get("stop_at") else None
        if window_start is None and window_stop is None:
            continue
        if (window_stop is None or window_stop >= start) and (window_start is None or window_start < stop):
            return True
    return False


async def events_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"object_id", "source", "type", "time_phase", "match_scope", "from", "to", "view", "fields", "limit", "offset", "snapshot_id"})
        limit, offset = _limit_offset(query)
        view = _view(query)
        fields = _fields(query)
        start = _parse_filter_time(query.get("from"), "from")
        stop = _parse_filter_time(query.get("to"), "to")
        if (start is None) != (stop is None):
            raise ValueError("Parametry from i to muszą wystąpić razem")
        if start and stop and start >= stop:
            raise ValueError("Parametr from musi określać czas wcześniejszy niż parametr to")
        events = _events(context)
        scope = query.get("match_scope")
        if scope:
            if scope not in {"confirmed", "candidate"}:
                raise ValueError("match_scope musi mieć wartość confirmed albo candidate")
            events = [event for event in events if point_confirmed(event) == (scope == "confirmed")]
        for key in ("object_id", "source", "type"):
            value = query.get(key)
            if value:
                source_key = "object_id" if key == "object_id" else key
                events = [event for event in events if str(event.get(source_key) or "") == value]
        phase = query.get("time_phase")
        if phase:
            if phase not in {"ongoing", "upcoming", "overdue", "closed", "unknown"}:
                raise ValueError("Nieznana wartość time_phase")
            events = [event for event in events if context["event_time_phase"](event) == phase]
        if start:
            events = [event for event in events if _event_intersects(context, event, start, stop)]
        items = [_event(context, event, view) for event in events]
        items.sort(key=lambda item: (item.get("start_at") or "", item["source"], item["event_id"]))
        items = [_project(item, fields, {"event_id", "object_id", "source"}) for item in items]
        return _response(request, _envelope(context, items, request, limit, offset))
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    except RuntimeError as error:
        return _error(request, 409, "snapshot_changed", "Dane zmieniły się między stronami", current_snapshot_id=str(error))


async def event_detail_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"include_raw"})
        include_raw = query.get("include_raw", "0")
        if include_raw not in {"0", "1"}:
            raise ValueError("include_raw musi mieć wartość 0 albo 1")
        event_id = request.match_info["event_id"]
        for event in _events(context):
            if str(event.get("event_key")) == event_id:
                return _response(request, {"api_version": API_VERSION, "item": _event(context, event, "full", include_raw == "1")})
        return _error(request, 404, "not_found", "Nie znaleziono zdarzenia")
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))


async def display_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        _params(request, set())
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    object_id = request.match_info["object_id"]
    obj = next((item for item in context["objects"]() if str(item.get("id")) == object_id), None)
    if obj is None:
        return _error(request, 404, "not_found", "Nie znaleziono obiektu")
    freshness = _freshness(context)
    events = [event for event in _events(context) if str(event.get("object_id")) == object_id]
    candidates = [event for event in events if not point_confirmed(event)]
    events = [event for event in events if point_confirmed(event)]
    pending = context["matching_pending"]().get(object_id, [])
    if freshness["data_status"] == "unavailable" or pending:
        counts = {"outage_count": None, "planned_now_count": None, "planned_future_count": None, "unverified_count": None}
        message = "Oczekiwanie na przeliczenie dopasowań po zmianie obiektu" if pending else "Brak aktualnych danych ze źródeł"
    else:
        phases = [(event, context["event_time_phase"](event)) for event in events]
        counts = {
            "outage_count": sum(
                1
                for event, phase in phases
                if event.get("type") in {"outage", "mass_outage"} and phase == "ongoing"
            ),
            "planned_now_count": sum(
                1
                for event, phase in phases
                if event.get("type") == "planned_outage" and phase == "ongoing"
            ),
            "planned_future_count": sum(
                1
                for event, phase in phases
                if event.get("type") == "planned_outage" and phase == "upcoming"
            ),
            "unverified_count": sum(
                1
                for event, phase in phases
                if event.get("verification_pending") or phase in {"overdue", "unknown"}
            ) + len(candidates),
        }
        message = "Brak komunikatów potwierdzających objęcie punktu" if not events else f"Potwierdzone objęcie punktu: {polish_count(len(events), ('komunikat', 'komunikaty', 'komunikatów'))}"
        if candidates:
            message += f"; do sprawdzenia: {len(candidates)}. To nie jest pomiar napięcia."
    future = sorted(
        (_event(context, event) for event in events if context["event_time_phase"](event) == "upcoming"),
        key=lambda item: item.get("start_at") or "",
    )
    successes = [item["last_success_at"] for item in freshness["sources"] if item["last_success_at"]]
    intervals = [item["interval_seconds"] for item in freshness["sources"]]
    payload = {
        "api_version": API_VERSION,
        "object_id": object_id,
        "name": str(obj.get("name") or obj.get("city") or ""),
        "matching_pending_sources": pending,
        "candidate_count": len(candidates),
        "monitoring_scope": "point_or_address",
        "data_status": freshness["data_status"],
        "last_success_at": min(successes) if successes else None,
        "fresh_until": None,
        **counts,
        "next_event_id": future[0]["event_id"] if future else None,
        "next_start_at": future[0].get("start_at") if future else None,
        "next_stop_at": future[0].get("stop_at") if future else None,
        "message": message,
        "poll_after_seconds": max(intervals) if intervals else 300,
    }
    if payload["last_success_at"] and intervals:
        parsed = _parse_filter_time(payload["last_success_at"], "last_success_at")
        payload["fresh_until"] = datetime.fromtimestamp(parsed.timestamp() + max(intervals) * 2, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if len(_json_bytes(payload)) > DISPLAY_MAX_BYTES:
        return _error(request, 500, "display_too_large", "Odpowiedź wyświetlacza przekroczyła limit")
    return _response(request, payload)


def _history_filter(context):
    view = publication.current()
    if view is None or not view.enforced:
        return None
    objects = context.get("private_objects", context["objects"])()
    return lambda source,object_id,before,after,legacy: view.history_allowed(source,object_id,before,after,legacy,objects)


def _history_snapshot(result, filters=None):
    meta = result['meta']
    return _snapshot({key: meta[key] for key in ('archive_instance_id', 'archive_revision', 'archive_last_change_id')}
                     | {'filters': filters or {}, 'policy':publication.current().token if publication.current() else None})


def _history_metadata(result):
    return {'history_scope': 'stored_event_changes', 'complete': False, 'includes_inactive_assignments': True,
            'archive_started_at': result['meta']['archive_started_at'], 'available_from': result['available_from'],
            'retention_days': None, 'retention_limited': False,
            'legacy_import_count': 0 if publication.current() and publication.current().enforced else int(result['meta']['legacy_import_count']),
            'unknown_time_count': result['unknown_time_count']}


async def history_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"object_id", "source", "from", "to", "limit", "offset", "snapshot_id", "latest_per_event"})
        if query.get('latest_per_event', '0') not in {'0', '1'}:
            raise ValueError('Parametr latest_per_event musi mieć wartość 0 albo 1')
        limit, offset = _limit_offset(query)
        start = _parse_filter_time(query.get("from"), "from")
        stop = _parse_filter_time(query.get("to"), "to")
        if (start is None) != (stop is None):
            raise ValueError("Parametry from i to muszą wystąpić razem")
        if start is not None and start >= stop:
            raise ValueError('Parametr from musi określać czas wcześniejszy niż parametr to')
        filters = {'source': query.get('source') or None, 'object_id': query.get('object_id') or None,
                   'start': history_time(start), 'stop': history_time(stop),
                   'latest_per_event': query.get('latest_per_event', '0') == '1'}
        result = await asyncio.to_thread(context['engine_store'].read_history, limit=limit, offset=offset, publication_filter=_history_filter(context), **filters)
        snapshot = _history_snapshot(result, filters)
        if query.get('snapshot_id') and query['snapshot_id'] != snapshot:
            raise RuntimeError(snapshot)
        next_url = None
        if offset + limit < result['total']:
            params = {key: value for key,value in query.items() if key not in {'offset','snapshot_id'}}
            params.update(offset=str(offset+limit), snapshot_id=snapshot)
            next_url = request.path + '?' + urlencode(params)
        freshness = _freshness(context)
        return _response(request, {'api_version': API_VERSION, 'snapshot_id': snapshot,
             **{key: freshness[key] for key in ('data_status','data_updated_at','stale_sources','missing_sources')},
             'count': len(result['items']), 'total': result['total'], 'limit': limit, 'offset': offset,
             'next': next_url, 'items': result['items'], 'latest_per_event': filters['latest_per_event'], **_history_metadata(result)})
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    except (EngineStoreError, sqlite3.Error):
        return _error(request, 503, 'storage_unavailable', 'Nie udało się odczytać historii')
    except RuntimeError as error:
        return _error(request, 409, "snapshot_changed", "Dane zmieniły się między stronami", current_snapshot_id=str(error))


async def export_json_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        query = _params(request, {"include_raw"})
        include_raw = query.get("include_raw", "0")
        if include_raw not in {"0", "1"}:
            raise ValueError("include_raw musi mieć wartość 0 albo 1")
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    project = context['capture_export']()
    try:
        history = await asyncio.to_thread(context['engine_store'].read_history, limit=200, departures_only=True, include_state=True, publication_filter=_history_filter(context))
    except (EngineStoreError, sqlite3.Error):
        return _error(request,503,'storage_unavailable','Nie udało się odczytać archiwum')
    context = project(history['state'])
    freshness = _freshness(context)
    events = [_event(context, event, "full", include_raw == "1") for event in _events(context)]
    events.sort(key=lambda item: item["event_id"])
    payload = {
            "api_version": API_VERSION,
            "data_updated_at": freshness["data_updated_at"],
            "data_status": freshness["data_status"],
            "sources": freshness["sources"],
            "objects": [_object(item, "full") for item in context["objects"]()],
            "events": events,
            "geometries": _map_geometries(context, events),
            "history": {"scope": "last_200_departures", **_history_metadata(history), 'items': history['items'],
                        'total': history['total'], 'archive_total': history['archive_total'],
                        'count': len(history['items']), 'truncated': history['total']>len(history['items']),
                        'snapshot_id': _history_snapshot(history), 'archive_url': '/api/v1/history'},
            'state_revision': history['state_revision'],
        }
    payload['snapshot_id'] = _snapshot(payload)
    return _response(request, payload)


def _csv_safe(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


async def export_csv_handler(request):
    context = request.app[API_CONTEXT_KEY]
    history = None
    try:
        query = _params(request, {"dataset"})
        dataset = query.get("dataset", "events")
        if dataset == "events":
            rows = [_event(context, event, "full") for event in _events(context)]
        elif dataset == "objects":
            rows = [_object(item, "full") for item in context["objects"]()]
        elif dataset == "history":
            history = await asyncio.to_thread(context['engine_store'].read_history, limit=200, departures_only=True, publication_filter=_history_filter(context))
            rows = history['items']
        else:
            raise ValueError("dataset musi mieć wartość events, objects albo history")
    except (EngineStoreError, sqlite3.Error):
        return _error(request,503,'storage_unavailable','Nie udało się odczytać archiwum')
    except ValueError as error:
        return _error(request, 400, "invalid_query", str(error))
    rows = public_data(rows)
    if dataset == 'objects':
        rows = [dict(row, catalog_source='Pochodzenie i licencje opisano osobno dla pól w provenance.', catalog_source_url='/readme', catalog_license='Zależna od źródła pola', catalog_license_url='https://creativecommons.org/licenses/by/4.0/', catalog_disclaimer='CC BY 4.0 dotyczy danych tekstowych RadioPolski. Dane mogą zawierać błędy i braki.') for row in rows]
    columns = sorted({key for row in rows for key in row})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    body = output.getvalue().encode("utf-8-sig")
    headers = dict(COMMON_HEADERS, ETag=_etag(body), **{"Content-Disposition": f'attachment; filename="osd-{dataset}.csv"'})
    if history is not None:
        headers.update({'X-History-Total': str(history['total']),
                        'X-History-Truncated': str(history['total']>len(rows)).lower(),
                        'Link': '</api/v1/history>; rel="related"'})
    if _etag_matches(request, headers["ETag"]):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type="text/csv", charset="utf-8", headers=headers)


async def openapi_handler(request):
    path = Path(request.app[API_CONTEXT_KEY]["root"]) / "docs" / "openapi-v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _error(request, 503, "documentation_unavailable", "Dokumentacja OpenAPI jest niedostępna")
    return _response(request, payload, document=True)


async def docs_handler(request):
    path = Path(request.app[API_CONTEXT_KEY]["root"]) / "web" / "api-docs" / "index.html"
    if not path.is_file():
        raise web.HTTPNotFound()
    return await community.html_response(path, request.app[API_CONTEXT_KEY]['config']()['profile'], headers={"Cache-Control": "no-cache"})


async def example_handler(request):
    path = Path(request.app[API_CONTEXT_KEY]["root"]) / "web" / "examples" / "osd-display.html"
    if not path.is_file():
        raise web.HTTPNotFound()
    return await community.html_response(path, request.app[API_CONTEXT_KEY]['config']()['profile'], headers={"Cache-Control": "no-cache"})


async def locations_example_handler(request):
    path = Path(request.app[API_CONTEXT_KEY]["root"]) / "web" / "examples" / "locations.html"
    if not path.is_file():
        raise web.HTTPNotFound()
    return await community.html_response(path, request.app[API_CONTEXT_KEY]['config']()['profile'], headers={"Cache-Control": "no-cache"})


async def history_example_handler(request):
    path = Path(request.app[API_CONTEXT_KEY]['root']) / 'web/examples/history.html'
    return await community.html_response(path, request.app[API_CONTEXT_KEY]['config']()['profile'], headers={'Cache-Control': 'no-cache'})


async def options_handler(request):
    return web.Response(status=204, headers=COMMON_HEADERS)


async def diagnostics_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        allowed = {'source', 'status', 'before', 'limit', 'from', 'to'}
        if set(request.query) - allowed:
            raise ValueError('Nieznany parametr diagnostyki')
        limit = int(request.query.get('limit', '50'))
        before = int(request.query['before']) if 'before' in request.query else None
        source, status = request.query.get('source'), request.query.get('status')
        def diagnostic_time(name):
            if name not in request.query: return None
            value=datetime.fromisoformat(request.query[name].replace('Z','+00:00'))
            if value.tzinfo is None: raise ValueError('Data wymaga strefy czasowej')
            return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')
        start, stop=diagnostic_time('from'), diagnostic_time('to')
        if start is not None and stop is not None and start > stop:
            raise ValueError('Początek przedziału nie może być późniejszy niż koniec')
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise ValueError('Limit: od 1 do 200; kursor musi być dodatni')
        if source is not None and source not in {'pge','tauron','energa','enea','stoen'}:
            raise ValueError('Nieznany operator')
        if status is not None and status not in {'running','success','failed','interrupted'}:
            raise ValueError('Nieznany wynik próby')
    except ValueError as error:
        return _error(request, 400, 'invalid_query', str(error))
    result = await context['diagnostics'].read(source=source, status=status, before=before, limit=limit, start=start, stop=stop)
    if result is None:
        return _error(request, 503, 'diagnostics_unavailable', 'Rejestr diagnostyczny jest chwilowo niedostępny')
    return _response(request, dict(result, api_version='1',
                                  diagnostics_degraded=context['diagnostics'].error is not None,
                                  diagnostics_error_code=context['diagnostics'].error))


async def quality_handler(request):
    context = request.app[API_CONTEXT_KEY]
    try:
        if set(request.query) - {'limit','offset','snapshot_id'}:
            raise ValueError('Nieznany parametr raportu jakości')
        limit, offset = int(request.query.get('limit','50')), int(request.query.get('offset','0'))
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError('Limit: od 1 do 200; offset nie może być ujemny')
    except ValueError as error:
        return _error(request, 400, 'invalid_query', str(error))
    try:
        value = await context['quality'].get(deepcopy(context['config']()['teryt_locations']))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return _error(request, 503, 'quality_unavailable', 'Raport jakości jest chwilowo niedostępny')
    snapshot = value['snapshot_id']
    if request.query.get('snapshot_id', snapshot) != snapshot:
        return _error(request, 409, 'snapshot_changed', 'Katalog zmienił się. Pobierz raport od początku')
    result = {key:item for key,item in value.items() if key != 'items'}
    result.update(api_version='1', snapshot_id=snapshot, offset=offset, limit=limit,
                  items=value['items'][offset:offset+limit])
    result['count'] = len(result['items'])
    result['next'] = ('/api/v1/diagnostics/quality?'+urlencode(dict(limit=limit, offset=offset+limit, snapshot_id=snapshot))
                      if offset+limit < len(value['items']) else None)
    return _response(request, result)


async def not_found_handler(request):
    return _error(request, 404, "not_found", "Nie znaleziono punktu końcowego API")


@web.middleware
async def cors_error_middleware(request, handler):
    if not request.path.startswith("/api/v1"):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPException as error:
        response = _error(request, error.status, "http_error", error.reason)
    for key, value in COMMON_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def register_api_v1(app, context):
    app[API_CONTEXT_KEY] = context
    app.middlewares.append(cors_error_middleware)
    routes = [
        ("/api/v1", root_handler),
        ("/api/v1/", root_handler),
        ("/api/v1/summary", summary_handler),
        ("/api/v1/sources", sources_handler),
        ("/api/v1/diagnostics/scans", diagnostics_handler),
        ("/api/v1/diagnostics/quality", quality_handler),
        ("/api/v1/locations", locations_handler),
        ("/api/v1/locations/{object_id}", location_detail_handler),
        ("/api/v1/localities", localities_handler),
        ("/api/v1/localities/{city_sym}", locality_detail_handler),
        ("/api/v1/map", map_handler),
        ("/api/v1/objects", objects_handler),
        ("/api/v1/objects/{object_id}", object_detail_handler),
        ("/api/v1/objects/{object_id}/location-review", location_review_handler),
        ("/api/v1/events", events_handler),
        ("/api/v1/events/{event_id}", event_detail_handler),
        ("/api/v1/display/{object_id}", display_handler),
        ("/api/v1/history", history_handler),
        ("/api/v1/export.json", export_json_handler),
        ("/api/v1/export.csv", export_csv_handler),
        ("/api/v1/openapi.json", openapi_handler),
    ]
    for path, handler in routes:
        app.router.add_get(path, handler)
    app.router.add_get("/api-docs/", docs_handler)
    app.router.add_get("/examples/osd-display.html", example_handler)
    app.router.add_get("/examples/locations.html", locations_example_handler)
    app.router.add_get("/examples/history.html", history_example_handler)
    app.router.add_get("/api/v1/{path:.*}", not_found_handler)
    app.router.add_route("OPTIONS", "/api/v1", options_handler)
    app.router.add_route("OPTIONS", "/api/v1/{path:.*}", options_handler)
