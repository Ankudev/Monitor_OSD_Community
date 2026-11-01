"""Niezmienne migawki historii i wspólna reprezentacja czasu zdarzeń."""
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from matching_evidence import match_details
from matching_explanation import explanation_details

LOCAL_TIMEZONE = ZoneInfo("Europe/Warsaw")


def event_time_warning(value):
    """Wykrywa niejednoznaczny lub nieistniejący czas lokalny bez przesunięcia względem UTC."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return None
    offsets = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=LOCAL_TIMEZONE, fold=fold)
        back = local.astimezone(timezone.utc).astimezone(LOCAL_TIMEZONE)
        if back.replace(tzinfo=None) == naive:
            offsets.add(local.utcoffset())
    if not offsets:
        return "nonexistent_local_time"
    return "ambiguous_local_time" if len(offsets) > 1 else None


def parse_event_datetime(value):
    """Czyta czas operatora. Czas bez strefy oznacza czas polski."""

    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        if event_time_warning(value) == "nonexistent_local_time":
            return None
        result = result.replace(tzinfo=LOCAL_TIMEZONE)
    return result


EVENT_START_FIELDS = ("startAt", "fromDate", "start", "start_at")
EVENT_STOP_FIELDS = ("stopAt", "toDate", "end", "stop_at")


def first_event_time_value(item, fields):
    for field in fields:
        value = item.get(field)
        if value not in (None, ""):
            return value
    return None


def event_time_windows(event):
    """Zwraca znormalizowane okna czasu bez zgadywania brakujących dat."""

    ranges = event.get("periods") if isinstance(event.get("periods"), list) else []
    if not ranges:
        ranges = [{"start_at": event.get("start_at"), "stop_at": event.get("stop_at")}]

    windows = []
    for item in ranges:
        if not isinstance(item, dict):
            continue
        raw_start = first_event_time_value(item, EVENT_START_FIELDS)
        raw_stop = first_event_time_value(item, EVENT_STOP_FIELDS)
        if raw_start in (None, "") and raw_stop in (None, ""):
            continue

        start = parse_event_datetime(raw_start)
        stop = parse_event_datetime(raw_stop)
        valid = True
        if raw_start not in (None, "") and start is None:
            valid = False
        if raw_stop not in (None, "") and stop is None:
            valid = False
        if start is not None and stop is not None and stop < start:
            valid = False

        windows.append(
            {
                "start_at": start.isoformat(timespec="seconds") if start else None,
                "stop_at": stop.isoformat(timespec="seconds") if stop else None,
                "valid": valid,
            }
        )
    return windows


def event_time_warnings(event):
    """Ostrzeżenia nie zmieniają surowego rekordu ani dowodu dopasowania."""
    values = [event.get("start_at"), event.get("stop_at"), event.get("updated_at")]
    for item in event.get("periods") or []:
        if isinstance(item, dict):
            values.extend((first_event_time_value(item, EVENT_START_FIELDS),
                           first_event_time_value(item, EVENT_STOP_FIELDS)))
    return sorted({warning for value in values if (warning := event_time_warning(value))})


def event_time_phase(event, now=None):
    """Oddziela status operatora od położenia zdarzenia na osi czasu."""

    if event.get("status") in {"cancelled", "resolved"}:
        return "closed"
    current = now or datetime.now().astimezone()
    intervals = []
    for item in event_time_windows(event):
        if not item["valid"]:
            continue
        start = parse_event_datetime(item["start_at"])
        stop = parse_event_datetime(item["stop_at"])
        if start or stop:
            intervals.append((start, stop))
    if not intervals:
        return "unknown"
    if any(
        (start is None or start <= current) and (stop is None or current <= stop)
        for start, stop in intervals
    ):
        return "ongoing"
    if any(start is not None and start > current for start, _ in intervals):
        return "upcoming"
    if all(stop is not None and stop < current for _, stop in intervals):
        return "overdue"
    return "unknown"


def _utc(value):
    """Czas zdarzenia operatora w UTC, niezależny od strefy hosta."""
    parsed = parse_event_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if parsed else None


def project_event(context, event, view="compact", include_raw=False):
    windows = []
    for window in context["event_time_windows"](event):
        windows.append(
            {
                "start_at": _utc(window.get("start_at")),
                "stop_at": _utc(window.get("stop_at")),
                "valid": bool(window.get("valid")),
            }
        )
    item = {
        "event_id": str(event.get("event_key") or ""),
        "object_id": str(event.get("object_id") or ""),
        "source": str(event.get("source") or "pge"),
        "operator": event.get("operator"),
        "source_event_id": str(event.get("source_event_id") or event.get("pge_uuid") or ""),
        "type": str(event.get("type") or "unknown"),
        "source_status": str(event.get("status") or "unknown"),
        "time_phase": context["event_time_phase"](event),
        "start_at": _utc(event.get("start_at")),
        "stop_at": _utc(event.get("stop_at")),
        "verification_pending": bool(event.get("verification_pending")),
        "match_confidence": event.get("match_confidence"),
        "match_evidence": match_details(event),
        "geometry_ref": event.get("geometry_ref"),
    }
    # Starsze historyczne JSON pozostają bez nowych, domniemanych dowodów.
    item['match_explanation'] = explanation_details(event, view)
    if view == "full":
        item.update(
            description=event.get("description"),
            configured_name=event.get("configured_name"),
            city=event.get("city"),
            city_sym=str(event.get("city_sym") or ""),
            street_sym=str(event.get("street_sym") or ""),
            object_latitude=event.get("object_latitude"),
            object_longitude=event.get("object_longitude"),
            object_house_number=event.get("object_house_number"),
            matched_locations=deepcopy(event.get("matched_locations") or []),
            coordinates=deepcopy(event.get("coordinates") or []),
            time_windows=windows,
            source_url=event.get("source_url"),
            source_updated_at=_utc((event.get("source_event") or {}).get("updated_at"))
            if isinstance(event.get("source_event"), dict)
            else None,
            location_review_required=bool(event.get("location_review_required")),
            revoked=bool(event.get("revoked")),
        )
    if include_raw:
        if event.get("pge_event") is not None:
            item["raw_scope"] = "full_record"
            item["raw"] = deepcopy(event.get("pge_event"))
        elif isinstance(event.get("source_event"), dict):
            item["raw_scope"] = "reduced_record"
            item["raw"] = deepcopy(event.get("source_event"))
        else:
            item["raw_scope"] = "unavailable"
            item["raw"] = None
    return item



def snapshot_event(event, recorded_at):
    at = parse_event_datetime(recorded_at)
    context = {
        "event_time_windows": event_time_windows,
        "event_time_phase": lambda item: event_time_phase(item, at) if at else "unknown",
    }
    result = project_event(context, event, "full")
    result["time_phase_at_recording"] = result["time_phase"]
    return result
