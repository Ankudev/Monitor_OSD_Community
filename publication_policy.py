"""Pochodzenie wartości i decyzje publikacji, niezależne od poprawności GPS."""
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from aiohttp import web

KEY = web.AppKey('publication_policy', object)
CURRENT = ContextVar('publication_view', default=None)
FIELDS = frozenset({'name', 'city_sym', 'street_sym', 'house_number', 'latitude', 'longitude'})
ACTIONS = frozenset({'collect', 'store', 'publish', 'export', 'notify', 'backup'})
OPERATORS = ('pge', 'tauron', 'energa', 'enea', 'stoen')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('Niepoprawny adres źródła.')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Źródło wymaga adresu HTTPS bez danych logowania.')
    return value


def date(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Data wymaga strefy czasowej.')
    return parsed.timestamp()


def valid(entry, now):
    return isinstance(entry, dict) and (entry.get('valid_until') is None or date(entry['valid_until']) > now)


class PolicyView:
    enforced = True
    def __init__(self, revision, sources, objects, *, available=True):
        self.revision = revision
        self.sources = sources
        self.decisions = objects
        self.available = available
        self.now = datetime.now(timezone.utc).timestamp()
        self.operation = 'publish'
        # Upływ ważności zmienia token także bez zapisu administratora.
        validity = [valid(x, self.now) for x in sources.values()]
        validity += [valid(x, self.now) for x in objects.values()]
        validity += [valid(field, self.now) for obj in objects.values() for field in obj.get('fields', {}).values()]
        self.token = digest([revision, validity, available])[:24]

    def allows(self, source, action='publish'):
        entry = self.sources.get(source, {})
        allowed = bool(self.available and valid(entry, self.now) and entry.get('actions', {}).get(action) is True)
        if action == 'publish' and self.operation == 'export':
            allowed = allowed and entry.get('actions', {}).get('export') is True
        return allowed

    def public_object(self, obj):
        decision = self.decisions.get(str(obj.get('id') or obj.get('object_id') or ''), {})
        if decision.get('visibility') != 'public' or not valid(decision, self.now):
            return None
        fields = {}
        provenance = {}
        for field, approval in decision.get('fields', {}).items():
            if (field in FIELDS and field in obj and valid(approval, self.now)
                    and self.allows(approval.get('source_id')) and approval.get('value_sha256') == digest(obj[field])):
                fields[field] = deepcopy(obj[field])
                provenance[field] = {key: approval.get(key) for key in ('source_id', 'source_url', 'acquired_at', 'modified')}
        if not fields.get('name'):
            return None
        # Powiązane oznaczenia miejscowości i ulicy tylko dla dopuszczonych kodów.
        result = dict(id=str(obj.get('id') or obj.get('object_id')), city_sym='', street_sym='',
                      latitude=None, longitude=None) | fields
        if fields.get('city_sym'):
            result['city'] = obj.get('city', fields['city_sym'])
        if fields.get('street_sym'):
            result['street'] = obj.get('street', '')
        if fields.get('latitude') is None or fields.get('longitude') is None:
            result['latitude'] = result['longitude'] = None
        audit = obj.get('location_audit') or {}
        if audit:
            # Stan weryfikacji GPS pozostaje odrębny od decyzji publikacji.
            # Nie kopiujemy prywatnych uwag, załączników, ścieżek ani bindingu.
            status = audit.get('status')
            if status in ('verified','requires_review','not_checked'):
                result['location_audit'] = {'status':status,'requires_review':bool(audit.get('requires_review',status != 'verified'))}
        result['provenance'] = provenance
        return result

    def objects(self, objects):
        return [item for obj in objects if (item := self.public_object(obj)) is not None]

    def event_allowed(self, event, objects, action='publish'):
        source = event.get('source') or ('pge' if event.get('pge_event') else 'unknown')
        if not self.allows(source, action):
            return False
        obj = next((x for x in objects if str(x.get('id')) == str(event.get('object_id'))), None)
        public = self.public_object(obj) if obj else None
        if public is None:
            return False
        for key, field in [('configured_name', 'name'), ('object_latitude', 'latitude'),
                           ('object_longitude', 'longitude'), ('object_house_number', 'house_number'),
                           ('city_sym', 'city_sym'), ('street_sym', 'street_sym')]:
            if event.get(key) not in (None, '') and event.get(key) != public.get(field):
                return False
        return True

    def history_allowed(self, source, object_id, before, after, legacy, objects):
        try:
            values = [json.loads(x) for x in (before, after) if x is not None]
            if legacy is not None:
                # Starszy surowy rekord ma własny zakres; nie ujawniamy go przez zgodę dla bieżącego widoku.
                return False
            return bool(values) and all(self.event_allowed(value, objects) for value in values)
        except (ValueError, TypeError, KeyError):
            return False

    def public_sources(self):
        return [{'source': name, 'publication_mode': entry.get('publication_mode', 'restricted'),
                 'review_status': entry.get('review_status', 'pending'),
                 'enabled': self.allows(name, 'collect'),
                 'url': entry.get('url'), 'license': entry.get('license'),
                 'notice': entry.get('notice', '')}
                for name, entry in sorted(self.sources.items())]


class PublicationPolicy:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError('Magazyn polityki nie może być dowiązaniem.')
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS policy_meta (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO policy_meta VALUES(1,1,0);
                CREATE TABLE IF NOT EXISTS policy_entries (kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS policy_changes (revision INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL);
            ''')
            connection.commit()
        self.path.chmod(0o600)

    def snapshot(self):
        try:
            if self.path.is_symlink():
                raise ValueError('Dowiązanie magazynu.')
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=3)) as connection:
                connection.execute('BEGIN')
                version, revision = connection.execute('SELECT version,revision FROM policy_meta WHERE id=1').fetchone()
                if version != 1:
                    raise ValueError('Nieobsługiwany schemat.')
                minimum_file = os.environ.get('PGE_PUBLICATION_MIN_REVISION_FILE')
                if minimum_file and revision < int(Path(minimum_file).read_text().strip()):
                    raise ValueError('Odtworzono starszą decyzję publikacji.')
                entries = connection.execute('SELECT kind,id,payload FROM policy_entries ORDER BY kind,id').fetchall()
                values = {'source': {}, 'object': {}}
                for kind, identifier, payload in entries:
                    entry = json.loads(payload)
                    validate_entry(kind, identifier, entry)
                    values[kind][identifier] = entry
                return PolicyView(revision, values['source'], values['object'])
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return PolicyView(-1, {}, {}, available=False)

    def write(self, kind, identifier, entry, expected_revision, *, actor='administrator'):
        validate_entry(kind, identifier, entry)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                version, revision = connection.execute('SELECT version,revision FROM policy_meta WHERE id=1').fetchone()
                if version != 1 or type(expected_revision) is not int or expected_revision != revision:
                    raise ValueError('Rewizja polityki zmieniła się. Wczytaj decyzje ponownie.')
                previous = connection.execute('SELECT payload FROM policy_entries WHERE kind=? AND id=?', (kind,identifier)).fetchone()
                payload = encoded(entry)
                if previous and previous[0] == payload:
                    return revision
                minimum_file = os.environ.get('PGE_PUBLICATION_MIN_REVISION_FILE')
                if minimum_file:
                    # Kotwica poza odtwarzaną migawką. Awaria przed COMMIT zamyka odczyt.
                    anchor = Path(minimum_file)
                    if anchor.is_symlink() or anchor.parent.is_symlink():
                        raise ValueError('Kotwica rewizji nie może być dowiązaniem.')
                    minimum = int(anchor.read_text().strip())
                    temporary = anchor.with_name(anchor.name+'.tmp')
                    with temporary.open('w',encoding='ascii') as stream:
                        stream.write(str(max(minimum,revision+1)))
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.chmod(0o600)
                    os.replace(temporary,anchor)
                connection.execute('INSERT OR REPLACE INTO policy_entries VALUES(?,?,?)', (kind,identifier,payload))
                connection.execute('UPDATE policy_meta SET revision=revision+1 WHERE id=1')
                connection.execute('INSERT INTO policy_changes VALUES(?,?,?,?,?,?,?)',
                                   (revision+1,utc_now(),actor,kind,identifier,previous[0] if previous else None,payload))
        return revision+1


def validate_entry(kind, identifier, entry):
    if kind not in ('source', 'object') or not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
        raise ValueError('Niepoprawny typ lub identyfikator decyzji.')
    if not isinstance(entry, dict) or len(encoded(entry)) > 32000:
        raise ValueError('Niepoprawna treść decyzji.')
    date(entry.get('valid_until'))
    if kind == 'source':
        if not re.fullmatch('[a-z0-9_]{1,64}', identifier):
            raise ValueError('Niepoprawny identyfikator źródła.')
        if entry.get('review_status') not in ('pending', 'approved', 'restricted'):
            raise ValueError('Niepoprawny stan oceny źródła.')
        if entry.get('publication_mode') not in ('links_only', 'restricted', 'public'):
            raise ValueError('Niepoprawny tryb źródła.')
        if not isinstance(entry.get('actions'), dict) or set(entry['actions']) != ACTIONS or any(type(v) is not bool for v in entry['actions'].values()):
            raise ValueError('Wymagana jest osobna decyzja dla każdej czynności.')
        if entry['publication_mode'] != 'public' and any(entry['actions'][x] for x in ('publish', 'export', 'notify')):
            raise ValueError('Tryb ograniczony nie pozwala udostępniać danych.')
        if entry['actions']['collect'] and not entry['actions']['store']:
            raise ValueError('Pobieranie wymaga decyzji przechowywania.')
        url(entry.get('url'))
        if not isinstance(entry.get('basis'), str) or not 10 <= len(entry['basis']) <= 3000:
            raise ValueError('Podaj podstawę lub udokumentowaną decyzję dalszego użycia.')
        if entry.get('review_status') != 'approved' and any(entry['actions'][x] for x in ('collect','publish','export')) and entry.get('interim_owner_decision') is not True:
            raise ValueError('Nierozstrzygnięte źródło wymaga jawnej decyzji właściciela o dalszym działaniu.')
    else:
        if set(entry) - {'visibility', 'fields', 'reason', 'valid_until'} or entry.get('visibility') not in ('private','public'):
            raise ValueError('Niepoprawna widoczność obiektu.')
        fields = entry.get('fields')
        if not isinstance(fields, dict) or set(fields) - FIELDS:
            raise ValueError('Niepoprawna lista pól obiektu.')
        for approval in fields.values():
            if not isinstance(approval, dict) or not re.fullmatch('[0-9a-f]{64}', str(approval.get('value_sha256',''))):
                raise ValueError('Decyzja musi wskazywać skrót zatwierdzonej wartości.')
            if not re.fullmatch('[a-z0-9_]{1,64}', str(approval.get('source_id',''))):
                raise ValueError('Brak źródła pola.')
            url(approval.get('source_url'))
            date(approval.get('valid_until'))
            date(approval.get('acquired_at'))
            if not isinstance(approval.get('method'), str) or not approval['method'].strip():
                raise ValueError('Podaj metodę i dowód kwalifikacji pola.')


def current():
    return CURRENT.get()


@web.middleware
async def middleware(request, handler):
    view = await __import__('asyncio').to_thread(request.app[KEY].snapshot)
    if request.path.endswith(('/export.json','/export.csv')):
        view.operation = 'export'
    token = CURRENT.set(view)
    try:
        if not view.enforced:
            return await handler(request)
        response = await handler(request)
        if request.path.startswith('/api/'):
            response.headers['X-Publication-Revision'] = view.token
            response.headers['Cache-Control'] = 'no-store'
            latest = await __import__('asyncio').to_thread(request.app[KEY].snapshot)
            if latest.token != view.token and request.method in ('GET','HEAD'):
                return web.json_response({'error':'Zakres publikacji zmienił się. Odśwież widok.'}, status=409,
                                         headers={'Cache-Control':'no-store'})
        return response
    finally:
        CURRENT.reset(token)


def public_metadata(view):
    return {'revision': view.token, 'sources': view.public_sources()}
