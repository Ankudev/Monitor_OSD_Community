"""Tworzy odrębne demo bez pobierania danych i nadpisywania istniejących plików."""
import argparse
import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publication_policy as publication
from community import NOTICE

SOURCE = 'https://github.com/Ankudev/Monitor_OSD_Community/tree/main/examples/demo'


def source_entry():
    return {'url': SOURCE, 'review_status': 'approved', 'publication_mode': 'public', 'license': 'MIT',
                'basis': 'Autorskie dane syntetyczne do prezentacji i testów projektu.',
                'notice': NOTICE, 'actions': {a: a not in ('collect', 'notify') for a in publication.ACTIONS}}


def initialize(destination):
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Katalog nie jest pusty. Wskaż nowy katalog danych demo.')
    root.mkdir(parents=True, exist_ok=True)
    (root/'data').mkdir()
    cities = json.loads((ROOT/'examples/demo/cities.json').read_text(encoding='utf-8'))
    objects = [{'id': 'demo-'+slug, 'name': city+' — przykład', 'city_sym': f'990{i:04d}',
                    'street_sym': '', 'latitude': lat, 'longitude': lon,
                    'location_audit': {'status': 'not_checked', 'requires_review': True}}
               for i, (slug, city, woj, lat, lon) in enumerate(cities, 1)]
    with closing(sqlite3.connect(root/'data/teryt.db')) as db:
        db.executescript('CREATE TABLE simc(sym TEXT PRIMARY KEY,nazwa TEXT,woj TEXT,pow TEXT,gmi TEXT);'
                         'CREATE TABLE ulic(sym_ul TEXT,sym TEXT,cecha TEXT,nazwa_1 TEXT,nazwa_2 TEXT);')
        db.executemany('INSERT INTO simc VALUES(?,?,?,?,?)',
                       [(obj['city_sym'], row[1], row[2], '00', '00') for obj, row in zip(objects, cities)])
        db.commit()
    config = {'profile': 'demo', 'web_host': '127.0.0.1', 'web_port': 8080,
                  'enabled_sources': list(publication.OPERATORS), 'notifications_enabled': False,
                  'our_api_url': '', 'geoportal_enabled': False, 'teryt_locations': [],
                  'geocoding_user_agent': 'Monitor-OSD-Community/0.1 (demo: network disabled)'}
    for name, data in [('config.json', config), ('objects.json', objects)]:
        (root/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    policy = publication.PublicationPolicy(root/'data/publication-policy.sqlite3')
    policy.initialize()
    for identifier in ['demo_locations', *publication.OPERATORS]:
        policy.write('source', identifier, source_entry(), policy.snapshot().revision, actor='demo-initializer')
    for obj in objects:
        fields = {key: {'source_id': 'demo_locations', 'source_url': SOURCE, 'acquired_at': None,
                           'value_sha256': publication.digest(value), 'method': 'Autorski przykład; punkt orientacyjny, kody syntetyczne.', 'modified': True}
                  for key, value in obj.items() if key in publication.FIELDS}
        policy.write('object', obj['id'], {'visibility': 'public', 'fields': fields, 'reason': 'Jawny przykład demonstracyjny.'},
                     policy.snapshot().revision, actor='demo-initializer')
    asyncio.run(refresh(root))
    return root


async def refresh(root, now=None, application=None):
    """Odnawia scenariusz; stałe ID, historia inicjowana tylko raz."""
    import app
    from engine_store import EngineStore, history_record
    now = now or datetime.now(timezone.utc)
    root = Path(root)
    objects = json.loads((root/'objects.json').read_text(encoding='utf-8'))
    events = {}
    labels = ['Zdarzenie aktywne', 'Planowane wyłączenie', 'Zdarzenie zakończone', 'Odwołany komunikat', 'Dopasowanie niepewne']
    for i, obj in enumerate(objects[:10]):
        source = publication.OPERATORS[i % 5]
        phase = i % 5
        start = now + timedelta(hours=[-1, 12, -5, 4, -1][phase])
        stop = start + timedelta(hours=3)
        key = f'{source}:demo-{i}:object:{obj["id"]}'
        events[key] = {'event_key': key, 'source': source, 'operator': app.SOURCE_LABELS[source],
                           'source_event_id': f'demo-{i}', 'object_id': obj['id'], 'type': 'planned' if phase in (1, 3) else 'unplanned',
                           'status': 'cancelled' if phase == 3 else 'active', 'revoked': phase == 3,
                           'city_sym': obj['city_sym'], 'city': obj['name'].split(' — ')[0], 'configured_name': obj['name'],
                           'street_sym': '', 'object_latitude': obj['latitude'], 'object_longitude': obj['longitude'],
                           'object_house_number': None, 'location_review_required': True, 'match_confidence': 'city_text',
                           'matched_locations': [], 'match_evidence': {'scope': 'candidate', 'point_confirmed': False},
                           'description': f'SYMULACJA: {labels[phase]}. {NOTICE}',
                           'start_at': start.isoformat(), 'stop_at': stop.isoformat(), 'periods': [], 'coordinates': [],
                           'source_url': SOURCE, 'demo': True}
    store = application[app.ENGINE_STORE_KEY] if application is not None else EngineStore(root/'data/engine.sqlite3', root/'state.json')
    try:
        previous = application[app.STATE_KEY] if application is not None else store.load_or_migrate(app.empty_state)[0]
        state = {'saved_at': now.isoformat(), 'events': events, 'demo': True,
                     'sources': {source: {'last_success_at': now.isoformat(), 'last_attempt_at': now.isoformat(),
                                           'source_event_count': 2, 'error': None} for source in publication.OPERATORS}}
        history = [history_record('new', event, None, now.isoformat()) for event in events.values()] if not previous['events'] else []
        if application is None:
            await store.write_state(state, history_records=history)
        else:
            await app.persist_state(application, state, history_records=history)
            application[app.STATE_KEY] = state
            application[app.RUNTIME_KEY]['events'] = events
            for source, values in state['sources'].items():
                application[app.RUNTIME_KEY]['sources'][source].update(values)
            app.refresh_runtime_health(application)
    finally:
        if application is None:
            await store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT/'.runtime/demo')
    args = parser.parse_args()
    try:
        print(initialize(args.data_dir))
    except ValueError as error:
        parser.exit(2, str(error)+'\n')
