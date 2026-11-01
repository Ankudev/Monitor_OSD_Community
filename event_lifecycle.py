"""Brak dopasowania nie jest potwierdzeniem przywrócenia zasilania."""

from copy import deepcopy


def reconcile(previous, matched, source_ids, missing, changed_objects=()):
    """Dwa poprawne odczyty potwierdzają brak. Błędy nie trafiają do tej funkcji."""
    current = deepcopy(matched)
    counters = {}
    departures = []
    for key, old in previous.items():
        if key in current or str(old.get("object_id")) in changed_objects:
            continue
        if old.get("status") in {"cancelled", "resolved"}:
            continue
        source_id = str(old.get("source_event_id") or old.get("pge_uuid") or "").lower()
        reason = "out_of_scope" if source_id in source_ids else "not_published"
        before = missing.get(key, {})
        count = int(before.get("count", 0)) + 1 if before.get("reason") == reason else 1
        if count < 2:
            counters[key] = {"count": count, "reason": reason}
            current[key] = deepcopy(old)
            current[key]["verification_pending"] = True
        else:
            departed = deepcopy(old)
            departed.pop("verification_pending", None)
            departed["status"] = reason
            departed["resolution_reason"] = reason
            departures.append((reason, departed))
    return current, counters, departures
