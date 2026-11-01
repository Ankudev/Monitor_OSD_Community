"""Lekki pomiar zasobów Linux; przechowuje najwyżej 7 dni próbek co 15 minut."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_SAMPLES = 672
MAX_FILE_BYTES = 4 * 1024 * 1024


def counters(path):
    result = {}
    for line in path.read_text().splitlines():
        key, value, *_ = line.replace(':', '').split()
        result[key] = int(value)
    return result


def warnings(sample):
    result = []
    if sample['disk_available_bytes'] / sample['disk_total_bytes'] < .20:
        result.append('Mniej niż 20% wolnego miejsca na dysku.')
    if sample['memory_available_bytes'] / sample['memory_total_bytes'] < .15:
        result.append('Mniej niż 15% dostępnej pamięci RAM.')
    if sample['service_state'] != 'active':
        result.append('Usługa aplikacji nie jest aktywna.')
    return result


def collect(root, service):
    memory = counters(Path('/proc/meminfo'))
    values = subprocess.check_output(
        ['systemctl', 'show', service, '-p', 'MainPID', '-p', 'ActiveState',
         '-p', 'NRestarts', '-p', 'MemoryCurrent', '-p', 'CPUUsageNSec'],
        text=True, timeout=10,
    )
    unit = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
    pid = int(unit['MainPID'])
    usage = shutil.disk_usage(root)
    sample = {
        'measured_at': datetime.now(timezone.utc).isoformat(),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'pid': pid, 'service_state': unit['ActiveState'],
        'restarts': int(unit['NRestarts']),
        'memory_total_bytes': memory['MemTotal'] * 1024,
        'memory_available_bytes': memory['MemAvailable'] * 1024,
        'swap_used_bytes': (memory['SwapTotal'] - memory['SwapFree']) * 1024,
        'disk_total_bytes': usage.total, 'disk_available_bytes': usage.free,
        'disk_used_bytes': usage.used,
        'service_memory_bytes': int(unit['MemoryCurrent']) if unit['MemoryCurrent'].isdigit() else None,
        'service_cpu_nsec': int(unit['CPUUsageNSec']) if unit['CPUUsageNSec'].isdigit() else None,
        'rss_bytes': None, 'pss_bytes': None,
    }
    if pid:
        try:
            # Pierwsza linia smaps_rollup jest nagłówkiem mapowania.
            rows = Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines()[1:]
            stats = {parts[0].rstrip(':'): int(parts[1]) for parts in map(str.split, rows)}
            sample.update(rss_bytes=stats['Rss'] * 1024, pss_bytes=stats['Pss'] * 1024)
        except (FileNotFoundError, ProcessLookupError):
            # Proces mógł zakończyć się w trakcie pomiaru; kolejna próbka pokaże nowy PID.
            pass
    sample['warnings'] = warnings(sample)
    return sample


def save(path, sample):
    rows = []
    if path.exists():
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('Plik pomiarów przekroczył limit; wymagany przegląd.')
        rows = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('Niepoprawny plik pomiarów.')
    now = datetime.fromisoformat(sample['measured_at'])
    rows = [row for row in rows if 0 <= (now - datetime.fromisoformat(row['measured_at'])).total_seconds() <= 7 * 86400]
    rows = [*rows[-(MAX_SAMPLES - 1):], sample]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.resources-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
            json.dump(rows, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/monitor-osd-community'))
    parser.add_argument('--service', default='monitor-osd-community.service')
    parser.add_argument('--samples', type=Path, default=Path('/var/lib/pge-resources/samples.json'))
    args = parser.parse_args()
    sample = collect(args.root, args.service)
    save(args.samples, sample)
    print(json.dumps(sample, ensure_ascii=False))
    return int(bool(sample['warnings']))


if __name__ == '__main__':
    raise SystemExit(main())
