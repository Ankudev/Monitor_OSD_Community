import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import test_app

import app
from catalog_journal import (
    CatalogJournal,
    CatalogJournalError,
    operations,
    undo_preview,
)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog = self.root / "objects.json"
        self.database = self.root / "journal.sqlite3"
        self.before = [{"id": "a", "name": "przed"}]
        self.after = [{"id": "a", "name": "po"}]
        app.save_json(self.catalog, self.before)
        self.journal = CatalogJournal(self.database, self.catalog)

    def test_process_crash_before_replace_after_replace_and_before_commit(self):
        for phase in ("before_replace", "after_replace", "before_commit"):
            with self.subTest(phase=phase):
                app.save_json(self.catalog, self.before)
                self.database.unlink()
                script = """
import os, sys
from pathlib import Path
from catalog_journal import CatalogJournal
from app import save_json
root, phase = Path(sys.argv[1]), sys.argv[2]
j = CatalogJournal(root / 'journal.sqlite3', root / 'objects.json')
def writer(path, data):
    if phase == 'before_replace': os._exit(17)
    save_json(path, data)
    if phase == 'after_replace': os._exit(17)
if phase == 'before_commit': j.finish = lambda *args: os._exit(17)
j.write([{'id':'a','name':'przed'}], [{'id':'a','name':'po'}], 'update', 'a', 'test', writer)
"""
                result = subprocess.run([sys.executable, "-c", script, str(self.root), phase], cwd=Path(app.__file__).parent, check=False)
                self.assertEqual(result.returncode, 17)
                self.assertEqual(operations(self.database)[0]["status"], "prepared")
                CatalogJournal(self.database, self.catalog)
                expected = "aborted" if phase == "before_replace" else "committed"
                self.assertEqual(operations(self.database)[0]["status"], expected)
                self.assertEqual(json.loads(self.catalog.read_text(encoding="utf-8")), self.before if expected == "aborted" else self.after)

    def test_prepare_failure_never_calls_writer(self):
        with patch.object(self.journal, "connect", side_effect=sqlite3.OperationalError("disk")), patch("app.save_json") as writer:
            with self.assertRaises(sqlite3.Error):
                self.journal.write(self.before, self.after, "update", "a", "test", writer)
            writer.assert_not_called()
        self.assertEqual(json.loads(self.catalog.read_text(encoding="utf-8")), self.before)

    def test_write_failure_aborts_and_lost_ack_keeps_applied_catalogue(self):
        with self.assertRaises(OSError):
            self.journal.write(self.before, self.after, "update", "a", "test", lambda *_: (_ for _ in ()).throw(OSError("disk")))
        self.assertEqual(operations(self.database)[0]["status"], "aborted")
        def lost_ack(path, data):
            app.save_json(path, data)
            raise OSError("lost acknowledgement")
        result = self.journal.write(self.before, self.after, "update", "a", "test", lost_ack)
        self.assertEqual(result["audit_status"], "committed")
        self.assertEqual(json.loads(self.catalog.read_text(encoding="utf-8")), self.after)

    def test_commit_failure_recovers_without_rewriting(self):
        with patch.object(self.journal, "finish", side_effect=sqlite3.OperationalError("disk")):
            result = self.journal.write(self.before, self.after, "update", "a", "test", app.save_json)
        self.assertEqual(result["audit_status"], "prepared")
        raw = self.catalog.read_bytes()
        CatalogJournal(self.database, self.catalog)
        self.assertEqual(self.catalog.read_bytes(), raw)
        self.assertEqual(operations(self.database)[0]["status"], "committed")

    def test_external_change_conflict_never_overwrites(self):
        with patch.object(self.journal, "finish", side_effect=OSError("disk")):
            self.journal.write(self.before, self.after, "update", "a", "test", app.save_json)
        external = [{"id": "a", "name": "zewnętrzna"}]
        app.save_json(self.catalog, external)
        recovered = CatalogJournal(self.database, self.catalog)
        self.assertEqual(operations(self.database)[0]["status"], "conflict")
        with self.assertRaises(CatalogJournalError):
            recovered.write(external, self.after, "update", "a", "test", app.save_json)
        self.assertEqual(json.loads(self.catalog.read_text(encoding="utf-8")), external)

    def test_undo_preview_create_update_delete_and_later_conflict(self):
        for action, before, after in (("create", [], self.before), ("update", self.before, self.after), ("delete", self.before, [])):
            app.save_json(self.catalog, before)
            journal = CatalogJournal(self.database, self.catalog)
            result = journal.write(before, after, action, "a", "test", app.save_json)
            raw = self.catalog.read_bytes()
            preview = undo_preview(self.database, self.catalog, result["operation_id"])
            self.assertEqual(preview["objects"], before)
            self.assertTrue(preview["preview_only"])
            self.assertEqual(self.catalog.read_bytes(), raw)
            app.save_json(self.catalog, [{"id": "a", "name": "później"}])
            with self.assertRaises(ValueError):
                undo_preview(self.database, self.catalog, result["operation_id"])


class JournalApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_app.ApiTests(methodName="test_status_endpoint")
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.client = self.fixture.client
        self.application = self.fixture.app

    async def test_concurrent_updates_keep_order_memory_and_catalogue(self):
        responses = await asyncio.gather(*[
            self.client.patch("/api/admin/objects/0123456", json={"name": "N" + str(i)}, headers={"X-Change-Reason": "Test współbieżności"})
            for i in range(8)
        ])
        self.assertEqual([r.status for r in responses], [200] * 8)
        payloads = [await response.json() for response in responses]
        self.assertEqual(len({p["operation_id"] for p in payloads}), 8)
        journal = self.application[app.CATALOG_JOURNAL_KEY]
        rows = list(reversed(operations(journal.path)))
        for previous, following in pairwise(rows):
            self.assertEqual(previous["after_json"], following["before_json"])
            self.assertEqual(previous["after_hash"], following["before_hash"])
        self.assertTrue(all(r["status"] == "committed" and r["reason"] == "Test współbieżności" for r in rows))
        self.assertEqual(json.loads(journal.catalog.read_text(encoding="utf-8")), self.application[app.CONFIG_KEY]["teryt_locations"])

    async def test_pending_commit_response_updates_memory_and_bad_reason_does_not_write(self):
        journal = self.application[app.CATALOG_JOURNAL_KEY]
        with patch.object(journal, "finish", side_effect=sqlite3.OperationalError("disk")):
            response = await self.client.patch("/api/admin/objects/0123456", json={"name": "Zapisano"})
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["audit_status"], "prepared")
        self.assertEqual(self.application[app.CONFIG_KEY]["teryt_locations"][0]["name"], "Zapisano")
        response = await self.client.patch("/api/admin/objects/0123456", json={"name": "Nie zapisano"}, headers={"X-Change-Reason": "x" * 501})
        self.assertEqual(response.status, 400)
        self.assertEqual(len(operations(journal.path)), 1)

    async def test_slow_catalogue_write_does_not_block_public_reads(self):
        journal=self.application[app.CATALOG_JOURNAL_KEY]
        original=journal.write
        entered,release=threading.Event(),threading.Event()
        def slow(*args):
            entered.set()
            if not release.wait(5): raise OSError('Limit testowego oczekiwania')
            return original(*args)
        with patch.object(journal,'write',side_effect=slow):
            writing=asyncio.create_task(self.client.patch('/api/admin/objects/0123456',json={'name':'Nowa nazwa'}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                response=await asyncio.wait_for(self.client.get('/api/v1/objects'),2)
                self.assertEqual(response.status,200)
                self.assertNotEqual((await response.json())['items'][0]['name'],'Nowa nazwa')
                self.assertTrue(self.application[app.OBJECTS_LOCK_KEY].locked())
            finally:
                release.set()
                response=await writing
            self.assertEqual(response.status,200)
        self.assertEqual(self.application[app.CONFIG_KEY]['teryt_locations'][0]['name'],'Nowa nazwa')

    async def test_repeated_cancellation_keeps_lock_and_publishes_committed_catalogue(self):
        journal=self.application[app.CATALOG_JOURNAL_KEY]
        original=journal.write
        entered,release=threading.Event(),threading.Event()
        def slow(*args):
            entered.set()
            if not release.wait(5): raise OSError('Limit testowego oczekiwania')
            return original(*args)
        request=SimpleNamespace(app=self.application,match_info={'object_id':'0123456'},headers={},body={'name':'Zatwierdzona nazwa'})
        async def read_body(request): return request.body,None
        with patch.object(journal,'write',side_effect=slow), patch('app.read_object_body',side_effect=read_body):
            writing=asyncio.create_task(app.update_object_handler(request))
            waiting=None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                writing.cancel()
                await asyncio.sleep(0)
                writing.cancel()
                await asyncio.sleep(0)
                self.assertFalse(writing.done())
                self.assertTrue(self.application[app.OBJECTS_LOCK_KEY].locked())
                waiting=asyncio.create_task(self.application[app.OBJECTS_LOCK_KEY].acquire())
                await asyncio.sleep(0)
                self.assertFalse(waiting.done())
            finally:
                release.set()
                try: await writing
                except asyncio.CancelledError: pass
                if waiting is not None:
                    await asyncio.wait_for(waiting,3)
                    self.application[app.OBJECTS_LOCK_KEY].release()
        self.assertTrue(writing.cancelled())
        self.assertEqual(self.application[app.CONFIG_KEY]['teryt_locations'][0]['name'],'Zatwierdzona nazwa')
        self.assertEqual(json.loads(journal.catalog.read_text(encoding='utf8')),self.application[app.CONFIG_KEY]['teryt_locations'])
        self.assertEqual(operations(journal.path)[0]['status'],'committed')

    async def test_cancelled_failed_write_keeps_previous_catalogue_and_allows_next_edit(self):
        journal=self.application[app.CATALOG_JOURNAL_KEY]
        response=await self.client.patch('/api/admin/objects/0123456',json={'name':'Stan początkowy'})
        self.assertEqual(response.status,200)
        previous=journal.catalog.read_bytes()
        entered,release=threading.Event(),threading.Event()
        def fail(*args):
            entered.set()
            if not release.wait(5): raise OSError('Limit testowego oczekiwania')
            raise OSError('Testowa awaria zapisu')
        request=SimpleNamespace(app=self.application,match_info={'object_id':'0123456'},headers={})
        with patch.object(journal,'write',side_effect=fail), patch('app.read_object_body',return_value=({'name':'Nie zapisz'},None)):
            writing=asyncio.create_task(app.update_object_handler(request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,3))
                writing.cancel()
                await asyncio.sleep(0)
                self.assertTrue(self.application[app.OBJECTS_LOCK_KEY].locked())
            finally:
                release.set()
                try: await writing
                except asyncio.CancelledError: pass
        self.assertEqual(journal.catalog.read_bytes(),previous)
        self.assertFalse(self.application[app.OBJECTS_LOCK_KEY].locked())
        response=await self.client.patch('/api/admin/objects/0123456',json={'name':'Kolejna próba'})
        self.assertEqual(response.status,200)
