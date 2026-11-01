"""Jednorazowa kontrola operacyjna. Wynik JSON i kod wyjścia, bez wysyłki alarmów."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import sys
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operations_check import condition, evaluate, transitions, number
from service_backup import no_symlinks
from tools.backup_service import NoRedirect, validate_origin, atomic_status, exclusive_job


def fetch_operations(origin):
    origin = validate_origin(origin)
    token_path = no_symlinks(os.environ['PGE_OPERATIONS_TOKEN_FILE'])
    if token_path.stat().st_size > 256:
        raise ValueError('Niepoprawny plik tokenu monitorowania.')
    token = token_path.read_text(encoding='ascii').strip()
    if not 32 <= len(token) <= 128 or not all(33 <= ord(char) <= 126 for char in token):
        raise ValueError('Niepoprawny token monitorowania.')
    request = Request(origin+'/api/operations', headers={'Authorization':'Bearer '+token, 'Cache-Control':'no-cache'})
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(1024*1024+1)
    if len(raw) > 1024*1024:
        raise ValueError('Odpowiedź operacyjna przekracza limit.')
    return json.loads(raw)


def check(origin, state_path, **thresholds):
    origin = validate_origin(origin)
    number(thresholds.get('disk_min_bytes',512*1024*1024))
    number(thresholds.get('api_p95_seconds',2),minimum=.001)
    number(thresholds.get('queue_grace_seconds',300))
    origin_key = hashlib.sha256(origin.encode('utf8')).hexdigest()
    state_path = no_symlinks(state_path)
    with exclusive_job(state_path.with_name(state_path.name+'.lock')):
        previous = {}
        if state_path.exists():
            if state_path.stat().st_size > 1024*1024:
                raise ValueError('Stan kontroli przekracza limit.')
            saved = json.loads(state_path.read_text(encoding='utf8'))
            if (not isinstance(saved,dict) or saved.get('version') != 1
                    or saved.get('origin_key') != origin_key):
                raise ValueError('Niepoprawny stan poprzedniej kontroli.')
            previous = saved['active']
        # Walidacja poprzedniego stanu przed połączeniem i jakimkolwiek zapisem.
        transitions(previous, [])
        complete = True
        try:
            conditions = evaluate(fetch_operations(origin), **thresholds)
        except (OSError,ValueError,KeyError,TypeError):
            complete = False
            conditions = [condition('probe_unavailable','critical','Nie można odczytać chronionego stanu aplikacji.')]
        active, changes = transitions(previous, conditions, complete=complete)
        checked_at = datetime.now(timezone.utc).isoformat()
        atomic_status(state_path, dict(version=1, checked_at=checked_at, origin_key=origin_key, active=active))
        state = 'critical' if 'critical' in active.values() else 'warning' if active else 'ok'
        return dict(state=state, checked_at=checked_at, complete=complete,
                    active=active, changes=changes, conditions=conditions, notifications_sent=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',required=True)
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--disk-min-mib',type=int,default=512)
    parser.add_argument('--api-p95-seconds',type=float,default=2)
    parser.add_argument('--queue-grace-seconds',type=int,default=300)
    args = parser.parse_args()
    try:
        result = check(args.origin,args.state,disk_min_bytes=args.disk_min_mib*1024*1024,
                       api_p95_seconds=args.api_p95_seconds,queue_grace_seconds=args.queue_grace_seconds)
        print(json.dumps(result,ensure_ascii=False))
        return {'ok':0,'warning':1,'critical':2}[result['state']]
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'state':'unknown','error':'Nie można wykonać lub utrwalić kontroli.','notifications_sent':False},ensure_ascii=False))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
