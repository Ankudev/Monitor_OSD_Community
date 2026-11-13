"""Trwały budżet PGE wspólny dla usługi i narzędzi; nie przechowuje tokenu."""
from contextlib import contextmanager, closing
import math
import os
from pathlib import Path
import sqlite3
import time

from pge_credentials import PgeAccessError, private_directory, check_file

DEFAULT_DIRECTORY = '/var/lib/monitor-osd-community-live/pge-api'
WINDOW_SECONDS = 3600
MAX_REQUESTS = 50


@contextmanager
def locked_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        check_file(os.fstat(fd))
        if os.name == 'posix':
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        yield
    finally:
        os.close(fd)


class RequestBudget:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('OSD_COMMUNITY_PGE_STATE_DIR', DEFAULT_DIRECTORY))

    @contextmanager
    def connect(self):
        try:
            private_directory(self.directory)
            with locked_file(self.directory / 'request.lock'):
                path = self.directory / 'requests.sqlite3'
                # Baza tworzona bez okna, w którym ma zbyt szerokie prawa.
                fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    check_file(os.fstat(fd))
                finally:
                    os.close(fd)
                with closing(sqlite3.connect(path, timeout=0)) as db:
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('CREATE TABLE IF NOT EXISTS attempts (at REAL NOT NULL)')
                    db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), '
                               'clock REAL NOT NULL, blocked REAL NOT NULL, paused INTEGER NOT NULL, '
                               'inflight INTEGER NOT NULL)')
                    db.execute('INSERT OR IGNORE INTO state VALUES (1, 0, 0, 0, 0)')
                    db.commit()
                    yield db
        except PgeAccessError:
            raise
        except (OSError, sqlite3.Error):
            raise PgeAccessError('PGE: magazyn limitu jest niedostępny lub trwa inny odczyt.', 'pge_budget') from None

    @staticmethod
    def reserve(db, now):
        clock, blocked, paused, inflight = db.execute(
            'SELECT clock, blocked, paused, inflight FROM state WHERE id=1').fetchone()
        if not math.isfinite(now) or now < clock:
            raise PgeAccessError('PGE: zegar cofnął się; odczyt został wstrzymany.', 'pge_budget')
        if paused or inflight:
            raise PgeAccessError('PGE: odczyty wstrzymano. Administrator musi sprawdzić dostęp i wznowić próby.', 'pge_paused')
        if now < blocked:
            raise PgeAccessError('PGE: trwa przerwa wyznaczona po ograniczeniu liczby zapytań.', 'pge_budget')
        db.execute('DELETE FROM attempts WHERE at <= ?', (now - WINDOW_SECONDS,))
        if db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] >= MAX_REQUESTS:
            raise PgeAccessError('PGE: osiągnięto lokalny limit zapytań. Zachowano poprzednie dane.', 'pge_budget')
        db.execute('INSERT INTO attempts VALUES (?)', (now,))
        # Awaria procesu lub zapisu wyniku pozostawia blokadę, nie nowe prawo do próby.
        db.execute('UPDATE state SET clock=?, inflight=1 WHERE id=1', (now,))
        db.commit()

    @contextmanager
    def attempt(self):
        with self.connect() as db:
            self.reserve(db, time.time())
            try:
                yield
            except BaseException as error:
                status = getattr(error, 'status', None)
                retry = getattr(error, 'retry_after', None)
                blocked = max(time.time() + WINDOW_SECONDS,
                              retry.timestamp() if retry is not None else 0) if status == 429 else 0
                with db:
                    db.execute('UPDATE state SET inflight=0, paused=?, blocked=MAX(blocked, ?) WHERE id=1',
                               (int(status in (401, 403)), blocked))
                raise
            else:
                with db:
                    db.execute('UPDATE state SET inflight=0 WHERE id=1')

    def resume(self):
        """Jawne wznowienie nie zeruje licznika, blokady 429 ani zegara."""
        with self.connect() as db, db:
            db.execute('UPDATE state SET paused=0, inflight=0 WHERE id=1')
