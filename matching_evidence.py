"""Zakres przypisania komunikatu. Nie jest pomiarem zasilania obiektu."""

import hashlib
import json
import re
from datetime import datetime

from geometry import geometry_contains
from location_validation import coordinates
from address_matching import address_decision, address_binding


EVIDENCE_VERSION = 2


def source_map_geometry(source_event):
    if source_event.get("geometry") is not None:
        return source_event["geometry"]
    points = []
    for point in source_event.get("coordinates") or []:
        if not isinstance(point, dict):
            continue
        try:
            lat, lon = coordinates(point.get("latitude"), point.get("longitude"))
        except ValueError:
            continue
        if lat is not None and lon is not None:
            points.append([lon, lat])
    # Niezależne punkty pozostają punktami, również gdy jest ich więcej niż trzy.
    return {"type": "MultiPoint", "coordinates": points} if points else None


def geometry_reference(geometry):
    if not geometry:
        return None
    encoded = json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "geometry:" + hashlib.sha256(encoded.encode()).hexdigest()


def location_verification(location):
    audit = location.get("location_audit") or {}
    if not isinstance(audit, dict):
        return "not_audited"
    if audit.get("requires_review"):
        return "review_required"
    try:
        checked = datetime.fromisoformat(str(audit.get("checked_at") or "").replace("Z", "+00:00"))
        binding = address_binding(location)
    except (ValueError, TypeError):
        return "not_audited"
    sources = audit.get("sources")
    verified = (audit.get("status") == "verified" and audit.get("object_point_confirmed") is True
                and checked.tzinfo is not None and audit.get("binding") == binding
                and isinstance(sources, list) and bool(sources)
                and all(isinstance(source, dict) and isinstance(source.get("reference"), str)
                        and bool(source["reference"].strip()) and isinstance(source.get("sha256"), str)
                        and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) for source in sources))
    return "recorded_audit" if verified else "not_audited"


def public_location_audit(location):
    """Podgląd wyniku bez plików i prywatnych źródeł dowodowych."""
    audit = location.get("location_audit") or {}
    if not isinstance(audit, dict):
        return {}
    result = {key: audit[key] for key in (
        "status", "requires_review", "review_reason", "checked_at", "invalidated_at",
        "method", "radio_id", "source_url", "gps_region", "gps_commune",
    ) if key in audit}
    if audit.get("status") == "verified":
        result["status"] = "verified" if location_verification(location) == "recorded_audit" else "requires_review"
        if result["status"] != "verified":
            result["requires_review"] = True
    return result


def matching_evidence(source_event, location, confidence, decision=None):
    verification = location_verification(location)
    geometry = source_event.get("geometry")
    try:
        latitude, longitude = coordinates(location.get("latitude"), location.get("longitude"))
    except ValueError:
        latitude = longitude = None
    # Jedynie jawna geometria z walidowanego parsera określa obszar.
    # Lista punktów, centroid i nieudokumentowany Radius nie są takim dowodem.
    inside = (decision['details'].get('geometry', {}).get('contains', False) if decision is not None else
              geometry is not None and latitude is not None and longitude is not None
              and geometry_contains(latitude, longitude, geometry))
    confirmed = confidence == "spatial_polygon" and inside and verification == "recorded_audit"
    address = decision['details']['address'] if decision is not None else address_decision(source_event, location)
    if confidence == "exact_address" and address["result"] == "matches":
        confirmed = True
    reasons = {
        "exact_teryt": "Zgodny kod miejscowości lub ulicy; brak potwierdzenia konkretnego adresu lub punktu.",
        "text_address": "Zgodny tekst miejscowości lub ulicy; brak potwierdzenia adresu punktu.",
        "text_keyword": "Zgodne słowo kluczowe; to nie jest dowód objęcia punktu.",
        "spatial_nearby": "Punkt obiektu jest blisko punktu komunikatu; zasięg wyłączenia nie jest potwierdzony.",
        "spatial_radius": "Punkt mieści się w promieniu operatora; znaczenie promienia jako zasięgu wyłączenia nie jest potwierdzone.",
        "spatial_polygon": "Brak jawnej geometrii lub zweryfikowanej lokalizacji punktu.",
        "county_mass": "Komunikat dotyczy powiatu; brak potwierdzenia konkretnego punktu.",
        "address_geometry_conflict": "Adres i obszar operatora są sprzeczne; konieczny przegląd.",
    }
    reason = ("Punkt z zapisanym audytem znajduje się w jawnym obszarze komunikatu operatora."
              if confirmed else reasons.get(confidence, "Brak wystarczających dowodów objęcia punktu."))
    if confirmed and confidence == "exact_address":
        reason = address["reason"] + " To nie jest pomiar napięcia."
    if not confirmed and verification == "review_required":
        reason += " Lokalizacja wymaga przeglądu."
    elif not confirmed and verification == "not_audited":
        reason += " Brak zapisanego audytu lokalizacji."
    return {"version": EVIDENCE_VERSION, "scope": "confirmed_point" if confirmed else "candidate",
            "point_confirmed": bool(confirmed), "reason": reason, "location_verification": verification,
            "address_result": address["result"], "confirmation_basis": "address" if confirmed and confidence == "exact_address" else "geometry" if confirmed else None}


def match_details(event):
    evidence = event.get("match_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    if evidence.get("version") == EVIDENCE_VERSION and evidence.get("scope") in {"confirmed_point", "candidate"}:
        if evidence.get("scope") == "candidate":
            return {**evidence, "point_confirmed": False}
        if evidence.get("point_confirmed") is True and not event.get("location_review_required"):
            return dict(evidence)
    return {"version": EVIDENCE_VERSION, "scope": "unknown", "point_confirmed": False,
            "reason": "Starsze przypisanie nie ma dowodu objęcia punktu; oczekuje na automatyczny skan.",
            "location_verification": "unknown"}


def point_confirmed(event):
    return match_details(event)["point_confirmed"] is True and not event.get("verification_pending")
