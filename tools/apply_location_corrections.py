"""Walidacja i atomowy zapis odebranych korekt na kopii lub zatrzymanej usłudze.

Nie łączy się z siecią. Manifest i dowody mają zostać przygotowane w odbiorze.
Domyślnie wykonuje tylko kontrolę; --output zapisuje nową kopię, nigdy wejście.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from address_matching import address_binding, address_verified
from matching_evidence import location_verification
from location_validation import validate_object_list
from location_review import atomic_json


def prepare(objects, manifest, database):
    validate_object_list(objects)
    result = deepcopy(objects)
    by_id = {str(obj['id']): obj for obj in result}
    touched = set()
    for source in manifest.get('evidence_files', []):
        if hashlib.sha256(Path(source['file']).read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Niezgodna suma pliku dowodowego')
    for change in manifest['changes']:
        oid = change['object_id']
        if oid in touched or oid not in by_id:
            raise ValueError(f'Nieznany lub powtórzony ID: {oid}')
        touched.add(oid)
        current = by_id[oid]
        if current != change['expected_object']:
            raise ValueError(f'Obiekt zmienił się po odbiorze: {oid}')
        allowed = {'city_sym', 'street_sym', 'house_number', 'latitude', 'longitude', 'location_audit', 'address_audit'}
        if set(change['fields']) - allowed:
            raise ValueError(f'Niedozwolone pola korekty: {oid}')
        current.update(deepcopy(change['fields']))
        audit = current.get('location_audit') or {}
        if audit.get('binding') != address_binding(current):
            raise ValueError(f'Audyt nie pasuje do docelowych pól: {oid}')
        evidence_hashes = {s['sha256'] for s in manifest.get('evidence_files', [])}
        for key in ('location_audit', 'address_audit'):
            candidate = current.get(key) or {}
            if candidate.get('status') == 'verified':
                sources = candidate.get('sources') or []
                if not sources or any(s.get('sha256') not in evidence_hashes for s in sources):
                    raise ValueError(f'Brak zachowanych dowodów audytu: {oid}')
                valid = location_verification(current) == 'recorded_audit' if key == 'location_audit' else address_verified(current)
                if not valid:
                    raise ValueError(f'Niepoprawne potwierdzenie audytu: {oid}')
    validate_object_list(result)
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        for obj in result:
            if db.execute('SELECT 1 FROM simc WHERE sym=?',(obj['city_sym'],)).fetchone() is None:
                raise ValueError(f'Nieznana miejscowość: {obj["id"]}')
            if obj.get('street_sym') and db.execute('SELECT 1 FROM ulic WHERE sym=? AND sym_ul=?',(obj['city_sym'],obj['street_sym'])).fetchone() is None:
                raise ValueError(f'Ulica nie należy do miejscowości: {obj["id"]}')
    if {str(o['id']) for o in objects} != {str(o['id']) for o in result}:
        raise ValueError('Zmiana ID podczas korekty')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--teryt-db', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    if hashlib.sha256(args.objects.read_bytes()).hexdigest() != manifest['catalog_sha256']:
        parser.error('Katalog zmienił się po przygotowaniu manifestu')
    objects = json.loads(args.objects.read_text(encoding='utf-8'))
    result = prepare(objects, manifest, args.teryt_db)
    if args.output:
        if args.output.exists() or args.output.resolve() in {args.objects.resolve(),args.manifest.resolve(),args.teryt_db.resolve()}:
            parser.error('Wyjście musi być nowym plikiem poza wejściami')
        atomic_json(args.output, result)
    print(json.dumps({'objects':len(result),'reviewed':len(manifest['changes']),'written':bool(args.output)}))


if __name__ == '__main__':
    main()
