"""Konserwatywne porównanie numerów. Nie zgaduje adresu z GPS ani nazwy."""

import re


NUMBER = r"[0-9]{1,9}(?:[A-Z]{1,2})?(?:/[0-9]{1,9}[A-Z]{0,2})?"
BINDING_FIELDS = ("id", "city_sym", "street_sym", "house_number", "latitude", "longitude")


def house_number(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Numer domu musi być tekstem lub null")
    value = value.strip().upper()
    if not value:
        return None
    if len(value) > 32 or not re.fullmatch(NUMBER, value):
        raise ValueError("Nieprawidłowy numer domu (np. 12, 12A, 12/3; maksymalnie 32 znaki)")
    return value


def address_binding(location):
    return {key: house_number(location.get(key)) if key == "house_number" else location.get(key)
            for key in BINDING_FIELDS}


def address_verified(location):
    audit = location.get("address_audit") or {}
    return (isinstance(audit, dict) and audit.get("status") == "verified"
            and audit.get("object_address_confirmed") is True
            and audit.get("street_context_confirmed") is True
            and bool(audit.get("checked_at"))
            and isinstance(audit.get("sources"), list) and bool(audit["sources"])
            and all(isinstance(source, dict) and source.get("reference")
                    and isinstance(source.get("sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                    for source in audit["sources"])
            and audit.get("binding") == address_binding(location)
            and bool(location.get("house_number"))
            and not (location.get("location_audit") or {}).get("requires_review"))


def public_address_audit(location):
    # Dokumenty i ich lokalne ścieżki nie trafiają do publicznego API.
    audit = location.get("address_audit") or {}
    verified = address_verified(location) if "binding" in audit else location.get("address_verified") is True
    return {"status": "verified" if verified else
            "requires_review" if audit.get("status") == "requires_review" else "unknown",
            "checked_at": audit.get("checked_at"), "reason": audit.get("review_reason")}


def parse_numbers(value):
    result = {"original": value, "complete": False, "items": [], "excluded": [],
              "parity": None, "unrecognized": []}
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            return result
        value = ", ".join(value)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return result
    text = value.strip().upper().replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(?:OD\s+)?([0-9]{1,9})\s+DO\s+([0-9]{1,9})\b", r"\1-\2", text)
    text = re.sub(r"\b(?:NR|NUMERY|NUMERÓW)\.?\s*", "", text)
    parity = re.findall(r"\b(NIEPARZYSTE|PARZYSTE)\b", text)
    if len(set(parity)) > 1:
        result["unrecognized"] = [text]
        return result
    result["parity"] = parity[0] if parity else None
    text = re.sub(r"\b(?:NIEPARZYSTE|PARZYSTE)\b", "", text)
    halves = re.split(r"\b(?:OPRÓCZ|Z WYJĄTKIEM)\b", text)
    if len(halves) > 2:
        result["unrecognized"] = [text]
        return result
    for position, half in enumerate(halves):
        target = result["items" if position == 0 else "excluded"]
        tokens = [token.strip() for token in re.split(r"[,;]|\s+I\s+", half) if token.strip()]
        if not tokens:
            result["unrecognized"].append(half or "pusty zakres")
        for token in tokens:
            compact = re.sub(r"\s+", "", token)
            interval = re.fullmatch(r"([0-9]{1,9})-([0-9]{1,9})", compact)
            if interval and int(interval[1]) <= int(interval[2]):
                target.append({"from": int(interval[1]), "to": int(interval[2])})
            elif re.fullmatch(NUMBER, compact):
                target.append({"number": compact})
            else:
                result["unrecognized"].append(token)
    result["complete"] = bool(result["items"]) and not result["unrecognized"]
    return result


def _contains(items, number):
    return any(item.get("number") == number or
               (number.isdigit() and "from" in item and item["from"] <= int(number) <= item["to"])
               for item in items)


def compare_numbers(value, number):
    parsed = parse_numbers(value)
    number = house_number(number)
    if not number or not parsed["complete"]:
        return "unknown", parsed
    # Nie wyprowadzamy parzystości budynku 12A z parzystości liczby 12.
    if parsed["parity"] and not number.isdigit():
        return "unknown", parsed
    included = _contains(parsed["items"], number)
    if parsed["parity"]:
        included = included and int(number) % 2 == (1 if parsed["parity"] == "NIEPARZYSTE" else 0)
    return ("matches" if included and not _contains(parsed["excluded"], number)
            else "does_not_match"), parsed


def address_decision(event, location):
    decision = {"result": "unknown", "reason": "Brak zweryfikowanego adresu lub potwierdzonego formatu operatora."}
    if event.get("source") != "pge" or not address_verified(location):
        return decision
    addresses = (event.get("raw") or {}).get("addresses")
    if not isinstance(addresses, list) or not addresses:
        return decision
    results = []
    details = []
    for index, row in enumerate(addresses):
        detail = {"row_index": index, "object_city_sym": location.get("city_sym"),
                  "object_street_sym": location.get("street_sym"), "object_house_number": location.get("house_number")}
        details.append(detail)
        if not isinstance(row, dict) or not isinstance(row.get("teryt"), dict):
            results.append("unknown")
            detail["result"] = "unknown"
            continue
        teryt = row["teryt"]
        city = str(teryt.get("citySym") or "").strip()
        street = str(teryt.get("streetSymUl") or "").strip()
        detail.update(source_city_sym=city, source_street_sym=street)
        if not city.isdigit() or (street and not street.isdigit()):
            results.append("unknown")
        elif city != location["city_sym"]:
            results.append("does_not_match")
        elif street != str(location.get("street_sym") or ""):
            # Brak ULIC w komunikacie nie jest dowodem innej ulicy.
            results.append("does_not_match" if street and location.get("street_sym") else "unknown")
        else:
            result, parsed = compare_numbers(row.get("numbers"), location["house_number"])
            detail.update(numbers=parsed)
            results.append(result)
        detail["result"] = results[-1]
    result = "unknown" if "unknown" in results else "matches" if "matches" in results else "does_not_match"
    return {"result": result, "reason": {"matches": "Zweryfikowany adres jest objęty zakresem numerów PGE.",
            "does_not_match": "Kompletny wykaz adresów PGE wyklucza zweryfikowany adres obiektu.",
            "unknown": "Wykaz adresów jest niekompletny lub nie ma jednoznacznego kontekstu ulicy."}[result],
            "row_results": results, "rows": details,
            "unknown_row_count": results.count("unknown")}
