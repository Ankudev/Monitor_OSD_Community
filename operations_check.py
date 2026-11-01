"""Ocena stanu operacyjnego bez wysyłania alarmów i bez odpytywania OSD."""
import math
import re
import time


def number(value, *, minimum=0):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('Niepoprawny pomiar operacyjny.')
    return value


def condition(code, severity, message):
    return dict(code=code, severity=severity, message=message)


def evaluate(payload, *, now=None, disk_min_bytes=512*1024*1024, api_p95_seconds=2,
             queue_grace_seconds=300):
    now = time.time() if now is None else number(now)
    number(disk_min_bytes)
    number(api_p95_seconds, minimum=.001)
    number(queue_grace_seconds)
    if not isinstance(payload, dict) or type(payload.get('local_ready')) is not bool:
        raise ValueError('Brak stanu gotowości aplikacji.')
    required = ('jobs','sources','disk','api','notifications','backups')
    if any(not isinstance(payload.get(key),dict) for key in required):
        raise ValueError('Niepełny stan operacyjny.')
    result = []
    if not payload['local_ready']:
        result.append(condition('local_storage', 'critical', 'Lokalny magazyn aplikacji jest niedostępny.'))
    jobs = payload['jobs']
    if not jobs:
        raise ValueError('Brak stanu zadań.')
    for name, job in jobs.items():
        if not isinstance(name,str) or not re.fullmatch('[a-z0-9_]{1,64}',name) or not isinstance(job,dict):
            raise ValueError('Niepoprawny opis zadania.')
        state = job.get('state')
        if state not in ('ok','disabled','stopped','stalled'):
            raise ValueError('Nieznany stan zadania.')
        if state in ('stopped','stalled'):
            result.append(condition('job:'+name, 'critical', 'Zadanie jest zatrzymane lub przekroczyło termin postępu.'))
    if not payload['sources']:
        raise ValueError('Brak stanu źródeł.')
    for source, health in payload['sources'].items():
        if (not isinstance(source,str) or not re.fullmatch('[a-z0-9_]{1,64}',source) or not isinstance(health,dict)
                or type(health.get('stale')) is not bool):
            raise ValueError('Niepoprawny stan źródła.')
        if health['stale']:
            result.append(condition('source:'+source,'warning','Źródło wymaga uwagi. Dane są nieaktualne lub odczyt się nie powiódł.'))
    disk = payload['disk']
    free, total = number(disk.get('free_bytes')), number(disk.get('total_bytes'),minimum=1)
    if free > total:
        raise ValueError('Niepoprawna pojemność dysku.')
    if free < disk_min_bytes or free / total < .05:
        result.append(condition('disk_space','critical','Mało wolnego miejsca na dysku aplikacji.'))
    inodes = disk.get('inodes')
    if inodes is not None:
        if not isinstance(inodes,dict): raise ValueError('Niepoprawny stan i-węzłów.')
        inode_free, inode_total = number(inodes.get('free')), number(inodes.get('total'))
        if inode_free > inode_total: raise ValueError('Niepoprawna liczba i-węzłów.')
        if inode_total and inode_free / inode_total < .05:
            result.append(condition('disk_inodes','critical','Mało wolnych i-węzłów na dysku aplikacji.'))
    api = payload['api']
    samples, failures = number(api.get('sample_count')), number(api.get('errors_5xx'))
    if failures > samples: raise ValueError('Niepoprawny licznik błędów API.')
    p95 = api.get('p95_seconds')
    if p95 is not None: number(p95)
    if samples >= 20:
        if p95 is None: raise ValueError('Brak pomiaru opóźnienia API.')
        if p95 > api_p95_seconds:
            result.append(condition('api_latency','warning','Opóźnienie API przekracza ustawiony próg p95.'))
        if failures / samples >= .05:
            result.append(condition('api_errors','warning','Co najmniej 5% próbek API zakończyło się błędem serwera.'))
    backup = payload['backups']
    state = backup.get('state')
    if state not in ('fresh','warning','stale','failed','stalled','unavailable','unconfigured'):
        raise ValueError('Nieznany stan kopii.')
    if state != 'fresh':
        level = 'warning' if state in ('warning','unconfigured') else 'critical'
        result.append(condition('backup',level,'Kopia wymaga uwagi. Sprawdź wiek danych, ostatnią próbę i konfigurację.'))
    if backup.get('retention_state') == 'failed':
        result.append(condition('backup_retention','warning','Kopia została zapisana, ale porządkowanie lokalnych migawek się nie powiodło.'))
    if backup.get('evidence_state') == 'requires_inventory':
        result.append(condition('backup_evidence','warning','Część ręcznych dowodów adresowych wymaga oddzielnej kopii odzyskiwania.'))
    queue = payload['notifications']
    if type(queue.get('enabled')) is not bool:
        raise ValueError('Brak stanu włączenia powiadomień.')
    if queue.get('state') == 'storage_error':
        result.append(condition('notification_storage','critical','Magazyn kolejki powiadomień jest niedostępny.'))
    elif queue['enabled']:
        counts = queue.get('counts')
        if not isinstance(counts,dict): raise ValueError('Brak liczników powiadomień.')
        if number(counts.get('failed')) or number(queue.get('blocked_by_failure')):
            result.append(condition('notification_failed','critical','Kolejka zawiera nieudane dostawy lub zablokowane zadania.'))
        due = queue.get('next_attempt_at')
        if due is not None and now - number(due) > queue_grace_seconds:
            result.append(condition('notification_overdue','warning','Minął termin wykonania oczekującej dostawy.'))
    return result


def transitions(previous, current, *, complete=True):
    """Brak odczytu zachowuje stare alarmy. Nie oznacza ich ustąpienia."""
    if (not isinstance(previous,dict) or any(not isinstance(code,str) or len(code)>96
            or not re.fullmatch('[a-z0-9_:]+',code) or level not in ('warning','critical')
            for code,level in previous.items())):
        raise ValueError('Niepoprawny poprzedni stan monitorowania.')
    active = {row['code']:row['severity'] for row in current}
    if not complete:
        active = dict(previous, **active)
    changes = []
    for code,level in sorted(active.items()):
        if previous.get(code) != level:
            changes.append(dict(code=code, severity=level,
                                event='opened' if code not in previous else 'changed'))
    for code in sorted(previous.keys()-active.keys()):
        changes.append(dict(code=code, severity='ok', event='resolved'))
    return active, changes
