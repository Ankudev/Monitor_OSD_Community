"""Stan pętli zadań, niezależny od aktualności danych operatora."""
import asyncio
import logging
import time
import math
import hmac
import os
from pathlib import Path
import json
from collections import deque
from datetime import datetime, timezone


readiness_logger = logging.getLogger('osd-monitor.readiness')


class JobHealth:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.phase = 'starting'
        self.deadline = clock() + 300
        self.last_progress_at = None
        self.task = None

    def mark(self, phase, seconds):
        self.phase = phase
        self.deadline = self.clock() + max(0, seconds) + 300
        self.last_progress_at = datetime.now(timezone.utc).isoformat()

    def payload(self, *, enabled=True):
        if not enabled:
            state = 'disabled'
        elif self.task is None or self.task.done():
            state = 'stopped'
        elif self.clock() > self.deadline:
            state = 'stalled'
        else:
            state = 'ok'
        return dict(state=state, phase=self.phase, last_progress_at=self.last_progress_at)


class Readiness:
    """Jedna próba odczytu naraz; timeout nie tworzy kolejki blokujących wątków."""
    def __init__(self, reader):
        self.reader = reader
        self.task = None
        self._last_failure = None

    async def _read(self):
        try:
            await asyncio.to_thread(self.reader)
        except Exception:
            return 'engine_read_error'
        return None

    def _record_result(self, reason, started):
        if reason != self._last_failure:
            elapsed_ms = (time.monotonic() - started) * 1000
            if reason is None:
                readiness_logger.info('Przywrócono gotowość (elapsed_ms=%.1f)', elapsed_ms)
            else:
                readiness_logger.warning(
                    'Sonda gotowości zgłasza niedostępność (reason=%s, elapsed_ms=%.1f)',
                    reason, elapsed_ms,
                )
            self._last_failure = reason
        return reason is None

    async def check(self, *, policy_available=True):
        started = time.monotonic()
        if not policy_available:
            return self._record_result('publication_policy_unavailable', started)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._read())
        task = self.task
        try:
            reason = await asyncio.wait_for(asyncio.shield(task), timeout=1)
        except TimeoutError:
            reason = 'engine_read_timeout'
        finally:
            if task.done() and self.task is task:
                self.task = None
        return self._record_result(reason, started)


class RequestMetrics:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.samples = deque(maxlen=512)

    def record(self, elapsed, status):
        self.samples.append((self.clock(), elapsed, status))

    def payload(self):
        rows = [row for row in self.samples if row[0] >= self.clock()-300]
        times = sorted(row[1] for row in rows)
        return dict(sample_count=len(rows), errors_5xx=sum(row[2] >= 500 for row in rows),
                    p95_seconds=times[max(0,math.ceil(len(times)*.95)-1)] if times else None,
                    window_seconds=300, sample_limit=512)


class OperationsToken:
    """Osobny sekret automatyzacji. Uprawnienia nadaje wyłącznie przypisana trasa."""
    def __init__(self, variable='PGE_OPERATIONS_TOKEN_FILE'):
        self.token = None
        try:
            path=Path(os.environ[variable])
            if path.stat().st_size > 256:
                return
            token=path.read_text(encoding='ascii').strip()
            if 32 <= len(token) <= 128 and all(33 <= ord(char) <= 126 for char in token):
                self.token=token
        except (OSError,KeyError,UnicodeError):
            pass

    def accepts(self, header):
        return bool(self.token and header.isascii() and hmac.compare_digest(header,'Bearer '+self.token))


def backup_health(now=None):
    configured=os.environ.get('PGE_BACKUP_STATUS_FILE')
    if not configured:
        return {'state':'unconfigured','offsite_verified':False}
    now=now or datetime.now(timezone.utc)
    def read(path):
        if path.stat().st_size > 65536: raise ValueError('Za duży plik stanu')
        value = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(value, dict): raise ValueError('Niepoprawny format stanu kopii')
        return value
    def age(value):
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None: raise ValueError('Brak strefy czasowej')
        seconds=(now-stamp).total_seconds()
        if seconds < -300: raise ValueError('Czas w przyszłości')
        return max(0,seconds)
    try:
        path=Path(configured)
        attempt_path=path.with_name(path.name+'.attempt.json')
        attempt=read(attempt_path) if attempt_path.exists() else {}
        if attempt.get('state') == 'failed':
            return {'state':'failed','offsite_verified':False}
        if attempt.get('state') == 'running' and age(attempt['started_at']) > 900:
            return {'state':'stalled','offsite_verified':False}
        status=read(path)
        seconds=age(status['data_started_at'])
        retention = status.get('retention', {})
        if not isinstance(retention, dict): raise ValueError('Niepoprawny stan retencji')
        retention_failed = retention.get('state') == 'failed'
        evidence_state = status.get('evidence_state','unknown')
        evidence_incomplete = evidence_state == 'requires_inventory'
        return {'state':'stale' if seconds > 3600 else 'warning' if seconds > 2700 or retention_failed or evidence_incomplete else 'fresh',
                'data_age_seconds':round(seconds), 'offsite_verified':False,
                'retention_state':retention.get('state','unconfigured'),
                'evidence_state':evidence_state,
                'attempt_state':attempt.get('state','unknown')}
    except (OSError,ValueError,KeyError,TypeError):
        return {'state':'unavailable','offsite_verified':False}
