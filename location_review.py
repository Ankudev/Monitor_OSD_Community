"""Atomowe raporty PRG, oddzielone od katalogu i historii wyłączeń."""

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

from address_matching import address_binding


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".review-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def expected_commune(database, obj):
    context_path = Path(database).parent / "geoportal-teryt-context.json"
    if context_path.exists():
        context = json.loads(context_path.read_text(encoding="utf-8"))
        value = context.get("codes", {}).get(obj["city_sym"])
        if isinstance(value, str) and len(value) == 7 and value.isdigit():
            return value
    if not Path(database).exists():
        return None
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM simc WHERE sym=?", (obj["city_sym"],)).fetchone()
        if row is None:
            return None
        if any(not isinstance(row[key], str) or not row[key].isdigit() or not 1 <= len(row[key]) <= 2 for key in ("woj", "pow", "gmi")):
            return None
        parts = "".join(row[key].zfill(2) for key in ("woj", "pow", "gmi"))
        return parts + str(row["rodz_gmi"]) if "rodz_gmi" in row.keys() and row["rodz_gmi"] else parts


class ReviewStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, object_id):
        return self.directory / "location-reviews" / (hashlib.sha256(str(object_id).encode()).hexdigest() + ".json")

    def save(self, report, evidence):
        for item in evidence:
            path = self.directory / "location-evidence" / (item["sha256"] + ".json")
            if not path.exists():
                body = json.dumps(item["body"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
                if hashlib.sha256(body).hexdigest() != item["sha256"]:
                    raise ValueError("Niezgodny hash dowodu PRG")
                atomic_json(path, item["body"])
        atomic_json(self.path(report["object_id"]), report)

    def read(self, obj):
        try:
            report = json.loads(self.path(obj["id"]).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"object_id": str(obj["id"]), "status": "not_checked", "current": False}
        result = dict(report)
        result.pop("binding", None)
        result["current"] = report.get("binding") == address_binding(obj) and report.get("version") == 1
        try:
            checked = datetime.fromisoformat(report["checked_at"])
            result["expired"] = (datetime.now(timezone.utc) - checked).total_seconds() > 2592000
        except (ValueError, TypeError, KeyError):
            result["expired"] = True
        if not result["current"]:
            result["status"] = "requires_review"
        return result

    def cached(self, obj, expected=None):
        result = self.read(obj)
        return result if (result.get("current") and not result.get("expired")
            and not result.get("truncated")
            and (expected is None or result.get("commune", {}).get("expected_code") == expected)) else None
