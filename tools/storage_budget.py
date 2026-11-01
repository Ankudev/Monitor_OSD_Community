"""Mierzy rozmiar danych i kopii oraz wolne miejsce. Opcjonalnie zapisuje próbki; nie usuwa plików."""
import argparse
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def inventory(root, backup_roots):
    groups = {"databases": 0, "wal": 0, "other_data": 0, "backups": 0}
    files = []
    seen = set()
    for folder, backup in [(root / "data", False), *((path, True) for path in backup_roots)]:
        if folder.is_symlink():
            continue
        candidates = [folder] if folder.is_file() else sorted(folder.rglob("*"))
        for path in candidates:
            if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents):
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            group = "backups" if backup else "wal" if path.name.endswith(("-wal", "-shm", "-journal")) else (
                "databases" if path.suffix in {".db", ".sqlite3"} else "other_data"
            )
            size = path.stat().st_size
            groups[group] += size
            files.append({"path": str(path), "bytes": size, "group": group})
    return groups, files


def evaluate(sample, previous=None, min_free_percent=20, horizon_days=30):
    percent = 100 * sample["free_bytes"] / sample["disk_bytes"]
    result = {"free_percent": round(percent, 2), "growth_bytes_per_day": None,
              "days_until_full": None, "warnings": []}
    if percent < min_free_percent:
        result["warnings"].append("free_space_below_threshold")
    if previous and previous.get("filesystem") == sample["filesystem"]:
        seconds = (datetime.fromisoformat(sample["measured_at"]) - datetime.fromisoformat(previous["measured_at"])).total_seconds()
        # Pomiar całego systemu plików obejmuje także przyrost danych poza projektem.
        if seconds >= 3600:
            rate = (previous["free_bytes"] - sample["free_bytes"]) * 86400 / seconds
            result["growth_bytes_per_day"] = round(rate, 2)
            result["project_growth_bytes_per_day"] = (
                round((sample["project_bytes"] - previous["project_bytes"]) * 86400 / seconds, 2)
                if sample.get("scope") == previous.get("scope") else None
            )
            if rate > 0:
                result["days_until_full"] = round(sample["free_bytes"] / rate, 2)
                if sample["free_bytes"] / rate <= horizon_days:
                    result["warnings"].append("forecast_within_horizon")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--backup-root", type=Path, action="append", default=[])
    parser.add_argument("--samples", type=Path, help="Dopisz próbkę JSONL i porównaj ją z najstarszą próbką sprzed co najmniej godziny")
    parser.add_argument("--min-free-percent", type=float, default=20)
    parser.add_argument("--horizon-days", type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.min_free_percent < 100 or not math.isfinite(args.horizon_days) or args.horizon_days <= 0:
        parser.error("progi: 0 < procent < 100, dni > 0")
    root = args.root.resolve()
    usage = shutil.disk_usage(root)
    groups, files = inventory(root, args.backup_root or [root / "snut-backups"])
    sample = {"measured_at": datetime.now(timezone.utc).isoformat(),
              "filesystem": str(root.anchor) + ":" + str(root.stat().st_dev),
              "scope": [str(root), *sorted(str(path.resolve()) for path in (args.backup_root or [root / "snut-backups"]))],
              "disk_bytes": usage.total, "free_bytes": usage.free, "project_bytes": sum(groups.values())}
    previous = None
    if args.samples and args.samples.exists():
        rows = [json.loads(line) for line in args.samples.read_text(encoding="utf-8").splitlines() if line.strip()]
        previous = next((row for row in rows if row.get("filesystem") == sample["filesystem"] and
                         (datetime.fromisoformat(sample["measured_at"]) - datetime.fromisoformat(row["measured_at"])).total_seconds() >= 3600), None)
    result = {**sample, "groups": groups, "files": files,
              "thresholds": {"min_free_percent": args.min_free_percent, "horizon_days": args.horizon_days},
              "forecast": evaluate(sample, previous, args.min_free_percent, args.horizon_days)}
    if args.samples:
        args.samples.parent.mkdir(parents=True, exist_ok=True)
        with args.samples.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(sample) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["forecast"]["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
