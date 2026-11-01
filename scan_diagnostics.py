"""Ograniczona historia prób OSD. Jej awaria nie unieważnia COMMIT silnika."""
import asyncio
import hashlib
import json
import re
import socket
import sqlite3
import ssl
import time
import uuid
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import aiohttp

CURRENT = ContextVar('osd_scan_diagnostics', default=None)
PUBLICATION_FIELDS = ('publication_updated_at', 'publication_timestamp_field',
                      'publication_observed_at', 'publication_time_status')
PARSER_VERSION = 'contracts:20261011:1'
LABELS = {
    'dns': 'Nie udało się ustalić adresu serwera.',
    'tls': 'Nie udało się zestawić bezpiecznego połączenia.',
    'connect_error': 'Nie udało się połączyć z serwerem.',
    'timeout': 'Przekroczono czas oczekiwania.',
    'http': 'Serwer zwrócił błąd HTTP.',
    'invalid_json': 'Odpowiedź nie jest poprawnym dokumentem JSON.',
    'contract': 'Odpowiedź nie spełnia kontraktu parsera.',
    'maintenance': 'Operator informuje o pracach serwisowych portalu.',
    'incomplete': 'Nie otrzymano kompletnego odczytu.',
    'conflicting_id': 'Operator zwrócił sprzeczne rekordy tego samego ID.',
    'catalog_changed': 'Katalog zmienił się podczas dopasowania.',
    'storage': 'Nie udało się zatwierdzić stanu w SQLite.',
    'interrupted': 'Próba nie ma potwierdzonego wyniku końcowego.',
    'client_retry_blocked': 'Dodatkowe żądanie PGE zostało zatrzymane przed wysłaniem.',
    'internal': 'Wewnętrzny błąd przetwarzania.',
}


class SourceContractError(ValueError):
    def __init__(self, message, code='contract'):
        super().__init__(message)
        self.code = code


class SourceRequestBudgetError(RuntimeError):
    pass


class SourceMaintenanceError(SourceContractError):
    def __init__(self, message):
        super().__init__(message, code='maintenance')


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def error_details(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error)); chain.append(error); error = error.__cause__
    # Jawna przyczyna adaptera ma pierwszeństwo przed ogólnym opakowaniem.
    for item in reversed(chain):
        code = None
        if isinstance(item, SourceContractError): code = item.code
        elif isinstance(item, SourceRequestBudgetError): code = 'client_retry_blocked'
        elif isinstance(item, (socket.gaierror, getattr(aiohttp, 'ClientConnectorDNSError', socket.gaierror))): code = 'dns'
        elif isinstance(item, (ssl.SSLError, aiohttp.ClientSSLError)): code = 'tls'
        elif isinstance(item, (TimeoutError, asyncio.TimeoutError)): code = 'timeout'
        elif isinstance(item, aiohttp.ClientConnectorError): code = 'connect_error'
        elif isinstance(item, json.JSONDecodeError): code = 'invalid_json'
        elif type(item).__name__ == 'SourceHttpError': code = 'http'
        elif type(item).__name__ == 'CatalogChangedError': code = 'catalog_changed'
        elif type(item).__name__.startswith('EngineStore'): code = 'storage'
        if code:
            retry_after = getattr(item, 'retry_after', None) if code == 'http' else None
            if isinstance(retry_after, datetime):
                retry_after = retry_after.isoformat()
            return {'error_code': code, 'message': LABELS[code],
                    'http_status': getattr(item, 'status', None) if code == 'http' else None,
                    'retry_after': retry_after}
    return {'error_code': 'internal', 'message': LABELS['internal'], 'http_status': None, 'retry_after': None}


@contextmanager
def measured(stage):
    run = CURRENT.get()
    start = time.monotonic()
    if run: run['stage'] = stage
    try:
        yield
    finally:
        if run:
            timings = run['timings_ms']
            timings[stage] = timings.get(stage, 0) + (time.monotonic() - start) * 1000


def parser(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with measured('parse'):
            return function(*args, **kwargs)
    return wrapped


def request_attempt(attempt, url):
    run = CURRENT.get()
    if run:
        run['request_count'] += 1
        run['retry_count'] += int(attempt > 1)
        # Prywatny zbiór służy tylko liczeniu zasobów; URL nie trafia do bazy.
        run['_resources'].add(str(url))


def source_trace(*, single_request=False):
    """Liczy wysłane nagłówki, również przy przekierowaniu i ponowieniu przez klienta."""
    trace = aiohttp.TraceConfig()
    trace.osd_counter = True
    async def headers_sent(_session, context, _params):
        run, metrics = CURRENT.get(), context.trace_request_ctx
        if not isinstance(metrics, dict): return
        if single_request and metrics['headers']:
            raise SourceRequestBudgetError(LABELS['client_retry_blocked'])
        if run is not None and metrics['headers']:
            run['request_count'] += 1
            if not metrics['redirect_pending']: run['retry_count'] += 1
        metrics['headers'] += 1
        metrics['traced'] = True
        metrics['redirect_pending'] = False
    async def redirect(_session, context, _params):
        run, metrics = CURRENT.get(), context.trace_request_ctx
        if run is None or not isinstance(metrics, dict): return
        metrics['redirect_pending'] = True
        completed('http_responses')
    trace.on_request_headers_sent.append(headers_sent)
    trace.on_request_redirect.append(redirect)
    return trace


def request_metrics(session):
    return {'headers': 0, 'traced': False, 'redirect_pending': False} if any(
        getattr(trace, 'osd_counter', False) for trace in getattr(session, 'trace_configs', ())
    ) else None


def response_redirects(response, metrics=None):
    run = CURRENT.get()
    if run:
        for item in (*getattr(response, 'history', ()), response):
            if getattr(item, 'url', None): run['_resources'].add(str(item.url))
        if metrics and metrics['traced']: return
        # Zewnętrzna sesja może ponawiać połączenie wewnętrznie bez historii.
        run['request_count_complete'] = False
        count = len(getattr(response, 'history', ()) or ())
        run['request_count'] += count
        run['completed_units']['http_responses'] = run['completed_units'].get('http_responses', 0) + count


def uncertain_request_count():
    run = CURRENT.get()
    if run:
        # Po przerwaniu transportu klient może nie udostępnić historii redirectów.
        # Zachowujemy jawne minimum, zamiast udawać dokładny licznik.
        run['request_count_complete'] = False


def completed(unit):
    run = CURRENT.get()
    if run:
        run['completed_units'][unit] = run['completed_units'].get(unit, 0) + 1


def warn(code):
    run = CURRENT.get()
    if run is not None and code not in run['warnings']:
        run['warnings'].append(code)


def response_metadata(status, content_type, body):
    """Wyłącznie metadane odczytanego ciała po dekompresji HTTP; bez nagłówków."""
    run = CURRENT.get()
    if run is None:
        return
    rows = run.setdefault('responses', [])
    if len(rows) >= 20:
        warn('response_metadata_limit')
        return
    media = str(content_type or '').split(';', 1)[0].strip().lower()
    media = media if re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+', media) else None
    rows.append({'observed_at': utc(), 'http_status': int(status), 'content_type': media,
                 'body_complete': body is not None, 'body_bytes': len(body) if body is not None else None,
                 'body_sha256': hashlib.sha256(body).hexdigest() if body is not None else None,
                 'hash_scope': 'decoded_body_bytes' if body is not None else None})


def publication_metadata(document):
    """Metadane należą do konkretnego skanu; do stanu trafiają dopiero po sukcesie."""
    run = CURRENT.get()
    if run is None:
        return
    raw = document.get('lastUpdate')
    value, status = None, 'missing'
    if raw not in (None, ''):
        status = 'invalid'
        try:
            stamp = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if stamp.tzinfo is not None:
                value = stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                status = 'available'
        except (ValueError, TypeError, OverflowError):
            pass
    run['_publication_metadata'] = {
        'publication_updated_at': value, 'publication_timestamp_field': 'document.lastUpdate' if value else None,
        'publication_observed_at': utc(), 'publication_time_status': status}


class Diagnostics:
    def __init__(self, path, *, retention_days=30, max_runs=10000):
        self.path = Path(path)
        self.retention_days, self.max_runs = retention_days, max_runs
        self._write_error = None
        self._read_error = None

    @property
    def error(self):
        return self._write_error or self._read_error

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
        except Exception:
            connection.close()
            raise
        return connection

    def _start(self):
        with closing(self._connect()) as c, c:
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1): raise ValueError('Nieobsługiwany schemat diagnostyki')
            c.execute('CREATE TABLE IF NOT EXISTS scan_runs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, scan_id TEXT UNIQUE NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL, payload TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS scan_source ON scan_runs(source, sequence)')
            c.execute('PRAGMA user_version=1')
            for row in c.execute("SELECT * FROM scan_runs WHERE status='running'").fetchall():
                data = json.loads(row['payload'])
                data.update(status='interrupted', finished_at=utc(), error_code='interrupted', message=LABELS['interrupted'])
                c.execute('UPDATE scan_runs SET status=?, payload=? WHERE sequence=?',
                          ('interrupted', json.dumps(data, ensure_ascii=False), row['sequence']))

    async def start(self):
        await self.safe(self._start)

    async def safe(self, function, *args, read=False):
        channel = '_read_error' if read else '_write_error'
        try:
            result = await asyncio.to_thread(function, *args)
            setattr(self, channel, None)
            return result
        except Exception as error:
            # Nie publikujemy dowolnego tekstu wyjątku, ścieżek ani adresów.
            setattr(self, channel, type(error).__name__)
            return None

    def _save(self, data):
        data = {key: value for key, value in data.items() if not key.startswith('_')}
        with closing(self._connect()) as c, c:
            if data['status'] == 'success':
                old = c.execute("SELECT payload FROM scan_runs WHERE source=? AND scope=? AND status='success' AND scan_id<>? ORDER BY sequence DESC LIMIT 1",
                                (data['source'], data['scope'], data['scan_id'])).fetchone()
                previous = json.loads(old[0]).get('source_event_count') if old else None
                if previous is not None and previous >= 20 and data['source_event_count'] <= previous * .2:
                    data['warnings'] = sorted(set(data['warnings']) | {'record_count_drop'})
            c.execute('INSERT INTO scan_runs(scan_id,source,started_at,status,scope,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(scan_id) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                      (data['scan_id'], data['source'], data['started_at'], data['status'], data['scope'], json.dumps(data, ensure_ascii=False)))
            c.execute("DELETE FROM scan_runs WHERE status<>'running' AND (started_at < ? OR sequence NOT IN (SELECT sequence FROM scan_runs ORDER BY sequence DESC LIMIT ?))",
                      (datetime.fromtimestamp(time.time() - self.retention_days * 86400, timezone.utc).isoformat(), self.max_runs))

    def _read(self, source=None, status=None, before=None, limit=50, start=None, stop=None):
        where, values = [], []
        for name, value in [('source', source), ('status', status)]:
            if value:
                where.append(name+'=?'); values.append(value)
        if before is not None: where.append('sequence<?'); values.append(before)
        if start is not None: where.append('started_at>=?'); values.append(start)
        if stop is not None: where.append('started_at<=?'); values.append(stop)
        clause = ' WHERE '+' AND '.join(where) if where else ''
        with closing(self._connect()) as c:
            rows = c.execute('SELECT sequence,payload FROM scan_runs'+clause+' ORDER BY sequence DESC LIMIT ?', values+[limit+1]).fetchall()
        items = [dict(json.loads(row['payload']), sequence=row['sequence']) for row in rows[:limit]]
        return {'items': items, 'count': len(items), 'next_before': items[-1]['sequence'] if len(rows)>limit else None,
                'retention_days': self.retention_days, 'max_runs': self.max_runs}

    async def read(self, **kwargs):
        return await self.safe(self._read, kwargs.get('source'), kwargs.get('status'), kwargs.get('before'), kwargs.get('limit', 50), kwargs.get('start'), kwargs.get('stop'), read=True)

    def capture(self, source, scope):
        return Capture(self, source, scope)


class Capture:
    def __init__(self, store, source, scope):
        self.store = store
        self.data = {'scan_id': str(uuid.uuid4()), 'source': source, 'started_at': utc(), 'finished_at': None,
                     'status': 'running', 'stage': 'persist', 'scope': scope, 'parser_version': PARSER_VERSION,
                     'timings_ms': {}, 'duration_ms': None, 'request_count': 0, 'request_count_complete': True, 'retry_count': 0, 'resource_count': 0,
                     'completeness': 'unknown', 'source_event_count': None, 'matched_event_count': None,
                     'confirmed_count': None, 'candidate_count': None, 'state_revision': None,
                     'error_code': None, 'message': None, 'http_status': None, 'retry_after': None,
                     'warnings': [], 'completed_units': {}, 'timing_scope': 'instrumented_operations', '_resources': set()}

    async def __aenter__(self):
        self.started = time.monotonic()
        self.token = CURRENT.set(self.data)
        await self.store.safe(self.store._save, self.data)
        return self

    def failed(self, error):
        self.data.update(status='failed', **error_details(error))
        if self.data['error_code'] == 'internal' and self.data['stage'] == 'parse' and isinstance(error, (ValueError, TypeError, KeyError)):
            self.data.update(error_code='contract', message=LABELS['contract'])
        stage = {'dns':'fetch', 'tls':'fetch', 'connect_error':'fetch', 'timeout':'fetch',
                 'http':'fetch', 'invalid_json':'parse', 'contract':'parse', 'incomplete':'parse',
                 'conflicting_id':'parse', 'catalog_changed':'match', 'storage':'persist'}
        stage['client_retry_blocked'] = 'fetch'
        self.data['stage'] = stage.get(self.data['error_code'], self.data['stage'])
        if self.data['error_code'] in {'contract','incomplete','conflicting_id','invalid_json'}:
            self.data['completeness'] = 'incomplete'

    async def __aexit__(self, kind, error, traceback):
        try:
            if error:
                if isinstance(error, asyncio.CancelledError):
                    self.data.update(status='interrupted', error_code='interrupted', message=LABELS['interrupted'])
                else: self.failed(error)
            if self.data['status'] == 'running':
                self.data.update(status='interrupted', error_code='interrupted', message=LABELS['interrupted'])
            self.data['resource_count'] = len(self.data['_resources'])
            self.data['finished_at'] = utc()
            self.data['duration_ms'] = round((time.monotonic()-self.started)*1000, 3)
            self.data['timings_ms'] = {k:round(v,3) for k,v in self.data['timings_ms'].items()}
            await self.store.safe(self.store._save, self.data)
        finally:
            CURRENT.reset(self.token)


def copy_database(source, destination):
    """Spójna kopia lub odtworzenie w nowej lokalizacji; bez nadpisywania plików."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError('Cel kopii musi być nowym plikiem')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
        if src.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('Nieobsługiwany schemat diagnostyki')
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Uszkodzona kopia diagnostyki')
    return destination


if __name__ == '__main__':
    import argparse
    parser_ = argparse.ArgumentParser(description='Kopia/odtworzenie diagnostyki do nowego pliku')
    parser_.add_argument('source',type=Path)
    parser_.add_argument('destination',type=Path)
    args = parser_.parse_args()
    print(copy_database(args.source,args.destination))
