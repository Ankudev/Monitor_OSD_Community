"""Tworzy pustą, prywatną instalację lokalną bez dostępu do sieci."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from publication_policy import PublicationPolicy


def initialize(destination):
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('Wymagany pusty katalog nowej instalacji.')
    (root/'data').mkdir(parents=True)
    config = json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
    (root/'config.json').write_text(json.dumps(config, indent=2)+'\n', encoding='utf-8')
    (root/'objects.json').write_text('[]\n', encoding='utf-8')
    with closing(sqlite3.connect(root/'data/teryt.db')) as db:
        db.executescript('CREATE TABLE simc(sym TEXT PRIMARY KEY,nazwa TEXT,woj TEXT,pow TEXT,gmi TEXT);'
                         'CREATE TABLE ulic(sym_ul TEXT,sym TEXT,cecha TEXT,nazwa_1 TEXT,nazwa_2 TEXT);')
        db.commit()
    PublicationPolicy(root/'data/publication-policy.sqlite3').initialize()
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    try:
        print(initialize(parser.parse_args().data_dir))
    except ValueError as error:
        parser.exit(2, str(error)+'\n')
