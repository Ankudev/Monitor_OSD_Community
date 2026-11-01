"""Ustawia hash hasła bez ujawniania hasła w argumentach i logach."""
import argparse
from getpass import getpass
from pathlib import Path
import os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_auth import PH

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    password = getpass('Nowe hasło administratora: ')
    if not 15 <= len(password) <= 128 or password != getpass('Powtórz hasło: '):
        raise SystemExit('Hasła muszą być zgodne i mieć od 15 do 128 znaków.')
    args.path.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.path.with_suffix('.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as stream:
        stream.write(PH.hash(password)+'\n')
    if os.name == 'posix' and args.path.exists():
        previous = args.path.stat()
        os.chown(temporary, previous.st_uid, previous.st_gid)
        os.chmod(temporary, previous.st_mode & 0o777)
    os.replace(temporary, args.path)
    print('Hash hasła zapisany. Uruchom ponownie usługę, aby unieważnić sesje.')

if __name__ == '__main__':
    main()
