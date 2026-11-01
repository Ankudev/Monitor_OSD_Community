"""Raport offline: wskazuje brak dowodu, nigdy nie zatwierdza obiektu."""
import asyncio
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from address_matching import house_number, address_verified
from location_validation import coordinates
from matching_evidence import location_verification
from scan_diagnostics import fingerprint, utc

LABELS = {
    'duplicate_id': ('error', 'Powtórzony identyfikator obiektu.', 'Rozdziel obiekty po sprawdzeniu źródła danych.'),
    'missing_id': ('error', 'Brak identyfikatora obiektu.', 'Nadaj trwały identyfikator.'),
    'invalid_gps': ('error', 'Nieprawidłowe współrzędne.', 'Sprawdź obie współrzędne i ich zakres.'),
    'missing_gps': ('warning', 'Brak współrzędnych.', 'Ustal fizyczny punkt obiektu.'),
    'invalid_city': ('error', 'Miejscowości nie ma w używanym katalogu SIMC.', 'Sprawdź kod i wersję TERYT.'),
    'invalid_street': ('error', 'Ulica nie należy do wskazanej miejscowości.', 'Sprawdź parę SIMC/ULIC.'),
    'invalid_number': ('error', 'Numer domu ma niepoprawny format.', 'Sprawdź fizyczny adres.'),
    'missing_number': ('info', 'Nie zapisano numeru domu.', 'Uzupełnij, jeśli obiekt ma numer adresowy.'),
    'location_unverified': ('warning', 'Brak aktualnego potwierdzenia położenia GPS.', 'Sprawdź źródła i powiązanie audytu z obiektem.'),
    'address_unverified': ('info', 'Brak aktualnego dowodu dokładnego adresu.', 'Zapisz źródło potwierdzenia adresu, jeśli jest dostępne.'),
}


def report(objects, teryt_path):
    issues = []
    counts = Counter(str(o.get('id') or '') for o in objects)
    path = Path(teryt_path)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as c:
        for obj in objects:
            object_id = str(obj.get('id') or '')
            codes = []
            if not object_id: codes.append('missing_id')
            elif counts[object_id] > 1: codes.append('duplicate_id')
            try:
                lat, lon = coordinates(obj.get('latitude'), obj.get('longitude'))
                if lat is None: codes.append('missing_gps')
            except ValueError: codes.append('invalid_gps')
            city, street = str(obj.get('city_sym') or ''), str(obj.get('street_sym') or '')
            if not c.execute('SELECT 1 FROM simc WHERE sym=? LIMIT 1', (city,)).fetchone(): codes.append('invalid_city')
            if street and not c.execute('SELECT 1 FROM ulic WHERE sym=? AND sym_ul=? LIMIT 1', (city, street)).fetchone(): codes.append('invalid_street')
            try:
                if not house_number(obj.get('house_number')): codes.append('missing_number')
            except ValueError: codes.append('invalid_number')
            try:
                if location_verification(obj) != 'recorded_audit': codes.append('location_unverified')
                if not address_verified(obj): codes.append('address_unverified')
            except (ValueError, TypeError):
                codes.extend(['location_unverified', 'address_unverified'])
            for code in codes:
                severity, message, next_step = LABELS[code]
                issues.append(dict(object_id=object_id, code=code, severity=severity, message=message, next_step=next_step))
    issues.sort(key=lambda x: ({'error':0,'warning':1,'info':2}[x['severity']], x['object_id'], x['code']))
    return {'catalog_fingerprint':fingerprint(objects), 'object_count':len(objects), 'issue_count':len(issues),
            'summary':dict(Counter(i['code'] for i in issues)), 'items':issues,
            'scope':'catalog_validation_not_electrical_supply', 'external_requests':0}


class QualityCache:
    def __init__(self, teryt_path):
        self.path = Path(teryt_path)
        self.key = None
        self.value = None
        self.lock = asyncio.Lock()

    async def get(self, objects):
        async with self.lock:
            stat = self.path.stat()
            key = (fingerprint(objects), stat.st_mtime_ns, stat.st_size)
            if key != self.key:
                value = await asyncio.to_thread(report, objects, self.path)
                value['generated_at'] = utc()
                value['snapshot_id'] = fingerprint(key)
                self.key, self.value = key, value
            return self.value


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--objects', type=Path, required=True)
    parser.add_argument('--teryt', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(report(json.loads(args.objects.read_text(encoding='utf-8-sig')), args.teryt), ensure_ascii=False, indent=2))
