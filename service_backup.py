"""SpĂłjne migawki aplikacji. WywoĹ‚ujÄ…cy musi blokowaÄ‡ zmiany katalogu i stanu."""
import hashlib
import json
import re
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
from datetime import datetime, timezone
from contextlib import closing
from engine_store import inspect_database

DATABASES = ('engine.sqlite3', 'catalog-journal.sqlite3',
             'diagnostics.sqlite3', 'teryt.db', 'geocoding-cache.sqlite3')
ALLOWED = {'config.json', 'objects.json',
           'data/geoportal-teryt-context.json', *('data/'+name for name in DATABASES)}
REQUIRED = {'config.json', 'objects.json', *('data/'+name for name in DATABASES)}
PRIVATE_FOLDERS = ('data/location-reviews', 'data/location-evidence')
POLICY_FILE = 'data/publication-policy.sqlite3'
MAX_FILES = 10010
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def allowed_file(name, *, version=3):
    return isinstance(name,str) and (name in ALLOWED or (version >= 3 and name == POLICY_FILE) or (version >= 2 and
        re.fullmatch(r'data/location-(?:reviews|evidence)/[0-9a-f]{64}\.json',name) is not None))


def private_files(root):
    """Tylko zarzÄ…dzane katalogi. Nie odczytuje Ĺ›cieĹĽek z treĹ›ci dowodĂłw."""
    files, missing = {}, []
    for folder in PRIVATE_FOLDERS:
        directory = no_symlinks(Path(root)/folder)
        if not directory.exists():
            missing.append(folder)
            continue
        if not directory.is_dir():
            raise ValueError('Magazyn prywatnych raportĂłw nie jest katalogiem.')
        for path in directory.iterdir():
            no_symlinks(path)
            # NiedokoĹ„czony zapis atomowy nie jest opublikowanym raportem.
            if path.name.startswith('.review-') and path.suffix == '.tmp' and path.is_file():
                continue
            name = folder+'/'+path.name
            if not allowed_file(name) or not path.is_file():
                raise ValueError('Nieznany plik w magazynie raportĂłw lub dowodĂłw.')
            files[name] = path
            if len(files) > MAX_FILES-len(ALLOWED):
                raise ValueError('Liczba prywatnych plikĂłw przekracza limit migawki.')
    return files, missing


def evidence_summary(directory, names):
    """Kontroluje zarzÄ…dzane dowody; rÄ™czne ĹşrĂłdĹ‚a poza zestawem tylko zlicza."""
    directory = Path(directory)
    hashes = set()
    reports = 0
    for name in names:
        if name.startswith('data/location-evidence/'):
            identifier = Path(name).stem
            if digest(directory/name) != identifier:
                raise ValueError('Niezgodna suma dowodu lokalizacji.')
            hashes.add(identifier)
    for name in names:
        if not name.startswith('data/location-reviews/'):
            continue
        path = directory/name
        if path.stat().st_size > 32*1024*1024:
            raise ValueError('Prywatny raport przekracza limit odczytu.')
        report = json.loads(path.read_text(encoding='utf8'))
        if (not isinstance(report,dict) or not isinstance(report.get('object_id'),str)
                or hashlib.sha256(report['object_id'].encode()).hexdigest() != path.stem):
            raise ValueError('Raport lokalizacji ma niezgodny identyfikator.')
        references = report.get('evidence',[])
        if not isinstance(references,list) or any(not isinstance(row,dict)
                or not isinstance(row.get('sha256'),str) or row['sha256'] not in hashes for row in references):
            raise ValueError('Brakuje zarzÄ…dzanego dowodu wskazanego w raporcie.')
        reports += 1
    objects = json.loads((directory/'objects.json').read_text(encoding='utf8'))
    if not isinstance(objects,list):
        raise ValueError('Niepoprawny katalog obiektĂłw migawki.')
    manual, external = 0, 0
    for obj in objects:
        if not isinstance(obj,dict): raise ValueError('Niepoprawny obiekt katalogu migawki.')
        audit = obj.get('address_audit') or {}
        sources = audit.get('sources',[]) if isinstance(audit,dict) else []
        if not isinstance(sources,list): raise ValueError('Niepoprawna lista dowodĂłw adresowych.')
        for source in sources:
            manual += 1
            if not isinstance(source,dict) or not isinstance(source.get('sha256'),str) or source['sha256'] not in hashes:
                external += 1
    return dict(location_reports=reports,location_evidence=len(hashes),
                manual_address_sources=manual,external_address_sources=external)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def no_symlinks(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('ĹšcieĹĽka kopii nie moĹĽe zawieraÄ‡ dowiÄ…zaĹ„ symbolicznych.')
        if os.name == 'nt' and part.exists():
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('ĹšcieĹĽka kopii nie moĹĽe zawieraÄ‡ punktĂłw ponownej analizy.')
    return path


def copy_database(source, target, *, seconds=60):
    deadline = time.monotonic() + seconds
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError('Przekroczono czas tworzenia kopii SQLite.')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=5)) as original:
        with closing(sqlite3.connect(target)) as copied:
            original.backup(copied, pages=256, progress=progress, sleep=.05)
            copied.commit()
            # Gotowa kopia jest samodzielnym plikiem, niezaleĹĽnym od WAL ĹşrĂłdĹ‚a.
            copied.execute('PRAGMA journal_mode=DELETE')
            check_database(copied, deadline=deadline)


def check_database(connection, *, deadline=None):
    if deadline is not None:
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    rows = connection.execute('PRAGMA integrity_check').fetchall()
    if rows != [('ok',)] or connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Kopia bazy nie przeszĹ‚a kontroli integralnoĹ›ci.')


def create_snapshot(files, destination, *, missing_optional=(), revision=None):
    """Tworzy nowy zestaw; nigdy nie nadpisuje poprzedniej kopii."""
    destination = no_symlinks(destination)
    if destination.exists():
        raise ValueError('Katalog docelowy kopii juĹĽ istnieje.')
    if len(files) > MAX_FILES or any(not allowed_file(name) for name in files) or not REQUIRED <= set(files):
        raise ValueError('Niepoprawny zakres kopii aplikacji.')
    sources = {name: no_symlinks(path) for name,path in files.items()}
    if any(not path.is_file() for path in sources.values()):
        raise ValueError('Brakuje wymaganego pliku kopii.')
    if any(destination == path or destination in path.parents for path in sources.values()):
        raise ValueError('Kopia nie moĹĽe zawieraÄ‡ plikĂłw ĹşrĂłdĹ‚owych.')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    needed = sum(path.stat().st_size for path in sources.values()) * 2 + 100*1024*1024
    if shutil.disk_usage(destination.parent).free < needed:
        raise OSError('Za maĹ‚o miejsca na migawkÄ™ i zapas podczas kopiowania.')
    temporary = destination.with_name(destination.name+'.partial')
    temporary.mkdir(mode=0o700)
    manifest = dict(version=3 if POLICY_FILE in files else 2, scope='application', started_at=datetime.now(timezone.utc).isoformat(),
                    revision=revision, missing_optional=list(missing_optional), files=[])
    deadline = time.monotonic()+60
    try:
        for name, source in sources.items():
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            started = datetime.now(timezone.utc).isoformat()
            if name.startswith('data/') and source.suffix in ('.sqlite3', '.db'):
                copy_database(source, target, seconds=max(0, deadline-time.monotonic()))
            else:
                with source.open('rb') as reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader, writer)
            target.chmod(0o600)
            with target.open('r+b') as stream:
                os.fsync(stream.fileno())
            manifest['files'].append(dict(path=name, bytes=target.stat().st_size,
                                         sha256=digest(target), started_at=started))
        manifest['completed_at'] = datetime.now(timezone.utc).isoformat()
        inspect_database(temporary/'data/engine.sqlite3')
        for name, expected_type in (('config.json',dict),('objects.json',list)):
            if not isinstance(json.loads((temporary/name).read_text(encoding='utf8')),expected_type):
                raise ValueError('Niepoprawna struktura danych JSON kopii.')
        manifest['evidence'] = evidence_summary(temporary,set(sources))
        manifest['recovery_warnings'] = (['external_address_evidence_not_in_snapshot']
                                        if manifest['evidence']['external_address_sources'] else [])
        path = temporary/'manifest.json'
        content = json.dumps(manifest,ensure_ascii=False,indent=2)
        if len(content.encode('utf8')) > MAX_MANIFEST_BYTES:
            raise ValueError('Manifest kopii przekracza limit.')
        with path.open('x', encoding='utf8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
        # Utrwalenie wpisĂłw nowych podkatalogĂłw przed publikacjÄ… caĹ‚ego zestawu.
        for folder in PRIVATE_FOLDERS:
            if (temporary/folder).is_dir():
                sync_directory(temporary/folder)
        sync_directory(temporary/'data')
        sync_directory(temporary)
        temporary.rename(destination)
        sync_directory(destination.parent)
        return manifest
    except BaseException:
        # WĹ‚asny, nowo utworzony katalog roboczy, nigdy wczeĹ›niejsza kopia.
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def verify_snapshot(directory):
    directory = no_symlinks(directory)
    manifest_path = no_symlinks(directory/'manifest.json')
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest kopii przekracza limit.')
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] not in (1,2,3) or manifest.get('scope') != 'application':
        raise ValueError('NieobsĹ‚ugiwany format kopii.')
    entries = manifest.get('files')
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise ValueError('Niepoprawna lista plikĂłw kopii.')
    if any(not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
           or not isinstance(entry.get('bytes'), int) or entry['bytes'] < 0
           or not isinstance(entry.get('sha256'), str) for entry in entries):
        raise ValueError('Niepoprawne metadane pliku kopii.')
    names = [entry['path'] for entry in entries]
    if manifest['version'] == 3 and POLICY_FILE not in names:
        raise ValueError('Brakuje polityki publikacji w nowym formacie kopii.')
    if len(set(names)) != len(names) or any(not allowed_file(name,version=manifest['version']) for name in names) or not REQUIRED <= set(names):
        raise ValueError('Niepoprawne Ĺ›cieĹĽki manifestu.')
    actual=set()
    for path in directory.rglob('*'):
        no_symlinks(path)
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(names)|{'manifest.json'}:
        raise ValueError('Katalog kopii zawiera pliki spoza manifestu.')
    for entry in entries:
        path = no_symlinks(directory/entry['path'])
        if path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
            raise ValueError('Niezgodna suma lub rozmiar pliku kopii.')
        if path.suffix in ('.db','.sqlite3'):
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as connection:
                check_database(connection, deadline=time.monotonic()+60)
    inspect_database(directory/'data/engine.sqlite3')
    if manifest['version'] >= 2:
        summary = evidence_summary(directory,set(names))
        warnings = ['external_address_evidence_not_in_snapshot'] if summary['external_address_sources'] else []
        if manifest.get('evidence') != summary or manifest.get('recovery_warnings') != warnings:
            raise ValueError('Niezgodne podsumowanie dowodĂłw migawki.')
    return manifest


def restore_snapshot(directory, destination):
    """Odtwarzanie wyĹ‚Ä…cznie do nowego katalogu, bez uruchamiania usĹ‚ugi."""
    manifest = verify_snapshot(directory)
    directory = Path(directory).absolute()
    destination = no_symlinks(destination)
    if destination.exists():
        raise ValueError('Odtwarzanie wymaga nowego katalogu docelowego.')
    destination.mkdir(parents=True, mode=0o700)
    try:
        for entry in manifest['files']:
            target = destination/entry['path']
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(directory/entry['path'], target)
            target.chmod(0o600)
            if digest(target) != entry['sha256']:
                raise ValueError('Niezgodna suma odtworzonego pliku.')
        # OryginaĹ‚ konfiguracji pozostaje zachowany; osobna konfiguracja do prĂłby.
        config = json.loads((destination/'config.json').read_text(encoding='utf8'))
        config.update(notifications_enabled=False, our_api_url='',
                      web_host='127.0.0.1', geoportal_enabled=False)
        safe_config = destination/'config-restore-test.json'
        safe_config.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf8')
        safe_config.chmod(0o600)
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise
