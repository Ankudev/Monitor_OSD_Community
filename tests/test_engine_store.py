from policy_helpers import create_test_app
import asyncio
import json
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

from engine_store import (
    EngineStore,
    EngineStoreCommittedError,
    EngineStoreError,
    backup_database,
    inspect_database,
    restore_database,
)


def state(saved_at="2026-09-30T10:00:00+00:00", marker="pierwszy"):
    return {
        "saved_at": saved_at,
        "events": {marker: {"event_key": marker, "source": "pge"}},
        "history": [],
        "sources": {
            "pge": {
                "last_attempt_at": saved_at,
                "blocked_until": "2026-09-30T11:00:00+00:00",
            }
        },
    }


class EngineStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.database = self.base / "data" / "engine.sqlite3"
        self.export = self.base / "state.json"
        self.store = EngineStore(self.database, self.export)

    async def asyncTearDown(self):
        await self.store.close()
        self.temporary.cleanup()









    async def test_missing_database_backup_fails_without_creating_source(self):
        missing = self.base / 'missing.sqlite3'
        destination = self.base / 'backup.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            backup_database(missing, destination)
        self.assertFalse(missing.exists())
        self.assertFalse(destination.exists())
        reader = EngineStore(missing, self.base / 'missing-export.json')
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.export_current()
            self.assertFalse(missing.exists())
        finally:
            await reader.close()







    async def test_first_migration_rejects_invalid_existing_json(self):
        import app
        config = self.base / 'config.json'
        config.write_text('{"teryt_locations": []}', encoding='utf-8')
        self.export.write_text('{"events": []}', encoding='utf-8')
        with self.assertRaisesRegex(EngineStoreError, 'migracja przerwana'):
            create_test_app(config, self.export, start_scanner=False)
        self.assertEqual(json.loads(self.export.read_text(encoding='utf-8')), {'events': []})
