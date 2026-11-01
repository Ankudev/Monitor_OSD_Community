"""Ograniczony klient geokodera z deduplikacją i trwałą pamięcią podręczną SQLite."""

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


class GeocoderError(RuntimeError):
    pass


class GeocoderBusyError(GeocoderError):
    pass


class GeocoderNotFoundError(GeocoderError):
    pass


class GeocoderRateLimitedError(GeocoderError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(math.ceil(retry_after)))
        super().__init__(f"Geokoder ograniczył ruch; spróbuj za {self.retry_after} s")


class GeocoderProviderError(GeocoderError):
    pass


class GeocoderHttpError(GeocoderProviderError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Geokoder zwrócił HTTP {status}")


class GeocoderCacheError(GeocoderError):
    pass


def validate_provider_url(value):
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Nieprawidłowy geocoding_provider_url")
    return text


class Geocoder:
    def __init__(
        self, session, cache_path, *, provider_url, user_agent,
        timeout_seconds=15, success_ttl_seconds=2_592_000,
        empty_ttl_seconds=86_400, max_entries=10_000, max_pending=20,
        min_interval_seconds=1.0, language="pl", clock=None,
        wall_clock=None, sleep=None,
        extra_params=None, max_response_bytes=None, persist_cooldown=False, shield_requests=True,
    ):
        self.session = session
        self.cache_path = Path(cache_path)
        self.provider_url = validate_provider_url(provider_url)
        self.user_agent = str(user_agent or "").strip()
        if not self.user_agent:
            raise ValueError("geocoding_user_agent nie może być pusty")
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.success_ttl_seconds = max(1, int(success_ttl_seconds))
        self.empty_ttl_seconds = max(1, int(empty_ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_pending = max(1, int(max_pending))
        self.min_interval_seconds = max(1.0, float(min_interval_seconds))
        self.language = str(language or "pl").strip() or "pl"
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.sleep = sleep or asyncio.sleep
        self.extra_params = {"accept-language": self.language} if extra_params is None else dict(extra_params)
        self.max_response_bytes = max_response_bytes
        self.persist_cooldown = persist_cooldown
        self.shield_requests = shield_requests
        self._guard = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._inflight = {}
        self._last_started = None
        self._blocked_until = 0.0
        self._closed = False

    async def start(self):
        await asyncio.to_thread(self._initialize_cache)
        if self.persist_cooldown:
            until = await asyncio.to_thread(self._read_cooldown)
            self._blocked_until = self.clock() + max(0, until - self.wall_clock())

    async def close(self):
        self._closed = True
        async with self._guard:
            tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _initialize_cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS geocoding_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    is_empty INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS geocoding_cache_created ON geocoding_cache(created_at)"
            )
            if self.persist_cooldown:
                connection.execute("CREATE TABLE IF NOT EXISTS geocoding_cooldown (provider TEXT PRIMARY KEY, until_time REAL NOT NULL)")
            connection.commit()

    def _read_cooldown(self):
        with closing(sqlite3.connect(self.cache_path)) as connection:
            row = connection.execute("SELECT until_time FROM geocoding_cooldown WHERE provider=?", (self.provider_url,)).fetchone()
            return float(row[0]) if row else 0

    def _write_cooldown(self, seconds):
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute("INSERT OR REPLACE INTO geocoding_cooldown VALUES (?, ?)", (self.provider_url, self.wall_clock() + seconds))
            connection.commit()

    def _key(self, endpoint, params):
        payload = json.dumps(
            {
                "provider": self.provider_url,
                "endpoint": endpoint,
                "language": self.language,
                "params": sorted((str(key), str(value)) for key, value in params.items()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache(self, cache_key):
        now = self.wall_clock()
        with closing(sqlite3.connect(self.cache_path)) as connection:
            row = connection.execute(
                "SELECT response_json, is_empty, expires_at, created_at FROM geocoding_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if float(row[2]) <= now:
                connection.execute("DELETE FROM geocoding_cache WHERE cache_key = ?", (cache_key,))
                connection.commit()
                return None
            return json.loads(row[0]), bool(row[1]), float(row[3])

    async def cached_at(self, endpoint, params):
        cached = await asyncio.to_thread(self._read_cache, self._key(endpoint, {**params, **self.extra_params}))
        return cached[2] if cached else None

    def _write_cache(self, cache_key, value, is_empty, ttl_override=None):
        now = self.wall_clock()
        ttl = self.empty_ttl_seconds if is_empty else self.success_ttl_seconds
        if ttl_override is not None and not is_empty:
            ttl = max(1, int(ttl_override))
        with closing(sqlite3.connect(self.cache_path)) as connection:
            connection.execute(
                """INSERT OR REPLACE INTO geocoding_cache
                   (cache_key, response_json, is_empty, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (cache_key, json.dumps(value, ensure_ascii=False), int(is_empty), now + ttl, now),
            )
            connection.execute("DELETE FROM geocoding_cache WHERE expires_at <= ?", (now,))
            count = connection.execute("SELECT COUNT(*) FROM geocoding_cache").fetchone()[0]
            excess = max(int(count) - self.max_entries, 0)
            if excess:
                connection.execute(
                    "DELETE FROM geocoding_cache WHERE cache_key IN (SELECT cache_key FROM geocoding_cache ORDER BY created_at ASC LIMIT ?)",
                    (int(excess),),
                )
            connection.commit()

    async def request_json(self, endpoint, params, *, empty=None, validator=None, cache_ttl=None):
        if self._closed:
            raise GeocoderError("Klient geokodera jest zamknięty")
        actual_params = {**params, **self.extra_params}
        cache_key = self._key(endpoint, actual_params)
        try:
            cached = await asyncio.to_thread(self._read_cache, cache_key)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
            raise GeocoderCacheError(f"Nie udało się odczytać pamięci podręcznej geokodera: {error}") from error
        if cached is not None:
            if validator is not None:
                validator(cached[0])
            return cached[0]

        async with self._guard:
            task = self._inflight.get(cache_key)
            if task is None:
                if len(self._inflight) >= self.max_pending:
                    raise GeocoderBusyError("Geokoder ma zbyt wiele oczekujących zapytań")
                task = asyncio.create_task(
                    self._fetch(cache_key, endpoint, actual_params, empty, validator, cache_ttl)
                )
                self._inflight[cache_key] = task
                task.add_done_callback(
                    lambda finished, key=cache_key: asyncio.create_task(self._forget(key, finished))
                )
        try:
            return await asyncio.shield(task) if self.shield_requests else await task
        finally:
            if task.done():
                await self._forget(cache_key, task)

    async def _forget(self, cache_key, task):
        async with self._guard:
            if self._inflight.get(cache_key) is task:
                self._inflight.pop(cache_key, None)

    async def _fetch(self, cache_key, endpoint, params, empty, validator, cache_ttl=None):
        try:
            cached = await asyncio.to_thread(self._read_cache, cache_key)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
            raise GeocoderCacheError(f"Nie udało się odczytać pamięci podręcznej geokodera: {error}") from error
        if cached is not None:
            if validator is not None:
                validator(cached[0])
            return cached[0]

        async with self._request_lock:
            now = self.clock()
            if now < self._blocked_until:
                raise GeocoderRateLimitedError(self._blocked_until - now)
            if self._last_started is not None:
                wait = self.min_interval_seconds - (now - self._last_started)
                if wait > 0:
                    await self.sleep(wait)
            self._last_started = self.clock()
            url = self.provider_url + "/" + endpoint.lstrip("/")
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    async with self.session.get(
                        url, params=params, headers={"User-Agent": self.user_agent},
                        allow_redirects=False,
                    ) as response:
                        if response.status == 429:
                            retry_after = self._retry_after(response.headers.get("Retry-After"))
                            self._blocked_until = max(self._blocked_until, self.clock() + retry_after)
                            if self.persist_cooldown:
                                await asyncio.to_thread(self._write_cooldown, retry_after)
                            raise GeocoderRateLimitedError(retry_after)
                        if response.status != 200:
                            raise GeocoderHttpError(response.status)
                        try:
                            if self.max_response_bytes is None:
                                value = await response.json(content_type=None)
                            else:
                                body = bytearray()
                                async for chunk in response.content.iter_chunked(65536):
                                    body.extend(chunk)
                                    if len(body) > self.max_response_bytes:
                                        raise GeocoderProviderError("Odpowiedź przekracza limit rozmiaru")
                                value = json.loads(body)
                        except (json.JSONDecodeError, UnicodeDecodeError, aiohttp.ContentTypeError) as error:
                            raise GeocoderProviderError("Geokoder zwrócił niepoprawny JSON") from error
            except asyncio.TimeoutError:
                raise
            except aiohttp.ClientError as error:
                raise GeocoderProviderError(f"Nie udało się połączyć z geokoderem: {type(error).__name__}") from error

        if validator is not None:
            try:
                validator(value)
            except GeocoderProviderError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise GeocoderProviderError("Geokoder zwrócił niepoprawną strukturę danych") from error
        is_empty = bool(empty(value)) if empty else value in (None, [], {})
        try:
            if cache_ttl is None:
                await asyncio.to_thread(self._write_cache, cache_key, value, is_empty)
            else:
                await asyncio.to_thread(self._write_cache, cache_key, value, is_empty, cache_ttl)
        except (OSError, sqlite3.Error) as error:
            raise GeocoderCacheError(f"Nie udało się zapisać pamięci podręcznej geokodera: {error}") from error
        return value

    def _retry_after(self, value):
        if value:
            try:
                return max(1.0, float(value))
            except ValueError:
                try:
                    return max(1.0, parsedate_to_datetime(value).timestamp() - self.wall_clock())
                except (TypeError, ValueError, OverflowError):
                    pass
        return 60.0
