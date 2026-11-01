import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.storage_budget import evaluate, inventory


class StorageBudgetTests(unittest.TestCase):
    def test_low_space_and_growth_alarm_with_known_rate(self):
        now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        previous = {"measured_at": (now - timedelta(days=2)).isoformat(), "filesystem": "test", "free_bytes": 400, "project_bytes": 50}
        sample = {"measured_at": now.isoformat(), "filesystem": "test", "free_bytes": 100, "disk_bytes": 1000, "project_bytes": 350}
        report = evaluate(sample, previous)
        self.assertEqual(report["growth_bytes_per_day"], 150)
        self.assertEqual(report["project_growth_bytes_per_day"], 150)
        self.assertAlmostEqual(report["days_until_full"], 0.67)
        self.assertEqual(report["warnings"], ["free_space_below_threshold", "forecast_within_horizon"])
        sample["free_bytes"] = 500
        self.assertIsNone(evaluate(sample, previous)["days_until_full"])
        previous["filesystem"] = "other"
        self.assertIsNone(evaluate(sample, previous)["growth_bytes_per_day"])

    def test_short_sample_no_forecast_and_threshold_boundary(self):
        sample = {"measured_at": "2026-10-12T10:00:00+00:00", "filesystem": "test", "free_bytes": 200, "disk_bytes": 1000, "project_bytes": 0}
        previous = {**sample, "measured_at": "2026-10-12T09:59:59+00:00", "free_bytes": 900}
        self.assertIsNone(evaluate(sample, previous)["growth_bytes_per_day"])
        self.assertEqual(evaluate(sample)["warnings"], [])

    def test_inventory_counts_wal_and_backups_once_without_modifying_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            backup = root / "backups"
            backup.mkdir()
            paths = {root / "data/engine.sqlite3": 10, root / "data/engine.sqlite3-wal": 20,
                     root / "data/export.json": 30, backup / "copy.tgz": 40}
            for path, size in paths.items():
                path.write_bytes(b"x" * size)
            groups, files = inventory(root, [backup, backup, backup / "copy.tgz"])
            self.assertEqual(groups, {"databases": 10, "wal": 20, "other_data": 30, "backups": 40})
            self.assertEqual(len(files), 4)
            self.assertEqual({p: p.stat().st_size for p in paths}, paths)
