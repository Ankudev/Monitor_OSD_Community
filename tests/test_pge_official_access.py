"""Oficjalny dostęp PGE: wyłącznie fikcyjne sekrety i lokalny serwer testowy."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import traceback
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

import osd_sources
from pge_credentials import CREDENTIAL_NAME, PgeAccessError, read_token, enabled, validate_token
from pge_request_budget import RequestBudget
from tools.set_pge_api_token import save_token
from test_app import sample_event

TOKEN = 'FAKE-test-only-PGE-token-184729'


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.file = self.directory / CREDENTIAL_NAME
        self.environment = patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_atomic_save_read_and_no_extra_files(self):
        save_token(self.file, TOKEN)
        self.assertEqual(read_token(), TOKEN)
        save_token(self.file, TOKEN + '-rotated')
        self.assertEqual(read_token(), TOKEN + '-rotated')
        self.assertEqual(list(self.directory.iterdir()), [self.file])
        if os.name == 'posix':
            self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_invalid_values_preserve_old_file(self):
        save_token(self.file, TOKEN)
        for value in ('', 'a\nb', 'a\rb', 'a\x00b', ' space', 'tab\t', 'ż', 'x' * 4097):
            with self.subTest(value=repr(value[:12])):
                with self.assertRaises(PgeAccessError):
                    save_token(self.file, value)
                self.assertEqual(read_token(), TOKEN)

    def test_failed_replace_preserves_file_and_cleans_temporary(self):
        save_token(self.file, TOKEN)
        with patch('tools.set_pge_api_token.os.replace', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                save_token(self.file, 'new-fake-token')
        self.assertEqual(read_token(), TOKEN)
        self.assertEqual(list(self.directory.iterdir()), [self.file])

    def test_missing_and_oversized_file_are_safe(self):
        with self.assertRaises(PgeAccessError):
            read_token()
        save_token(self.file, TOKEN)
        self.file.write_bytes(b'x' * 4097)
        with self.assertRaises(PgeAccessError):
            read_token()

    def test_only_community_credential_enables_live_mode(self):
        self.assertFalse(enabled('live'))
        save_token(self.directory / 'pge-power-outage-token', TOKEN)
        self.assertFalse(enabled('live'))
        save_token(self.file, TOKEN)
        self.assertTrue(enabled('live'))
        self.assertFalse(enabled('demo'))
        self.assertFalse(enabled('local'))

    @unittest.skipUnless(os.name == 'posix', 'Prawa i dowiązania sprawdzamy na Linuksie.')
    def test_permissions_and_symlinks(self):
        save_token(self.file, TOKEN)
        self.file.chmod(0o644)
        with self.assertRaises(PgeAccessError):
            read_token()
        self.file.chmod(0o600)
        target = self.directory / 'target'
        self.file.rename(target)
        self.file.symlink_to(target)
        with self.assertRaises(PgeAccessError):
            read_token()
        with self.assertRaises(PgeAccessError):
            save_token(self.file, TOKEN)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.budget = RequestBudget(self.directory)

    def test_rolling_window_survives_new_instance_and_resume(self):
        with patch('pge_request_budget.time.time', return_value=10000):
            for _ in range(50):
                with self.budget.attempt():
                    pass
            restarted = RequestBudget(self.directory)
            restarted.resume()
            with self.assertRaisesRegex(PgeAccessError, 'limit'):
                with restarted.attempt():
                    self.fail('Nie wolno wysłać żądania.')
        with patch('pge_request_budget.time.time', return_value=13600):
            with self.budget.attempt():
                pass

    def test_auth_failure_blocks_across_restart_until_resume(self):
        with self.assertRaises(osd_sources.SourceHttpError):
            with self.budget.attempt():
                raise osd_sources.SourceHttpError('test', status=403)
        with self.assertRaisesRegex(PgeAccessError, 'Administrator'):
            with RequestBudget(self.directory).attempt():
                self.fail('Nie wolno wysłać żądania.')
        self.budget.resume()
        with self.budget.attempt():
            pass
        with closing(sqlite3.connect(self.directory / 'requests.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)

    def test_retry_after_and_clock_rollback_cannot_be_reset(self):
        from datetime import datetime, timezone
        with patch('pge_request_budget.time.time', return_value=10000):
            with self.assertRaises(osd_sources.SourceHttpError):
                with self.budget.attempt():
                    raise osd_sources.SourceHttpError('test', status=429,
                        retry_after=datetime.fromtimestamp(20000, timezone.utc))
            self.budget.resume()
        for now in (9999, 14000, 19999):
            with patch('pge_request_budget.time.time', return_value=now), self.assertRaises(PgeAccessError):
                with self.budget.attempt():
                    self.fail('Nie wolno wysłać żądania.')
        with patch('pge_request_budget.time.time', return_value=20000):
            with self.budget.attempt():
                pass

    def test_concurrent_clients_and_resume_cannot_enter(self):
        with self.budget.attempt():
            with self.assertRaises(PgeAccessError):
                with RequestBudget(self.directory).attempt():
                    self.fail('Równoległy odczyt jest zabroniony.')
            with self.assertRaises(PgeAccessError):
                RequestBudget(self.directory).resume()

    def test_crash_reservation_requires_manual_resume(self):
        with self.budget.connect() as db:
            self.budget.reserve(db, 10000)
        with self.assertRaisesRegex(PgeAccessError, 'Administrator'):
            with self.budget.attempt():
                self.fail('Pozostała nierozliczona próba.')

    def test_storage_failure_never_allows_request(self):
        with patch('pge_request_budget.sqlite3.connect', side_effect=sqlite3.OperationalError('test')):
            with self.assertRaises(PgeAccessError):
                with self.budget.attempt():
                    self.fail('Nie wolno wysłać żądania.')


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.credentials = self.directory / 'credentials'
        self.credentials.mkdir(mode=0o700)
        save_token(self.credentials / CREDENTIAL_NAME, TOKEN)
        self.budget = self.directory / 'budget'
        self.budget.mkdir(mode=0o700)
        env = patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': str(self.credentials),
            'OSD_COMMUNITY_PGE_STATE_DIR': str(self.budget)})
        env.start()
        self.addCleanup(env.stop)
        self.observed = []
        self.original_json = osd_sources._request_json

    async def run_response(self, handler):
        application = web.Application()
        application.router.add_get('/pge', handler)
        async def target(request):
            self.observed.append('redirect_target')
            return web.json_response([])
        application.router.add_get('/target', target)
        async with TestServer(application) as server:
            async def local_request(session, method, url, **kwargs):
                self.assertEqual(url, 'https://power-outage.gkpge.pl/api/power-outage')
                self.assertFalse(session.trust_env)
                self.assertIsInstance(session.cookie_jar, aiohttp.DummyCookieJar)
                return await self.original_json(session, method, str(server.make_url('/pge')), **kwargs)
            # Jedynie test zmienia cel na lokalny HTTP. Kod produkcyjny wymaga stałego HTTPS.
            with patch('osd_sources._request_json', side_effect=local_request):
                async with aiohttp.ClientSession(headers={'X-Test-Shared': 'must-not-leak'}) as shared:
                    result = await osd_sources.fetch_pge(shared, {'profile': 'live'})
                    self.assertNotIn('X-Auth-Token', shared.headers)
                    return result

    async def test_header_only_on_dedicated_request_and_contract_preserved(self):
        async def handler(request):
            self.observed.append(request.path)
            self.assertEqual(request.headers.get('X-Auth-Token'), TOKEN)
            self.assertEqual(request.headers.get('Content-Type'), 'application/json')
            self.assertNotIn('X-Test-Shared', request.headers)
            self.assertNotIn('types[]', request.query)
            self.assertIn('startAtTo', request.query)
            self.assertIn('stopAtFrom', request.query)
            return web.json_response([sample_event()])
        result = await self.run_response(handler)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['raw'], sample_event())
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.observed, ['/pge'])

    async def test_no_redirect_or_secret_in_traceback(self):
        async def handler(request):
            self.observed.append(request.path)
            return web.Response(status=302, headers={'Location': '/target?value=' + TOKEN})
        try:
            await self.run_response(handler)
        except osd_sources.SourceHttpError as error:
            self.assertEqual(error.status, 302)
            self.assertNotIn(TOKEN, traceback.format_exc())
        else:
            self.fail('Przekierowanie musi być odrzucone.')
        self.assertEqual(self.observed, ['/pge'])

    async def test_unauthorized_is_persistently_paused(self):
        async def handler(request):
            self.observed.append(request.path)
            return web.Response(status=401, text=TOKEN)
        with self.assertRaises(osd_sources.SourceHttpError):
            await self.run_response(handler)
        with self.assertRaisesRegex(PgeAccessError, 'Administrator'):
            await self.run_response(handler)
        self.assertEqual(self.observed, ['/pge'])

    async def test_reflected_token_rejected_before_normalization(self):
        async def handler(request):
            return web.json_response([sample_event(description=TOKEN)])
        with patch('osd_sources.normalize_pge') as normalize:
            with self.assertRaises(PgeAccessError) as error:
                await self.run_response(handler)
            normalize.assert_not_called()
            self.assertNotIn(TOKEN, str(error.exception))

    async def test_invalid_json_and_client_errors_do_not_disclose_token(self):
        async def handler(request):
            return web.Response(text=TOKEN)
        try:
            await self.run_response(handler)
        except PgeAccessError as error:
            self.assertEqual(error.code, 'invalid_json')
            self.assertNotIn(TOKEN, traceback.format_exc())
        else:
            self.fail('Niepoprawny JSON musi być odrzucony.')
        with patch('osd_sources._fetch_pge_response', side_effect=RuntimeError(TOKEN)):
            try:
                await osd_sources.fetch_pge(None, {'profile': 'live'})
            except PgeAccessError:
                self.assertNotIn(TOKEN, traceback.format_exc())
            else:
                self.fail('Błąd klienta musi być odrzucony.')

    async def test_diagnostics_do_not_store_reflected_header_or_secret_hash(self):
        import hashlib
        from scan_diagnostics import Diagnostics
        store = Diagnostics(self.directory / 'diagnostics.sqlite3')
        await store.start()
        async def handler(request):
            return web.Response(body=TOKEN.encode(), headers={'Content-Type': TOKEN})
        async with store.capture('pge', 'synthetic-token-echo') as run:
            try:
                await self.run_response(handler)
            except PgeAccessError as error:
                run.failed(error)
            else:
                self.fail('Odbite poświadczenie musi zostać odrzucone.')
        stored = json.dumps(await store.read())
        self.assertNotIn(TOKEN, stored)
        self.assertNotIn(hashlib.sha256(TOKEN.encode()).hexdigest(), stored)

    async def test_wrong_destination_and_invalid_secret_never_start_network(self):
        with patch('osd_sources.aiohttp.ClientSession') as client:
            with patch('osd_sources.PGE_URL', 'https://attacker.invalid/'):
                with self.assertRaises(PgeAccessError):
                    await osd_sources.fetch_pge(None, {'profile': 'live'})
            (self.credentials / CREDENTIAL_NAME).write_bytes(b'')
            with self.assertRaises(PgeAccessError):
                await osd_sources.fetch_pge(None, {'profile': 'live'})
            client.assert_not_called()

    async def test_live_without_credential_uses_anonymous_request(self):
        from unittest.mock import AsyncMock
        (self.credentials / CREDENTIAL_NAME).unlink()
        with patch('osd_sources._request_json', new_callable=AsyncMock, return_value=[]) as request:
            self.assertEqual(await osd_sources.fetch_pge(None, {'profile': 'live'}), [])
            self.assertNotIn('headers', request.call_args.kwargs)

    async def test_demo_never_starts_pge_request_even_with_credential(self):
        from scan_diagnostics import SourceContractError
        with patch('osd_sources._request_json') as request:
            with self.assertRaises(SourceContractError):
                await osd_sources.fetch_pge(None, {'profile': 'demo'})
            request.assert_not_called()

    async def test_connection_failure_does_not_retry(self):
        async def handler(request):
            self.observed.append(request.path)
            request.transport.close()
            return web.Response(text='[]')
        with self.assertRaises(PgeAccessError) as failure:
            await self.run_response(handler)
        self.assertEqual(failure.exception.code, 'client_retry_blocked')
        self.assertEqual(self.observed, ['/pge'])

    async def test_timeout_is_classified_without_exception_details(self):
        with patch('osd_sources._fetch_pge_response', side_effect=TimeoutError(TOKEN)):
            try:
                await osd_sources.fetch_pge(None, {'profile': 'live'})
            except PgeAccessError as error:
                self.assertEqual(error.code, 'timeout')
                self.assertNotIn(TOKEN, traceback.format_exc())
            else:
                self.fail('Przekroczenie czasu oczekiwania musi zostać zgłoszone.')

    def test_echo_detection_handles_escaped_values_and_keys(self):
        token = validate_token('fake-"\\token')
        for value in ([{'description': token}], {token: 'value'}):
            encoded = json.dumps(value)
            self.assertTrue(osd_sources._contains_secret(json.loads(encoded), token))


if __name__ == '__main__':
    unittest.main()
