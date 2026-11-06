"""Transakcyjny magazyn bieżącego stanu silnika."""

import asyncio
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from event_history import snapshot_event, _utc, parse_event_datetime


SCHEMA_VERSION = 3

import notification_queue as queue


class EngineStoreError(RuntimeError):
    """Błąd trwałego magazynu silnika."""


class EngineStoreCommittedError(EngineStoreError):
    """Błąd testowy zgłoszony po skutecznym COMMIT."""

    def __init__(self, revision):
        super().__init__(f"Stan został zatwierdzony w rewizji {revision}")
        self.revision = revision


@dataclass(frozen=True)
class StoreWriteResult:
    revision: int
    export_error: str | None = None


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode_state(state):
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        raise EngineStoreError("Stan silnika ma niepoprawną strukturę")
    try:
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise EngineStoreError(f"Stanu silnika nie można zapisać jako JSON: {error}") from error


def _decode_state(value):
    try:
        state = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise EngineStoreError(f"Stan silnika w SQLite jest uszkodzony: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        raise EngineStoreError("Stan silnika w SQLite ma niepoprawną strukturę")
    return state


def _atomic_json_export(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = file.name
            json.dump(state, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _connect(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _create_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS engine_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            saved_at TEXT,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is not None and row[0] not in {'1', '2', '3'}:
        raise EngineStoreError(
            f"Nieobsługiwana wersja bazy silnika: {row[0]} (oczekiwano {SCHEMA_VERSION})"
        )
    connection.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        ('1',),
    )


DEPARTURES = ('not_published', 'out_of_scope')


def history_time(value):
    parsed = parse_event_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z') if parsed else None


def history_record(action, event, previous, at, origin='scan'):
    return {'change_id': str(uuid.uuid4()), 'origin': origin, 'recorded_at': history_time(at),
            'action': action, 'before': snapshot_event(previous, at) if previous else None,
            'after': snapshot_event(event, at), 'legacy': None}


def _insert_changes(connection, records, revision):
    last_id = None
    for index, record in enumerate(records):
        after = record['after']
        if not isinstance(after, dict) or not isinstance(record['action'], str):
            raise EngineStoreError('Niepoprawny wpis historii')
        if record['origin'] != 'legacy' and record['action'] not in {*DEPARTURES, 'new', 'updated', 'cancelled', 'snapshot'}:
            raise EngineStoreError('Nieznany rodzaj zmiany historii')
        encoded = [json.dumps(record.get(key), ensure_ascii=False, allow_nan=False) if record.get(key) is not None else None
                   for key in ('before', 'after', 'legacy')]
        last_id = record['change_id']
        connection.execute("""INSERT INTO event_changes
            (change_id,origin,state_revision,batch_index,recorded_at,source,object_id,event_id,action,before_json,after_json,legacy_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (last_id, record['origin'], revision, index, record['recorded_at'],
             after.get('source') or 'unknown', str(after.get('object_id') or ''),
             str(after.get('event_id') or ''), record['action'], *encoded))
    if records:
        connection.execute("UPDATE schema_meta SET value=CAST(value AS INTEGER)+1 WHERE key='archive_revision'")
        connection.execute("UPDATE schema_meta SET value=? WHERE key='archive_last_change_id'", (last_id,))


def _migrate_history(connection, state, revision, at, failpoint=None):
    connection.execute("""CREATE TABLE event_changes (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT NOT NULL UNIQUE,
        origin TEXT NOT NULL CHECK(origin IN ('legacy','baseline','scan')),
        state_revision INTEGER NOT NULL, batch_index INTEGER NOT NULL, recorded_at TEXT,
        source TEXT NOT NULL, object_id TEXT NOT NULL, event_id TEXT NOT NULL, action TEXT NOT NULL,
        before_json TEXT, after_json TEXT, legacy_json TEXT,
        UNIQUE(origin,state_revision,batch_index))""")
    for name, columns in [('source_sequence','source,sequence'), ('object_sequence','object_id,sequence'),
                          ('recorded_at','recorded_at')]:
        connection.execute(f'CREATE INDEX event_changes_{name} ON event_changes({columns})')
    legacy = state.get('history', [])
    if not isinstance(legacy, list):
        raise EngineStoreError('Historia źródłowa nie jest listą')
    records = []
    for index, item in enumerate(legacy):
        if not isinstance(item, dict) or not isinstance(item.get('event'), dict) or not isinstance(item.get('action'), str):
            raise EngineStoreError(f'Niepoprawny wpis starej historii: {index}')
        record = history_record(item['action'], item['event'], None, item.get('at'), 'legacy')
        record['legacy'] = item
        records.append(record)
    connection.executemany("INSERT OR REPLACE INTO schema_meta(key,value) VALUES(?,?)", [
        ('archive_instance_id', str(uuid.uuid4())), ('archive_started_at', _utc(at)),
        ('archive_revision','0'), ('archive_last_change_id',''),
        ('legacy_import_count', str(len(legacy))),
        ('legacy_unknown_time_count', str(sum(r['recorded_at'] is None for r in records))),
        ('history_complete','false'), ('history_retention_days','null')])
    _insert_changes(connection, records, revision)
    baseline = [history_record('snapshot', dict(event, event_key=key), None, at, 'baseline')
                for key, event in sorted(state['events'].items())]
    _insert_changes(connection, baseline, revision)
    connection.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
    if failpoint == 'before_commit':
        raise EngineStoreError('Testowe przerwanie migracji przed COMMIT')


class EngineStore:
    """Jeden serializowany wykonawca zapisów do SQLite."""

    def __init__(self, database_path, compatibility_path):
        self.database_path = Path(database_path)
        self.compatibility_path = Path(compatibility_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine-store")
        self._closed = False
        self.export_error = None

    def load_or_migrate(self, legacy_state, *, failpoint=None):
        with closing(_connect(self.database_path)) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                _create_schema(connection)
                row = connection.execute(
                    "SELECT revision, state_json FROM engine_state WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    if connection.execute("SELECT 1 FROM schema_meta WHERE key='migrated_at'").fetchone():
                        raise EngineStoreError("Brak zatwierdzonego stanu w istniejącej bazie; nie importuję starego JSON")
                    legacy_state = legacy_state() if callable(legacy_state) else legacy_state
                    encoded = _encode_state(legacy_state)
                    legacy_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                    migrated_at = _utc_now()
                    connection.execute(
                        """
                        INSERT INTO engine_state(singleton, revision, saved_at, state_json, updated_at)
                        VALUES(1, 1, ?, ?, ?)
                        """,
                        (legacy_state.get("saved_at"), encoded, migrated_at),
                    )
                    connection.executemany(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                        [
                            ("migrated_at", migrated_at),
                            ("legacy_state_sha256", legacy_hash),
                            ("history_complete", "false"),
                        ],
                    )
                    state = deepcopy(legacy_state)
                    revision = 1
                else:
                    revision = int(row[0])
                    state = _decode_state(row[1])
                version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                if version == '1':
                    _migrate_history(connection, state, revision, _utc_now(), failpoint)
                version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                if version == '2':
                    queue.create_schema(connection)
                    if failpoint == 'before_commit':
                        raise EngineStoreError('Testowe przerwanie migracji kolejki')
                _validate_database(connection)
                connection.execute("UPDATE schema_meta SET value='2' WHERE key='queue_payload_version'")
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass
        self.export_error = None
        try:
            _atomic_json_export(self.compatibility_path, state)
        except Exception as error:
            self.export_error = f"{type(error).__name__}: {error}"
        return state, revision

    def load_current(self):
        with closing(_read_connection(self.database_path)) as connection:
            _meta, row = _validate_database(connection)
        return _decode_state(row[3]), int(row[0])

    def _write_state(self, state, failpoint=None, history_records=(), notification_jobs=()):
        encoded = _encode_state(state)
        connection = _connect(self.database_path)
        revision = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] not in {'2', '3'}:
                raise EngineStoreError('Zapis wymaga schematu 2')
            row = connection.execute(
                "SELECT revision FROM engine_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise EngineStoreError("Baza silnika nie zawiera bieżącego stanu")
            revision = int(row[0]) + 1
            connection.execute(
                """
                UPDATE engine_state
                SET revision = ?, saved_at = ?, state_json = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (revision, state.get("saved_at"), encoded, _utc_now()),
            )
            _insert_changes(connection, history_records, revision)
            if notification_jobs:
                queue.insert_jobs(connection, notification_jobs)
            if failpoint == "before_commit":
                raise EngineStoreError("Testowe przerwanie przed COMMIT")
            connection.commit()
            if failpoint == "after_commit":
                raise EngineStoreCommittedError(revision)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

        export_error = None
        try:
            _atomic_json_export(self.compatibility_path, state)
        except Exception as error:
            export_error = f"{type(error).__name__}: {error}"
        return StoreWriteResult(revision=revision, export_error=export_error)

    async def write_state(self, state, *, failpoint=None, history_records=(), notification_jobs=()):
        if self._closed:
            raise EngineStoreError("Magazyn silnika jest zamknięty")
        snapshot = deepcopy(state)
        loop = asyncio.get_running_loop()
        try:
            args = (snapshot, failpoint, deepcopy(history_records), deepcopy(notification_jobs)) if notification_jobs else (snapshot, failpoint, deepcopy(history_records)) if history_records else (snapshot, failpoint)
            future = loop.run_in_executor(self._executor, self._write_state, *args)
            cancelled = False
            try:
                while True:
                    try:
                        return await asyncio.shield(future)
                    except asyncio.CancelledError:
                        # Wątek może już wykonywać COMMIT. Najpierw pozwól
                        # wywołującemu opublikować wynik, potem anuluj zadanie.
                        cancelled = True
            finally:
                if cancelled:
                    asyncio.current_task().cancel()
        except EngineStoreError:
            raise
        except Exception as error:
            raise EngineStoreError(
                f"Nie udało się zapisać stanu silnika: {type(error).__name__}: {error}"
            ) from error

    def _backup(self, destination):
        destination = Path(destination)
        _reject_same_database(self.database_path, destination)
        inspect_database(self.database_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        _reject_same_database(self.database_path, temporary)
        if temporary.exists():
            temporary.unlink()
        source = _read_connection(self.database_path)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.commit()
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise EngineStoreError(f"Kopia bazy nie przeszła kontroli: {result}")
            _validate_database(target, full=True)
        finally:
            target.close()
            source.close()
        os.replace(temporary, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        return destination

    async def backup(self, destination):
        if self._closed:
            raise EngineStoreError("Magazyn silnika jest zamknięty")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._backup, Path(destination))

    def export_current(self, destination=None):
        _reject_same_database(self.database_path, Path(destination or self.compatibility_path))
        state, revision = self.load_current()
        _atomic_json_export(Path(destination or self.compatibility_path), state)
        return revision

    def read_history(self, *, limit=50, offset=0, source=None, object_id=None, start=None, stop=None,
                     departures_only=False, include_state=False, publication_filter=None, latest_per_event=False):
        with closing(_read_connection(self.database_path)) as connection:
            connection.execute('BEGIN')
            meta, row = _validate_database(connection)
            if meta['schema_version'] not in {'2', '3'}:
                raise EngineStoreError('Historia wymaga schematu 2')
            conditions, values = [], []
            for column, value in [('source', source), ('object_id', object_id)]:
                if value is not None:
                    conditions.append(column + '=?'); values.append(value)
            if start is not None:
                conditions.extend(['recorded_at>=?', 'recorded_at<?']); values.extend([start, stop])
            if publication_filter is not None:
                connection.create_function("publication_allowed",5,publication_filter)
                conditions.append("publication_allowed(source,object_id,before_json,after_json,legacy_json)=1")
            if departures_only:
                conditions.append("action IN ('not_published','out_of_scope')")
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            selection = 'SELECT * FROM event_changes' + where
            if latest_per_event:
                # Filter public visibility before grouping. Old rows without an event ID
                # remain independent rather than being merged into one unknown event.
                selection = ('WITH visible AS (' + selection + ') SELECT * FROM visible WHERE sequence IN ('
                             'SELECT MAX(sequence) FROM visible GROUP BY source, object_id, '
                             "COALESCE(NULLIF(json_extract(after_json, '$.event_key'), ''), "
                             "NULLIF(json_extract(after_json, '$.source_event_id'), ''), "
                             "NULLIF(event_id, ''), change_id))")
            total = connection.execute('SELECT COUNT(*) FROM (' + selection + ')', values).fetchone()[0]
            publication_where = ' WHERE publication_allowed(source,object_id,before_json,after_json,legacy_json)=1' if publication_filter is not None else ''
            archive_total, available_from, unknown = connection.execute(
                'SELECT COUNT(*),MIN(recorded_at),SUM(recorded_at IS NULL) FROM event_changes'+publication_where).fetchone()
            cursor = connection.execute('SELECT * FROM (' + selection + ')' +
                                        ' ORDER BY sequence DESC LIMIT ? OFFSET ?', [*values, limit, offset])
            columns = [item[0] for item in cursor.description]
            items = [_history_item(dict(zip(columns, entry))) for entry in cursor]
            result = {'items': items, 'total': total, 'archive_total': archive_total,
                      'available_from': available_from, 'unknown_time_count': unknown or 0, 'meta': meta}
            if include_state:
                result['state'], result['state_revision'] = _decode_state(row[3]), row[0]
            return result

    def _queue_operation(self, operation, *args):
        allowed = {'claim': queue.claim, 'finish': queue.finish, 'summary': queue.summary, 'decide': queue.decide,
                   'history':queue.delivery_history}
        read_only = operation in {'summary','history'}
        with closing(_read_connection(self.database_path) if read_only else _connect(self.database_path)) as connection:
            with connection:
                connection.execute('BEGIN' if read_only else 'BEGIN IMMEDIATE')
                return allowed[operation](connection, *args)

    async def queue_operation(self, operation, *args):
        if self._closed:
            raise EngineStoreError('Magazyn silnika jest zamknięty')
        future = asyncio.get_running_loop().run_in_executor(self._executor, self._queue_operation, operation, *args)
        cancelled = False
        try:
            while True:
                try:
                    return await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
        finally:
            if cancelled:
                asyncio.current_task().cancel()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)


def _read_connection(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _reject_same_database(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    protected = {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}
    if destination in protected or (destination.exists() and os.path.samefile(source, destination)):
        raise EngineStoreError("Kopia lub eksport nie może nadpisać swojej bazy źródłowej")


def _validate_database(connection, *, full=False):
    meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
    if meta.get("schema_version") not in {'1', '2', '3'}:
        raise EngineStoreError("Nieobsługiwana wersja schematu kopii silnika")
    rows = connection.execute(
        "SELECT singleton, revision, saved_at, updated_at, state_json FROM engine_state"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], int) or rows[0][1] < 1:
        raise EngineStoreError("Baza silnika nie zawiera poprawnego bieżącego stanu")
    _decode_state(rows[0][4])
    if meta['schema_version'] in {'2', '3'}:
        required = {'archive_instance_id', 'archive_started_at', 'archive_revision', 'archive_last_change_id',
                    'legacy_import_count', 'legacy_unknown_time_count', 'history_complete', 'history_retention_days'}
        if not required <= meta.keys():
            raise EngineStoreError('Niepoprawne metadane archiwum')
        try:
            uuid.UUID(meta['archive_instance_id'])
            if int(meta['archive_revision']) < 0 or int(meta['legacy_import_count']) < 0 or int(meta['legacy_unknown_time_count']) < 0:
                raise ValueError('Ujemny licznik')
        except ValueError as error:
            raise EngineStoreError('Niepoprawne metadane archiwum') from error
        if _utc(meta['archive_started_at']) != meta['archive_started_at']:
            raise EngineStoreError('Niepoprawny początek archiwum')
        latest = connection.execute('SELECT change_id FROM event_changes ORDER BY sequence DESC LIMIT 1').fetchone()
        if meta['archive_last_change_id'] != (latest[0] if latest else ''):
            raise EngineStoreError('Metadane archiwum nie zgadzają się z ostatnim wpisem')
        if full:
            cursor = connection.execute('SELECT * FROM event_changes ORDER BY sequence')
            columns = [item[0] for item in cursor.description]
            for entry in cursor:
                _history_item(dict(zip(columns, entry)))
            counts = connection.execute("SELECT COUNT(*),SUM(recorded_at IS NULL) FROM event_changes WHERE origin='legacy'").fetchone()
            if counts != (int(meta['legacy_import_count']), int(meta['legacy_unknown_time_count']) if counts[0] else None):
                raise EngineStoreError('Liczba wpisów importu nie zgadza się z metadanymi')
    if meta['schema_version'] == '3':
        try:
            queue.validate_queue(connection, full)
        except (ValueError, TypeError, KeyError, sqlite3.Error) as error:
            raise EngineStoreError(f'Uszkodzona kolejka powiadomień: {type(error).__name__}') from error
    return meta, rows[0][1:]


def _history_item(row):
    try:
        uuid.UUID(row['change_id'])
        before = json.loads(row['before_json']) if row['before_json'] is not None else None
        after = json.loads(row['after_json'])
        if not isinstance(after, dict) or (before is not None and not isinstance(before, dict)):
            raise ValueError('Migawka nie jest obiektem')
        json.dumps([before, after], allow_nan=False)
        if row['state_revision'] < 1 or row['batch_index'] < 0 or row['origin'] not in {'legacy','baseline','scan'}:
            raise ValueError('Niepoprawna tożsamość wpisu')
        if (row['source'], row['object_id'], row['event_id']) != (after.get('source') or 'unknown', str(after.get('object_id') or ''), str(after.get('event_id') or '')):
            raise ValueError('Indeks wpisu nie zgadza się z migawką')
        if row['recorded_at'] is not None and history_time(row['recorded_at']) != row['recorded_at']:
            raise ValueError('Niepoprawny czas wpisu')
        if row['legacy_json'] is not None:
            legacy = json.loads(row['legacy_json'])
            if not isinstance(legacy, dict) or not isinstance(legacy.get('event'), dict):
                raise ValueError('Niepoprawny oryginał wpisu')
        item = dict(after)
        item.update(history_id=row['change_id'], action=row['action'], recorded_at=row['recorded_at'],
                    origin=row['origin'], state_revision=row['state_revision'], before=before, after=after)
        return item
    except (ValueError, TypeError, KeyError) as error:
        raise EngineStoreError(f'Uszkodzony wpis archiwum: {error}') from error


def inspect_database(path):
    connection = _read_connection(path)
    try:
        connection.execute('BEGIN')
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise EngineStoreError(f"Baza nie przeszła kontroli integralności: {integrity}")
        meta, row = _validate_database(connection, full=True)
        archive_count = connection.execute('SELECT COUNT(*) FROM event_changes').fetchone()[0] if meta['schema_version']in {'2', '3'} else 0
    finally:
        connection.close()
    if row is None:
        raise EngineStoreError("Baza silnika nie zawiera bieżącego stanu")
    state = _decode_state(row[3])
    return {
        "integrity": integrity,
        "schema": meta,
        "revision": int(row[0]),
        "saved_at": row[1],
        "updated_at": row[2],
        "event_count": len(state["events"]),
        "history_count": len(state.get("history", [])),
        "history_preview_count": len(state.get("history", [])),
        "history_archive_count": archive_count,
        "source_count": len(state.get("sources", {})),
        "database_bytes": Path(path).stat().st_size,
        "wal_bytes": Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,
    }


def backup_database(source, destination):
    inspect_database(source)
    store = EngineStore(source, Path(destination).with_suffix(".state.json"))
    try:
        return store._backup(Path(destination))
    finally:
        store._executor.shutdown(wait=True, cancel_futures=True)
        store._closed = True


def restore_database(backup, destination):
    backup = Path(backup)
    destination = Path(destination)
    _reject_same_database(backup, destination)
    inspect_database(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".restore.tmp")
    _reject_same_database(backup, temporary)
    if temporary.exists():
        temporary.unlink()
    source = _read_connection(backup)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        target.commit()
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise EngineStoreError(f"Odtworzona baza nie przeszła kontroli: {result}")
        _validate_database(target, full=True)
    finally:
        target.close()
        source.close()
    for suffix in ("-wal", "-shm"):
        # Przy zatrzymanej usłudze zachowaj stare COMMIT także wtedy,
        # gdy późniejsza podmiana pliku zakończy się błędem.
        if suffix == "-wal" and destination.exists():
            with closing(sqlite3.connect(destination)) as connection:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0:
                    raise EngineStoreError("Baza docelowa jest zajęta; zatrzymaj aplikację przed odtworzeniem")
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    os.replace(temporary, destination)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return destination


def main():
    parser = argparse.ArgumentParser(description="Obsługa magazynu stanu silnika")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Sprawdź bazę i pokaż jej metadane")
    inspect.add_argument("database", type=Path)
    backup = commands.add_parser("backup", help="Wykonaj bezpieczną kopię SQLite")
    backup.add_argument("database", type=Path)
    backup.add_argument("destination", type=Path)
    export = commands.add_parser("export", help="Wyeksportuj bieżący stan do JSON")
    export.add_argument("database", type=Path)
    export.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Odtwórz bazę z kopii przy zatrzymanej usłudze")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    migrate = commands.add_parser('migrate', help='Migruj istniejącą bazę przy zatrzymanej usłudze')
    migrate.add_argument('database', type=Path)
    migrate.add_argument('destination', type=Path, help='Eksport zgodności state.json')
    history_export = commands.add_parser('history-export', help='Pełna historia w JSONL ze spójnej kopii')
    history_export.add_argument('database', type=Path)
    history_export.add_argument('destination', type=Path)
    args = parser.parse_args()

    if args.command == "inspect":
        result = inspect_database(args.database)
    elif args.command == "backup":
        result = {"backup": str(backup_database(args.database, args.destination))}
    elif args.command == "restore":
        result = {"database": str(restore_database(args.backup, args.destination))}
    elif args.command == 'history-export':
        result = {'history_export': str(export_history(args.database, args.destination))}
    else:
        store = EngineStore(args.database, args.destination)
        try:
            if args.command == 'migrate':
                inspect_database(args.database)
                _reject_same_database(args.database, args.destination)
                _state, revision = store.load_or_migrate(None)
            else:
                revision = store.export_current(args.destination)
        finally:
            store._executor.shutdown(wait=True, cancel_futures=True)
            store._closed = True
        result = {"export": str(args.destination), "revision": revision}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def export_history(source, destination):
    destination = Path(destination)
    _reject_same_database(source, destination)
    temporary = None
    try:
        with closing(_read_connection(source)) as connection:
            connection.execute('BEGIN')
            meta, _row = _validate_database(connection)
            if meta['schema_version'] not in {'2', '3'}:
                raise EngineStoreError('Pełny eksport historii wymaga schematu 2')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                             prefix=destination.name+'.', suffix='.tmp', delete=False) as output:
                temporary = output.name
                cursor = connection.execute('SELECT * FROM event_changes ORDER BY sequence')
                columns = [item[0] for item in cursor.description]
                for row in cursor:
                    output.write(json.dumps(_history_item(dict(zip(columns, row))), ensure_ascii=False, allow_nan=False)+'\n')
                output.flush(); os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        return destination
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
