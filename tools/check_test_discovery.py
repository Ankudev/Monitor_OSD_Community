"""Sprawdza ID testów unittest bez uruchamiania metod testowych."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Opcjonalny raport JSON')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    counts = Counter(test_ids(loader.discover(str(ROOT / 'tests'))))
    result = dict(executions=sum(counts.values()), unique_ids=len(counts),
                  duplicates={key: value for key, value in counts.items() if value > 1},
                  loader_errors=loader.errors, ids=sorted(counts))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'ids'}, ensure_ascii=False))
    return 1 if result['duplicates'] or loader.errors or not counts else 0


if __name__ == '__main__':
    raise SystemExit(main())
