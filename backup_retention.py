"""Plan retencji lokalnej. Nie usuwa danych i nie zmienia repozytorium restic."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from service_backup import digest, no_symlinks, verify_snapshot


def snapshot_name(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def restic_id(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def repository_key():
    repository = os.environ.get('RESTIC_REPOSITORY')
    if not repository:
        raise ValueError('Nie wskazano repozytorium kopii.')
    return hashlib.sha256(repository.encode('utf8')).hexdigest()


def read_metadata(path):
    path = no_symlinks(path)
    if path.stat().st_size > 256 * 1024:
        raise ValueError('Metadane kopii przekraczają limit.')
    value = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ValueError('Niepoprawne metadane kopii.')
    return value


def receipt_path(snapshot):
    snapshot = no_symlinks(snapshot)
    if not snapshot_name(snapshot.name):
        raise ValueError('Retencja wymaga nazwy migawki w postaci UUID.')
    return snapshot.parent / '.backup-receipts' / (snapshot.name + '.json')


def upload_receipt(snapshot, snapshot_id):
    snapshot = no_symlinks(snapshot).resolve()
    if not snapshot_name(snapshot.name) or not restic_id(snapshot_id):
        raise ValueError('Niepoprawny identyfikator potwierdzenia kopii.')
    return dict(version=1, local_snapshot_name=snapshot.name, source_path=str(snapshot),
                manifest_sha256=digest(snapshot / 'manifest.json'),
                repository_key=repository_key(), snapshot_id=snapshot_id)


def checked_local_path(staging, name):
    """Sprawdza granicę katalogu również tuż przed usuwaniem na Windows."""
    if not snapshot_name(name):
        raise ValueError('Niepoprawna nazwa lokalnej migawki.')
    staging = no_symlinks(staging).resolve()
    candidate = no_symlinks(staging / name).resolve()
    if candidate.parent != staging or not candidate.is_dir():
        raise ValueError('Migawka nie jest bezpośrednim katalogiem miejsca kopii.')
    return candidate


def retention_plan(staging, current_name, keep, remote_snapshots):
    """Chroni bieżącą i co najmniej dwie najnowsze potwierdzone kopie lokalne."""
    if isinstance(keep, bool) or not isinstance(keep, int) or not 2 <= keep <= 48:
        raise ValueError('Liczba zachowanych kopii musi wynosić od 2 do 48.')
    if not isinstance(remote_snapshots, list):
        raise ValueError('Niepoprawna lista migawek repozytorium.')
    remote = {}
    for row in remote_snapshots:
        if (not isinstance(row, dict) or not restic_id(row.get('id'))
                or not isinstance(row.get('paths'), list)
                or not all(isinstance(path, str) for path in row['paths'])
                or not isinstance(row.get('tags'), list)):
            raise ValueError('Niepoprawne metadane migawki repozytorium.')
        remote[row['id']] = row
    staging = no_symlinks(staging).resolve()
    checked_local_path(staging, current_name)
    repository = repository_key()
    confirmed, preserved = [], []
    now = datetime.now(timezone.utc)
    for entry in staging.iterdir():
        if not snapshot_name(entry.name):
            continue
        try:
            path = checked_local_path(staging, entry.name)
            receipt = read_metadata(receipt_path(path))
            if (receipt.get('version') != 1 or receipt.get('local_snapshot_name') != entry.name
                    or receipt.get('source_path') != str(path)
                    or receipt.get('repository_key') != repository
                    or receipt.get('manifest_sha256') != digest(path / 'manifest.json')):
                raise ValueError('Niezgodne potwierdzenie wysyłki.')
            remote_row = remote.get(receipt.get('snapshot_id'))
            if (remote_row is None or 'monitor-osd' not in remote_row['tags']
                    or str(path) not in remote_row['paths']):
                raise ValueError('Brak migawki w wybranym repozytorium.')
            manifest = verify_snapshot(path)
            started = datetime.fromisoformat(manifest['started_at'])
            if started.tzinfo is None or (started - now).total_seconds() > 300:
                raise ValueError('Niepoprawny czas migawki.')
            confirmed.append((started, entry.name))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            preserved.append(entry.name)
    names = {name for _, name in confirmed}
    if current_name not in names:
        raise ValueError('Bieżąca kopia nie ma poprawnego potwierdzenia w repozytorium.')
    newest = [name for _, name in sorted(confirmed, reverse=True)]
    protected = set(newest[:keep]) | {current_name}
    return dict(keep=keep, protected=sorted(protected), preserved=sorted(preserved),
                delete=[name for name in newest if name not in protected])
