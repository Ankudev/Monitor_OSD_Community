"""Kontrola, szyfrowane wysyłanie i izolowane odtwarzanie przygotowanej migawki."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import shutil
from contextlib import contextmanager
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service_backup import verify_snapshot, restore_snapshot, no_symlinks
from backup_retention import (snapshot_name, restic_id, repository_key, receipt_path,
                              upload_receipt, retention_plan, checked_local_path, read_metadata)


def atomic_status(path, value):
    group=os.environ.get('PGE_BACKUP_STATUS_GROUP')
    gid=None
    if group:
        if os.name != 'posix':
            raise ValueError('Udostępnianie stanu grupie wymaga systemu Linux.')
        import grp
        try:
            gid=grp.getgrnam(group).gr_gid
        except KeyError:
            raise ValueError('Nie znaleziono grupy odczytu stanu kopii.') from None
    path=no_symlinks(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor,name=tempfile.mkstemp(dir=path.parent,prefix='.backup-status-')
    try:
        with os.fdopen(descriptor,'w',encoding='utf8') as stream:
            if gid is not None:
                # Ustal uprawnienia przed publikacją, bez okna niedostępności.
                os.fchown(stream.fileno(),-1,gid)
                os.fchmod(stream.fileno(),0o640)
            json.dump(value,stream,ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if Path(name).exists(): Path(name).unlink()


@contextmanager
def exclusive_job(path):
    path=no_symlinks(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0: os.write(descriptor,b'0')
            os.lseek(descriptor,0,os.SEEK_SET)
            msvcrt.locking(descriptor,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_origin(origin):
    parsed=urlsplit(origin)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Wymagany adres aplikacji bez ścieżki i danych dostępowych.')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1','::1'):
        raise ValueError('HTTP jest dozwolone wyłącznie dla lokalnego adresu pętli zwrotnej.')
    parsed.port  # Walidacja portu przed otwarciem połączenia.
    return origin.rstrip('/')


def run_backup(origin, status_path, *, keep_local=None):
    if keep_local is None and os.environ.get('PGE_BACKUP_KEEP_LOCAL'):
        keep_local = int(os.environ['PGE_BACKUP_KEEP_LOCAL'])
    if keep_local is not None and (type(keep_local) is not int or not 2 <= keep_local <= 48):
        raise ValueError('Liczba zachowanych kopii musi wynosić od 2 do 48.')
    origin = validate_origin(origin)
    status_path=Path(status_path)
    with exclusive_job(status_path.with_name(status_path.name+'.lock')):
        attempt_path=status_path.with_name(status_path.name+'.attempt.json')
        attempt={'started_at':datetime.now(timezone.utc).isoformat(),'state':'running'}
        atomic_status(attempt_path,attempt)
        try:
            if not shutil.which('restic'):
                raise ValueError('Nie znaleziono programu restic.')
            if not os.environ.get('RESTIC_REPOSITORY') or not Path(os.environ['RESTIC_PASSWORD_FILE']).is_file():
                raise ValueError('Nie skonfigurowano repozytorium kopii.')
            token=Path(os.environ['PGE_BACKUP_TOKEN_FILE']).read_text(encoding='ascii').strip()
            if not 32 <= len(token) <= 128 or not all(33 <= ord(char) <= 126 for char in token):
                raise ValueError('Niepoprawny token tworzenia kopii.')
            staging=no_symlinks(os.environ['PGE_BACKUP_STAGING'])
            request=Request(origin.rstrip('/')+'/api/operations/snapshot',data=b'',method='POST',headers={'Authorization':'Bearer '+token})
            with build_opener(NoRedirect()).open(request,timeout=120) as response:
                payload=json.loads(response.read(16384))
            identifier=str(uuid.UUID(payload['snapshot_id']))
            result=upload(staging/identifier,status_path)
            if keep_local is not None:
                try:
                    cleaned = prune_local_unlocked(staging, status_path, keep_local, apply=True)
                    result['retention'] = {'state':'ok', 'deleted_count':len(cleaned['deleted'])}
                except Exception:
                    # Kopia została zapisana. Osobno raportujemy błąd porządkowania.
                    result['retention'] = {'state':'failed'}
                atomic_status(status_path, result)
            attempt.update(state='success',finished_at=datetime.now(timezone.utc).isoformat())
            atomic_status(attempt_path,attempt)
            return result
        except Exception:
            attempt.update(state='failed',finished_at=datetime.now(timezone.utc).isoformat())
            atomic_status(attempt_path,attempt)
            raise ValueError('Próba wykonania kopii nie zakończyła się powodzeniem.') from None


def upload(snapshot, status_path):
    manifest = verify_snapshot(snapshot)
    password = os.environ.get('RESTIC_PASSWORD_FILE')
    if not os.environ.get('RESTIC_REPOSITORY') or not password or not Path(password).is_file():
        raise ValueError('Wymagane są RESTIC_REPOSITORY i plik RESTIC_PASSWORD_FILE.')
    status_path = no_symlinks(status_path)
    snapshot = Path(snapshot).resolve()
    if status_path == snapshot or snapshot in status_path.parents:
        raise ValueError('Stan wysyłki musi być przechowywany poza migawką.')
    try:
        result = subprocess.run(['restic','backup','--json','--tag','monitor-osd',str(snapshot)],
                                capture_output=True, text=True, timeout=600, check=False)
    except (OSError,subprocess.TimeoutExpired):
        raise ValueError('Nie udało się uruchomić lub zakończyć szyfrowanej wysyłki.') from None
    if result.returncode:
        # Wyjście narzędzia może zawierać adres i dane dostępowe repozytorium.
        raise ValueError('Wysyłka kopii nie zakończyła się powodzeniem.')
    try:
        summary = next((row for row in (json.loads(line) for line in result.stdout.splitlines() if line.strip())
                        if isinstance(row, dict) and row.get('message_type') == 'summary'
                        and isinstance(row.get('snapshot_id'), str) and row['snapshot_id']), None)
    except ValueError:
        raise ValueError('Niepoprawne potwierdzenie programu restic.') from None
    if summary is None or not restic_id(summary['snapshot_id']):
        raise ValueError('Brak potwierdzenia zapisania migawki w repozytorium.')
    status = dict(snapshot_id=summary['snapshot_id'], data_started_at=manifest['started_at'],
                  uploaded_at=datetime.now(timezone.utc).isoformat(), scope='application',
                  offsite_verified=False)
    status['evidence_state'] = ('requires_inventory' if manifest.get('recovery_warnings')
                                else 'managed_complete' if manifest['version'] >= 2 else 'unknown')
    if snapshot_name(snapshot.name):
        receipt = upload_receipt(snapshot, summary['snapshot_id'])
        atomic_status(receipt_path(snapshot), receipt)
        status.update(local_snapshot_name=snapshot.name, repository_key=repository_key())
    atomic_status(status_path,status)
    return status


def prune_local_unlocked(staging, status_path, keep, *, apply=False):
    staging = no_symlinks(staging).resolve()
    status_path = no_symlinks(status_path)
    if status_path.resolve().is_relative_to(staging):
        raise ValueError('Stan retencji musi być przechowywany poza miejscem migawek.')
    status = read_metadata(status_path)
    if status.get('scope') != 'application' or status.get('repository_key') != repository_key():
        raise ValueError('Brak sukcesu kopii dla wybranego repozytorium.')
    try:
        response = subprocess.run(['restic','snapshots','--json','--tag','monitor-osd'],
                                  capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Nie można potwierdzić migawek w repozytorium.') from None
    if response.returncode:
        raise ValueError('Odczyt repozytorium przed retencją nie powiódł się.')
    plan = retention_plan(staging, status.get('local_snapshot_name'), keep, json.loads(response.stdout))
    current = read_metadata(receipt_path(checked_local_path(staging, status['local_snapshot_name'])))
    if current.get('snapshot_id') != status.get('snapshot_id'):
        raise ValueError('Bieżący sukces nie jest zgodny z potwierdzeniem migawki.')
    deleted = []
    if apply:
        for name in plan['delete']:
            path = checked_local_path(staging, name)
            # Ponowna kontrola bezpośrednio przed operacją na własnym katalogu.
            receipt = read_metadata(receipt_path(path))
            if receipt.get('manifest_sha256') != upload_receipt(path, receipt['snapshot_id'])['manifest_sha256']:
                raise ValueError('Migawka zmieniła się po przygotowaniu planu retencji.')
            verify_snapshot(path)
            shutil.rmtree(path)
            receipt_path(path).unlink()
            deleted.append(name)
    return dict(plan, applied=apply, deleted=deleted)


def prune_local(staging, status_path, keep, *, apply=False):
    status_path = Path(status_path)
    with exclusive_job(status_path.with_name(status_path.name+'.lock')):
        return prune_local_unlocked(staging, status_path, keep, apply=apply)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    verify=sub.add_parser('verify',help='Sprawdź manifest, sumy i bazy')
    verify.add_argument('snapshot',type=Path)
    restore=sub.add_parser('restore',help='Odtwórz pliki do nowego katalogu; nie uruchamia usługi')
    restore.add_argument('snapshot',type=Path)
    restore.add_argument('destination',type=Path)
    send=sub.add_parser('upload',help='Wyślij zweryfikowaną migawkę przez restic')
    send.add_argument('snapshot',type=Path)
    send.add_argument('--status',required=True,type=Path)
    scheduled=sub.add_parser('run',help='Jedna próba: migawka aplikacji i szyfrowana wysyłka')
    scheduled.add_argument('--origin',required=True)
    scheduled.add_argument('--status',required=True,type=Path)
    scheduled.add_argument('--keep-local',type=int,help='Zachowaj co najmniej tyle potwierdzonych kopii lokalnych (2 do 48)')
    clean=sub.add_parser('prune-local',help='Podgląd retencji lokalnych migawek; domyślnie nie usuwa plików')
    clean.add_argument('staging',type=Path)
    clean.add_argument('--status',required=True,type=Path)
    clean.add_argument('--keep',required=True,type=int)
    clean.add_argument('--apply',action='store_true',help='Usuń wyłącznie potwierdzone nadmiarowe migawki lokalne')
    args=parser.parse_args()
    try:
        if args.command == 'verify':
            result=verify_snapshot(args.snapshot)
            print(json.dumps({'verified':True,'scope':result['scope'],'file_count':len(result['files']),
                              'recovery_warnings':result.get('recovery_warnings',[])}))
        elif args.command == 'restore':
            restore_snapshot(args.snapshot,args.destination)
            print(json.dumps({'restored':True,'service_started':False}))
        elif args.command == 'upload':
            with exclusive_job(args.status.with_name(args.status.name+'.lock')):
                print(json.dumps(upload(args.snapshot,args.status)))
        elif args.command == 'prune-local':
            print(json.dumps(prune_local(args.staging,args.status,args.keep,apply=args.apply)))
        else:
            print(json.dumps(run_backup(args.origin,args.status,keep_local=args.keep_local)))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        print('Operacja kopii nie powiodła się: '+type(error).__name__,file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
