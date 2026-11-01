"""Sprawdza wyłącznie pliki przeznaczone do publikacji; nie wypisuje wartości danych."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ['em'+'itel', 'radio'+'polska', 'srv26-'+'30130', 'andrzej'+'130',
             'pge-'+'powiadomienia', 'monitor-osd'+'.bieda.it']


def check():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    errors = []
    for name in sorted(set(filter(None, names))):
        path = ROOT/name
        if not path.is_file():
            continue
        if path.suffix in {'.sqlite3', '.db', '.pem', '.key', '.zip'} or name in {'objects.json', 'config.json', 'state.json'}:
            errors.append((name, 'Niedozwolony plik danych'))
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            continue
        if name not in {'tools/check_public_release.py'} and any(value in text.casefold() for value in FORBIDDEN):
            errors.append((name, 'Pozostałość wykluczonej integracji lub wdrożenia'))
        if re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}', text):
            errors.append((name, 'Potencjalny sekret'))
    print(json.dumps({'files_checked': len(set(filter(None, names))), 'errors': errors}, ensure_ascii=False))
    return not errors


if __name__ == '__main__':
    raise SystemExit(0 if check() else 1)
