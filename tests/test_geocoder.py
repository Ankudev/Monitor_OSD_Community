import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from geocoder import (
    Geocoder,
    GeocoderBusyError,
    GeocoderCacheError,
    GeocoderProviderError,
    GeocoderRateLimitedError,
    validate_provider_url,
)


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def now(self):
        return self.value

    async def sleep(self, delay):
        self.value += delay
        await asyncio.sleep(0)


class FakeResponse:
    def __init__(self, session, status, payload, headers=None, gate=None, json_error=None):
        self.session = session
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.gate = gate
        self.json_error = json_error

    async def __aenter__(self):
        self.session.active += 1
        self.session.max_active = max(self.session.max_active, self.session.active)
        return self

    async def __aexit__(self, *_args):
        self.session.active -= 1

    async def json(self, content_type=None):
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.started.set()
        reply = self.replies.pop(0) if self.replies else (200, [{"lat": "52", "lon": "21"}], {}, None, None)
        status, payload, headers, gate, json_error = reply
        return FakeResponse(self, status, payload, headers, gate, json_error)


class GeocoderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name) / "cache.sqlite3"
        self.clock = FakeClock()

    async def asyncTearDown(self):
        self.directory.cleanup()

    def client(self, session, **kwargs):
        return Geocoder(
            session, self.cache, provider_url="https://geo.example.test",
            user_agent="Test/1.0 (+https://example.test)",
            min_interval_seconds=1, clock=self.clock.now,
            wall_clock=self.clock.now, sleep=self.clock.sleep, **kwargs,
        )

    async def test_search_and_reverse_share_limiter_and_are_serial(self):
        session = FakeSession([
            (200, [{"lat": "52", "lon": "21"}], {}, None, None),
            (200, {"address": {"city": "Test"}}, {}, None, None),
        ])
        client = self.client(session)
        await client.start()
        await asyncio.gather(
            client.request_json("search", {"q": "A"}),
            client.request_json("reverse", {"lat": "52", "lon": "21"}),
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.max_active, 1)
        self.assertGreaterEqual(self.clock.value, 1001.0)
        await client.close()

    async def test_identical_parallel_requests_share_one_call(self):
        gate = asyncio.Event()
        session = FakeSession([(200, [{"lat": "52", "lon": "21"}], {}, gate, None)])
        client = self.client(session)
        await client.start()
        tasks = [asyncio.create_task(client.request_json("search", {"q": "A"})) for _ in range(3)]
        try:
            # Czekamy na faktyczne żądanie, a nie na arbitralne 20 ms.
            await asyncio.wait_for(session.started.wait(), 5)
            self.assertEqual(len(session.calls), 1)
            gate.set()
            results = await asyncio.gather(*tasks)
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(session.calls), 1)
        finally:
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()

    async def test_cache_survives_restart_and_expires(self):
        session = FakeSession()
        first = self.client(session, success_ttl_seconds=30)
        await first.start()
        await first.request_json("search", {"q": "A"})
        await first.close()

        second = self.client(session, success_ttl_seconds=30)
        await second.start()
        await second.request_json("search", {"q": "A"})
        self.assertEqual(len(session.calls), 1)
        self.clock.value += 31
        await second.request_json("search", {"q": "A"})
        self.assertEqual(len(session.calls), 2)
        await second.close()

    async def test_empty_result_uses_short_ttl(self):
        session = FakeSession([(200, [], {}, None, None), (200, [], {}, None, None)])
        client = self.client(session, empty_ttl_seconds=5)
        await client.start()
        await client.request_json("search", {"q": "missing"}, empty=lambda value: not value)
        self.clock.value += 4
        await client.request_json("search", {"q": "missing"}, empty=lambda value: not value)
        self.assertEqual(len(session.calls), 1)
        self.clock.value += 2
        await client.request_json("search", {"q": "missing"}, empty=lambda value: not value)
        self.assertEqual(len(session.calls), 2)
        await client.close()

    async def test_cache_limit_evicts_oldest_entry(self):
        session = FakeSession()
        client = self.client(session, max_entries=2)
        await client.start()
        for query in ("A", "B", "C"):
            self.clock.value += 1
            await client.request_json("search", {"q": query})
        await client.request_json("search", {"q": "A"})
        self.assertEqual(len(session.calls), 4)
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM geocoding_cache").fetchone()[0], 2)
        await client.close()

    async def test_429_blocks_next_request_without_retry_loop(self):
        session = FakeSession([
            (429, {}, {"Retry-After": "5"}, None, None),
            (200, [{"lat": "52", "lon": "21"}], {}, None, None),
        ])
        client = self.client(session)
        await client.start()
        with self.assertRaisesRegex(GeocoderRateLimitedError, "5 s"):
            await client.request_json("search", {"q": "A"})
        with self.assertRaises(GeocoderRateLimitedError):
            await client.request_json("search", {"q": "B"})
        self.assertEqual(len(session.calls), 1)
        self.clock.value += 5
        await client.request_json("search", {"q": "B"})
        self.assertEqual(len(session.calls), 2)
        await client.close()

    async def test_pending_limit_and_cancelled_waiter_release_resources(self):
        gate = asyncio.Event()
        session = FakeSession([(200, [{"lat": "52", "lon": "21"}], {}, gate, None)])
        client = self.client(session, max_pending=1)
        await client.start()
        first = asyncio.create_task(client.request_json("search", {"q": "A"}))
        await asyncio.sleep(0.02)
        with self.assertRaises(GeocoderBusyError):
            await client.request_json("search", {"q": "B"})
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        joined = asyncio.create_task(client.request_json("search", {"q": "A"}))
        gate.set()
        self.assertTrue(await joined)
        await asyncio.sleep(0)
        self.assertEqual(session.active, 0)
        await client.close()

    async def test_timeout_invalid_json_and_cache_failure_are_distinct(self):
        gate = asyncio.Event()
        timeout_session = FakeSession([(200, {}, {}, gate, None)])
        timeout_client = self.client(timeout_session, timeout_seconds=0.01)
        await timeout_client.start()
        with self.assertRaises(asyncio.TimeoutError):
            await timeout_client.request_json("search", {"q": "timeout"})
        await timeout_client.close()

        bad_json = json.JSONDecodeError("bad", "x", 0)
        invalid_session = FakeSession([(200, {}, {}, None, bad_json)])
        invalid_client = self.client(invalid_session)
        await invalid_client.start()
        with self.assertRaises(GeocoderProviderError):
            await invalid_client.request_json("search", {"q": "json"})
        await invalid_client.close()

        shape_session = FakeSession([
            (200, {"unexpected": True}, {}, None, None),
            (200, [{"lat": "52", "lon": "21"}], {}, None, None),
        ])
        shape_client = self.client(shape_session)
        await shape_client.start()
        validator = lambda value: (
            None if isinstance(value, list)
            else (_ for _ in ()).throw(GeocoderProviderError("shape"))
        )
        with self.assertRaises(GeocoderProviderError):
            await shape_client.request_json("search", {"q": "shape"}, validator=validator)
        await shape_client.request_json("search", {"q": "shape"}, validator=validator)
        self.assertEqual(len(shape_session.calls), 2)
        await shape_client.close()

        cache_session = FakeSession()
        cache_client = self.client(cache_session)
        await cache_client.start()
        cache_client._write_cache = lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("disk"))
        with self.assertRaises(GeocoderCacheError):
            await cache_client.request_json("search", {"q": "cache"})
        await cache_client.close()

    def test_provider_url_is_admin_configuration(self):
        self.assertEqual(validate_provider_url("https://geo.example.test/"), "https://geo.example.test")
        for value in ("file:///tmp/x", "https://user:pass@example.test", "https://example.test?q=x"):
            with self.assertRaises(ValueError):
                validate_provider_url(value)
