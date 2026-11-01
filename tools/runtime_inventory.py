"""Odczyt faktycznie używanego środowiska. Nie rozstrzyga obecności poprawek Ubuntu."""
import hashlib
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import platform
import sqlite3
import _sqlite3
import sys


def inventory():
    connection=sqlite3.connect(':memory:')
    try:
        source_id=connection.execute('SELECT sqlite_source_id()').fetchone()[0]
        options=[row[0] for row in connection.execute('PRAGMA compile_options')]
    finally:
        connection.close()
    dependencies={}
    for name in ('aiohttp','argon2-cffi','beautifulsoup4','tzdata'):
        try: dependencies[name]=version(name)
        except PackageNotFoundError: dependencies[name]=None
    driver=Path(_sqlite3.__file__)
    return dict(python=platform.python_version(),executable=sys.executable,
                platform=platform.platform(),sqlite=sqlite3.sqlite_version,
                sqlite_source_id=source_id,sqlite_compile_options=sorted(options),
                sqlite_driver=str(driver),sqlite_driver_sha256=hashlib.sha256(driver.read_bytes()).hexdigest(),
                dependencies=dependencies,wal_reset_patch_evidence='requires_vendor_or_build_evidence')


if __name__ == '__main__':
    print(json.dumps(inventory(),ensure_ascii=False,indent=2))
