"""Trwały zapis zamiaru zmiany katalogu przez API; odzyskiwanie nie nadpisuje objects.json."""

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class CatalogJournalError(OSError):
    pass


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def catalog_fingerprint(path):
    return fingerprint(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def sync_catalog(path):
    if path.exists():
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class CatalogJournal:
    def __init__(self, path, catalog):
        self.path = Path(path)
        self.catalog = Path(catalog)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS catalog_operations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                action TEXT NOT NULL, object_id TEXT NOT NULL, reason TEXT NOT NULL,
                before_json TEXT, after_json TEXT, before_hash TEXT, after_hash TEXT NOT NULL,
                status TEXT NOT NULL, resolved_at TEXT
            )""")
        self.recover()
        self.expected_hash = catalog_fingerprint(self.catalog)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def finish(self, operation_id, status):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "UPDATE catalog_operations SET status=?, resolved_at=? WHERE operation_id=? AND status='prepared'",
                (status, datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def recover(self):
        current = catalog_fingerprint(self.catalog)
        with closing(self.connect()) as connection:
            pending = connection.execute("SELECT * FROM catalog_operations WHERE status='prepared' ORDER BY sequence").fetchall()
        for row in pending:
            status = "committed" if current == row["after_hash"] else (
                "aborted" if current == row["before_hash"] else "conflict"
            )
            if status == "committed":
                sync_catalog(self.catalog)
            self.finish(row["operation_id"], status)

    def write(self, before, after, action, object_id, reason, writer):
        """Wywołujący utrzymuje blokadę katalogu. Zachowaj zamiar przy utracie potwierdzenia."""
        self.recover()
        if catalog_fingerprint(self.catalog) != self.expected_hash:
            raise CatalogJournalError("Katalog zmieniono poza API; wymagany restart i przegląd dziennika")
        with closing(self.connect()) as connection:
            if connection.execute("SELECT 1 FROM catalog_operations WHERE status='conflict' LIMIT 1").fetchone():
                raise CatalogJournalError("Konflikt niedokończonej operacji; wymagany przegląd dziennika")
        operation_id = str(uuid.uuid4())
        previous = next((item for item in before if str(item["id"]) == object_id), None)
        updated = next((item for item in after if str(item["id"]) == object_id), None)
        after_hash = fingerprint(after)
        with closing(self.connect()) as connection, connection:
            connection.execute("""INSERT INTO catalog_operations
                (operation_id,created_at,action,object_id,reason,before_json,after_json,before_hash,after_hash,status)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared')""", (
                    operation_id, datetime.now(timezone.utc).isoformat(), action, object_id, reason,
                    json.dumps(previous, ensure_ascii=False), json.dumps(updated, ensure_ascii=False),
                    self.expected_hash, after_hash,
                ))
        try:
            writer(self.catalog, after)
        except OSError:
            # os.replace may have succeeded even when the writer lost its acknowledgement.
            if catalog_fingerprint(self.catalog) != after_hash:
                self.recover()
                raise
        if catalog_fingerprint(self.catalog) != after_hash:
            self.recover()
            raise CatalogJournalError("Fingerprint katalogu nie odpowiada zapisanej operacji")
        self.expected_hash = after_hash
        try:
            sync_catalog(self.catalog)
            self.finish(operation_id, "committed")
            status = "committed"
        except (OSError, sqlite3.Error):
            # Intent and catalogue are durable. Memory must follow the applied write.
            status = "prepared"
        return {"operation_id": operation_id, "audit_status": status}


def operations(path, limit=50):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM catalog_operations ORDER BY sequence DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def undo_preview(path, catalog, operation_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM catalog_operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None or row["status"] != "committed":
        raise ValueError("Podgląd wymaga zatwierdzonej operacji")
    current = json.loads(Path(catalog).read_text(encoding="utf-8"))
    item = next((item for item in current if str(item["id"]) == row["object_id"]), None)
    before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
    if item != after:
        raise ValueError("Obiekt zmieniono później; cofnięcie wymaga ręcznego rozstrzygnięcia")
    result = [before if str(item["id"]) == row["object_id"] else item for item in current
              if str(item["id"]) != row["object_id"] or before is not None]
    if before is not None and after is None:
        result.append(before)
    return {"operation_id": operation_id, "current_fingerprint": fingerprint(current),
            "proposed_fingerprint": fingerprint(result), "objects": result,
            "preview_only": True}
