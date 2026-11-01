import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import resource_monitor as monitor


class ResourceMonitorTests(unittest.TestCase):
    def test_available_memory_is_used_instead_of_free_or_cache(self):
        sample = {'disk_available_bytes': 30, 'disk_total_bytes': 100,
                  'memory_available_bytes': 80, 'memory_total_bytes': 100, 'service_state': 'active'}
        self.assertEqual(monitor.warnings(sample), [])
        sample.update(memory_available_bytes=14, disk_available_bytes=19, service_state='failed')
        self.assertEqual(len(monitor.warnings(sample)), 3)

    def test_samples_are_bounded_by_count_and_age(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'samples.json'
            now = datetime.now(timezone.utc)
            rows = [{'measured_at': (now - timedelta(seconds=n)).isoformat()} for n in range(800, 0, -1)]
            rows.insert(0, {'measured_at': (now - timedelta(days=8)).isoformat()})
            path.write_text(json.dumps(rows), encoding='utf8')
            sample = {'measured_at': now.isoformat(), 'pid': 123}
            monitor.save(path, sample)
            actual = json.loads(path.read_text(encoding='utf8'))
            self.assertEqual(len(actual), monitor.MAX_SAMPLES)
            self.assertEqual(actual[-1], sample)
            self.assertTrue(all(datetime.fromisoformat(row['measured_at']) > now - timedelta(days=7) for row in actual))

    def test_corrupt_history_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'samples.json'
            path.write_text('broken', encoding='utf8')
            with self.assertRaises(ValueError):
                monitor.save(path, {'measured_at': datetime.now(timezone.utc).isoformat()})
            self.assertEqual(path.read_text(), 'broken')

    def test_meminfo_parses_kib(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meminfo'
            path.write_text('MemTotal: 1310720 kB\nMemAvailable: 900000 kB\n')
            self.assertEqual(monitor.counters(path), {'MemTotal': 1310720, 'MemAvailable': 900000})
