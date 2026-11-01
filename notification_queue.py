"""Trwałe zadania webhooka. HTTP zawsze poza transakcją magazynu."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

STATUSES = ('pending', 'sending', 'delivered', 'failed', 'skipped')
DELAYS = (60, 300, 900, 3600, 10800)


def destination_id(url):
    return hashlib.sha256(url.strip().encode()).hexdigest()


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.fragment:
        raise ValueError('our_api_url musi być adresem HTTP/HTTPS bez fragmentu')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('Niepoprawny port odbiorcy') from error
    return url


def create_schema(c):
    c.execute('''CREATE TABLE notification_jobs (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL UNIQUE,
        change_id TEXT NOT NULL REFERENCES event_changes(change_id),
        destination_id TEXT NOT NULL, destination_url TEXT NOT NULL,
        source TEXT NOT NULL, object_id TEXT NOT NULL, action TEXT NOT NULL,
        object_fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','sending','delivered','failed','skipped')),
        created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        max_attempts INTEGER NOT NULL DEFAULT 6 CHECK(max_attempts>=attempts),
        last_attempt_at REAL, lease_until REAL, lease_token TEXT,
        delivered_at REAL, finished_at REAL, last_http_status INTEGER,
        last_error_code TEXT, last_error TEXT,
        last_decision_at REAL, last_decision_reason TEXT,
        UNIQUE(change_id,destination_id))''')
    for name, columns in [('due', 'status,next_attempt_at,sequence'),
                          ('lease', 'status,lease_until'),
                          ('ordering', 'destination_id,object_id,sequence')]:
        c.execute(f'CREATE INDEX notification_jobs_{name} ON notification_jobs({columns})')
    c.executemany('INSERT INTO schema_meta(key,value) VALUES(?,?)', [
        ('queue_started_at', datetime.now(timezone.utc).isoformat()), ('queue_payload_version', '2')])
    c.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")


def make_job(record, event, url, fingerprint, payload, now=None):
    dest = destination_id(url)
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'osd-r7:'+record['change_id']+':'+dest))
    now = time.time() if now is None else now
    body = dict(payload, notification_id=job_id, change_id=record['change_id'],
                payload_version=2, recorded_at=record['recorded_at'],
                queued_at=datetime.fromtimestamp(now, timezone.utc).isoformat())
    return dict(job_id=job_id, change_id=record['change_id'], destination_id=dest,
                destination_url=url, source=event['source'], object_id=str(event['object_id']),
                action=record['action'], object_fingerprint=fingerprint,
                payload_json=json.dumps(body, ensure_ascii=False, allow_nan=False, sort_keys=True),
                created_at=now, next_attempt_at=now)


def insert_jobs(c, jobs):
    for job in jobs:
        old = c.execute('SELECT payload_json,destination_url FROM notification_jobs WHERE change_id=? AND destination_id=?',
                        (job['change_id'], job['destination_id'])).fetchone()
        if old:
            if old != (job['payload_json'], job['destination_url']):
                raise ValueError('Sprzeczny duplikat zadania powiadomienia')
            continue
        keys = tuple(job)
        c.execute('INSERT INTO notification_jobs ('+','.join(keys)+",status) VALUES("+
                  ','.join('?' for _ in keys)+",'pending')", tuple(job[k] for k in keys))


def validate_queue(c, full=False):
    if not c.execute("SELECT 1 FROM schema_meta WHERE key='queue_started_at'").fetchone():
        raise ValueError('Brak metadanych kolejki')
    if full and c.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Uszkodzone powiązanie historii i kolejki')
    if not full:
        c.execute('SELECT job_id,status FROM notification_jobs LIMIT 1')
        return
    c.row_factory = sqlite3.Row
    try:
        for row in c.execute('SELECT * FROM notification_jobs'):
            payload = json.loads(row['payload_json'])
            json.dumps(payload, allow_nan=False)
            if (payload.get('notification_id') != row['job_id'] or payload.get('change_id') != row['change_id']
                    or row['destination_id'] != destination_id(row['destination_url'])
                    or payload.get('object_id') != row['object_id']
                    or payload.get('action') != row['action'] or payload.get('source') != row['source']):
                raise ValueError('Treść powiadomienia nie zgadza się z zadaniem')
            if (row['status'] == 'sending') != (row['lease_token'] is not None and row['lease_until'] is not None):
                raise ValueError('Niepoprawna rezerwacja zadania')
            if row['status'] != 'sending' and (row['lease_token'] is not None or row['lease_until'] is not None):
                raise ValueError('Rezerwacja zakończonego zadania')
            for field in ('created_at', 'next_attempt_at', 'last_attempt_at', 'lease_until', 'delivered_at', 'finished_at'):
                if row[field] is not None and not math.isfinite(row[field]):
                    raise ValueError('Niepoprawny czas zadania')
            validate_url(row['destination_url'])
    finally:
        c.row_factory = None


def claim(c, dest, now):
    # Niepewna próba po śmierci procesu też podlega odstępom retry.
    for job_id, attempts, next_at in c.execute("SELECT job_id,attempts,next_attempt_at FROM notification_jobs WHERE status='sending' AND lease_until<=?", (now,)).fetchall():
        delay = DELAYS[min(max(attempts-1, 0), len(DELAYS)-1)]
        c.execute('UPDATE notification_jobs SET next_attempt_at=? WHERE job_id=?', (max(next_at, now+delay), job_id))
    c.execute("""UPDATE notification_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'pending' END,
        lease_token=NULL,lease_until=NULL,last_error_code='lease_expired',
        last_error='Dostarczenie w ostatniej próbie nie jest potwierdzone',
        finished_at=CASE WHEN attempts>=max_attempts THEN ? ELSE NULL END
        WHERE status='sending' AND lease_until<=?""", (now, now))
    cursor = c.execute('''SELECT j.* FROM notification_jobs j
        WHERE j.destination_id=? AND j.status='pending' AND j.next_attempt_at<=?
        AND j.attempts<j.max_attempts AND NOT EXISTS (
            SELECT 1 FROM notification_jobs prior WHERE prior.destination_id=j.destination_id
            AND prior.object_id=j.object_id AND prior.sequence<j.sequence
            AND prior.status IN ('pending','sending','failed'))
        ORDER BY j.sequence LIMIT 1''', (dest, now))
    row = cursor.fetchone()
    if row is None:
        return None
    job = dict(zip((col[0] for col in cursor.description), row))
    token = str(uuid.uuid4())
    c.execute("UPDATE notification_jobs SET status='sending',attempts=attempts+1,last_attempt_at=?,lease_until=?,lease_token=? WHERE job_id=?",
              (now, now+90, token, job['job_id']))
    job.update(status='sending', attempts=job['attempts']+1, lease_token=token, lease_until=now+90)
    return job


def finish(c, job, outcome, now):
    status = outcome['status']
    if status == 'pending' and job['attempts'] >= job['max_attempts']:
        status = 'failed'
    delay = DELAYS[min(job['attempts']-1, len(DELAYS)-1)]
    next_at = max(now+delay, outcome.get('retry_after') or 0)
    cursor = c.execute('''UPDATE notification_jobs SET status=?,next_attempt_at=?,lease_until=NULL,
        lease_token=NULL,delivered_at=?,finished_at=?,last_http_status=?,last_error_code=?,last_error=?
        WHERE job_id=? AND status='sending' AND lease_token=? AND lease_until>?''',
        (status, next_at, now if status=='delivered' else None,
         now if status in ('delivered','failed','skipped') else None,
         outcome.get('http_status'), outcome.get('code'), outcome.get('error'),
         job['job_id'], job['lease_token'], now))
    return cursor.rowcount == 1


def summary(c, dest, now):
    counts = {status: 0 for status in STATUSES}
    counts.update(dict(c.execute('SELECT status,COUNT(*) FROM notification_jobs GROUP BY status')))
    oldest = c.execute("SELECT MIN(created_at) FROM notification_jobs WHERE status='pending'").fetchone()[0]
    due = c.execute("""SELECT MIN(j.next_attempt_at) FROM notification_jobs j
        WHERE j.status='pending' AND j.destination_id=? AND NOT EXISTS (
        SELECT 1 FROM notification_jobs prior WHERE prior.destination_id=j.destination_id
        AND prior.object_id=j.object_id AND prior.sequence<j.sequence
        AND prior.status IN ('pending','sending','failed'))""", (dest,)).fetchone()[0]
    blocked = c.execute('''SELECT COUNT(*) FROM notification_jobs j WHERE j.status='pending' AND EXISTS
        (SELECT 1 FROM notification_jobs p WHERE p.object_id=j.object_id AND p.destination_id=j.destination_id
        AND p.sequence<j.sequence AND p.status='failed')''').fetchone()[0]
    foreign = c.execute("SELECT COUNT(*) FROM notification_jobs WHERE status IN ('pending','sending','failed') AND destination_id!=?", (dest,)).fetchone()[0]
    last = c.execute("SELECT last_error_code FROM notification_jobs WHERE last_error_code IS NOT NULL ORDER BY sequence DESC LIMIT 1").fetchone()
    recent = [dict(notification_id=row[0], action=row[1], result=row[2], http_status=row[3], error=row[4])
              for row in c.execute('SELECT job_id,action,status,last_http_status,last_error FROM notification_jobs ORDER BY sequence DESC LIMIT 10')]
    return dict(counts=counts, oldest_pending_at=oldest, next_attempt_at=due, recent=recent,
                blocked_by_failure=blocked, other_destination_count=foreign,
                last_error_code=last[0] if last else None)


def delivery_history(c, limit=20, offset=0, status=None):
    if not 1 <= limit <= 100 or offset < 0 or (status is not None and status not in STATUSES):
        raise ValueError('Niepoprawne parametry historii powiadomień.')
    fields = ('job_id','change_id','source','object_id','action','status','attempts','max_attempts',
              'created_at','last_attempt_at','next_attempt_at','delivered_at','finished_at',
              'last_http_status','last_error_code','last_decision_at','last_decision_reason')
    where = ' WHERE status=?' if status is not None else ''
    args = (status,) if status is not None else ()
    total = c.execute('SELECT COUNT(*) FROM notification_jobs'+where,args).fetchone()[0]
    rows = c.execute('SELECT '+','.join(fields)+' FROM notification_jobs'+where+
                     ' ORDER BY sequence DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
    return dict(total=total,limit=limit,offset=offset,items=[dict(zip(fields,row)) for row in rows])


def decide(c, job_id, operation, reason, now):
    if not reason.strip():
        raise ValueError('Wymagany powód decyzji')
    row = c.execute('SELECT status,lease_until FROM notification_jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None or row[0] in ('delivered','skipped') or (row[0]=='sending' and row[1]>now):
        raise ValueError('Zadanie nie istnieje, jest zakończone albo trwa wysyłka')
    if operation == 'retry':
        if row[0] != 'failed':
            raise ValueError('Ponowić można tylko zadanie ze statusem failed')
        c.execute("UPDATE notification_jobs SET status='pending',max_attempts=attempts+6,next_attempt_at=?,finished_at=NULL,last_decision_at=?,last_decision_reason=? WHERE job_id=?",
                  (now,now,reason,job_id))
    elif operation == 'skip':
        c.execute("UPDATE notification_jobs SET status='skipped',lease_token=NULL,lease_until=NULL,finished_at=?,last_decision_at=?,last_decision_reason=? WHERE job_id=?", (now,now,reason,job_id))
    else:
        raise ValueError('Nieznana decyzja')


def retry_after(value, now):
    if not value:
        return None
    try:
        seconds = int(value)
        result = now+seconds if seconds >= 0 else None
        return result if result is not None and math.isfinite(result) else None
    except (ValueError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except (TypeError, ValueError, OverflowError):
            return None


async def deliver(session, job, now=None):
    provider=os.environ.get('PGE_NOTIFICATION_PROVIDER') or 'webhook'
    if provider == 'onesignal':
        from onesignal_push import deliver as deliver_push
        return await deliver_push(session,job,now)
    if provider != 'webhook':
        return dict(status='failed',code='notification_provider',error='Niepoprawna konfiguracja dostawcy powiadomień.')
    try:
        async with session.post(job['destination_url'], data=job['payload_json'].encode('utf-8'),
                                headers={'Content-Type':'application/json','Idempotency-Key':job['job_id']},
                                timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False) as response:
            status = response.status
            if 200 <= status < 300:
                return {'status':'delivered','http_status':status}
            await response.content.read(2048)
            return dict(status='pending' if status in (408,429) or status>=500 else 'failed',
                        http_status=status,code='http_'+str(status),error=f'Odbiorca zwrócił HTTP {status}',
                        retry_after=retry_after(response.headers.get('Retry-After'), time.time() if now is None else now))
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        return dict(status='pending',code=type(error).__name__,error='Błąd transportu webhooka: '+type(error).__name__)


class NotificationWorker:
    def __init__(self, store, config, eligibility):
        from runtime_health import JobHealth
        self.health = JobHealth()
        self.store, self.config, self.eligibility = store, config, eligibility
        self.wake = asyncio.Event()
        self.task = None
        self.error = None
        self.state = 'disabled'

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        async with aiohttp.ClientSession() as session:
            while True:
                self.health.mark('delivery', 90)
                config = self.config()
                try:
                    if config['notifications_enabled'] and config['our_api_url']:
                        self.state = 'running'
                        job = await self.store.queue_operation('claim', destination_id(config['our_api_url']), time.time())
                        if job:
                            reason = await self.eligibility(job)
                            outcome = {'status':'skipped','code':'catalog_changed','error':reason} if reason else await deliver(session, job)
                            await self.store.queue_operation('finish', job, outcome, time.time())
                            self.error = None
                            continue
                    else:
                        self.state = 'disabled'
                    self.error = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.error = 'Magazyn kolejki: '+type(error).__name__
                    self.state = 'storage_error'
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=30)
                except TimeoutError:
                    pass
                self.wake.clear()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.state = 'stopped'


def main():
    parser = argparse.ArgumentParser(description='Odzyskanie pojedynczego zadania R7')
    parser.add_argument('database')
    parser.add_argument('operation', choices=['status','retry','skip'])
    parser.add_argument('--job-id')
    parser.add_argument('--reason')
    parser.add_argument('--status', choices=STATUSES)
    args = parser.parse_args()
    from pathlib import Path
    c = sqlite3.connect(Path(args.database).resolve().as_uri()+('?mode=ro' if args.operation=='status' else '?mode=rw'), uri=True, timeout=30)
    try:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA synchronous=FULL')
        with c:
            c.execute('BEGIN' if args.operation=='status' else 'BEGIN IMMEDIATE')
            if c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] != '3':
                raise ValueError('Narzędzie wymaga schematu 3')
            if args.operation=='status':
                columns = ('job_id','change_id','object_id','source','action','status','attempts','max_attempts','next_attempt_at','last_error_code','last_error')
                query = 'SELECT '+','.join(columns)+' FROM notification_jobs'
                conditions, values = [], []
                if args.job_id:
                    conditions.append('job_id=?'); values.append(args.job_id)
                if args.status:
                    conditions.append('status=?'); values.append(args.status)
                if conditions:
                    query += ' WHERE '+' AND '.join(conditions)
                jobs = [dict(zip(columns,row)) for row in c.execute(query+' ORDER BY sequence DESC LIMIT 50',values)]
                print(json.dumps({'counts':dict(c.execute('SELECT status,COUNT(*) FROM notification_jobs GROUP BY status')), 'jobs':jobs},ensure_ascii=False))
            else:
                if not args.job_id or not args.reason:
                    parser.error('retry/skip wymaga --job-id i --reason')
                decide(c,args.job_id,args.operation,args.reason,time.time())
                print('Decyzja zapisana')
    finally:
        c.close()


if __name__ == '__main__':
    main()
