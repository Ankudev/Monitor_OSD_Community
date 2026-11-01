"""Odczytuje dziennik i pokazuje podgląd cofnięcia zmiany; nie stosuje korekt."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog_journal import operations, undo_preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--undo-preview", metavar="OPERATION_ID")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit: 1..1000")
    journal = args.root / "data/catalog-journal.sqlite3"
    try:
        result = undo_preview(journal, args.root / "objects.json", args.undo_preview) if args.undo_preview else operations(journal, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
