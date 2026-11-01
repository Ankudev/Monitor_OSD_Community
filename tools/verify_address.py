"""Zapis dowodu adresu w kopii katalogu. Nie łączy się z serwerem."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from address_matching import address_binding, address_verified
from location_validation import validate_object_list


def verify(objects, evidence):
    validate_object_list(objects)
    item = next(row for row in objects if str(row["id"]) == str(evidence["object_id"]))
    if evidence["binding"] != address_binding(item):
        raise ValueError("Dowód dotyczy innych danych obiektu; sprawdź bieżący katalog")
    if evidence.get("object_address_confirmed") is not True or evidence.get("street_context_confirmed") is not True:
        raise ValueError("Brak dowodu powiązania obiektu z adresem i kontekstem ulicy")
    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Podaj co najmniej jedno zachowane źródło")
    for source in sources:
        path = Path(source["file"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"] or not source.get("reference"):
            raise ValueError("Niezgodny hash lub brak opisu źródła")
    audit = {**evidence, "status": "verified", "checked_at": datetime.now(timezone.utc).isoformat()}
    item["address_audit"] = audit
    if not address_verified(item):
        raise ValueError("Adres nie spełnia kryteriów; sprawdź numer i otwarty audyt lokalizacji")
    return objects


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.objects.resolve(), args.evidence.resolve()}:
        parser.error("Wyjście musi być nową kopią, nie plikiem wejściowym")
    objects = verify(json.loads(args.objects.read_text(encoding="utf-8")),
                     json.loads(args.evidence.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(objects, target, ensure_ascii=False, indent=2)
    print("Zweryfikowana kopia zapisana. Produkcja nie została zmieniona.")


if __name__ == "__main__":
    main()
