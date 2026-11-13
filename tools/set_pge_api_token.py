"""Zapisuje token PGE z ukrytego wejścia terminala; nie wywołuje API ani restartu."""
import getpass
import os
from pathlib import Path
import sys
import tempfile
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pge_credentials import PgeAccessError, private_directory, check_file, validate_token

TOKEN_PATH = Path('/etc/monitor-osd-community-live/secrets/pge-power-outage.token')


def save_token(path, value):
    validate_token(value)
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise PgeAccessError('PGE: nie można zastąpić dowiązania.')
    if path.exists():
        check_file(path.lstat())
    fd, temporary = tempfile.mkstemp(prefix='.pge-token-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value.encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if len(sys.argv) != 1 or os.name != 'posix' or os.geteuid() != 0 or not sys.stdin.isatty():
        raise SystemExit('Uruchom bez argumentów, jako administrator, w interaktywnym terminalu SSH.')
    try:
        private_directory(TOKEN_PATH.parent)
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = validate_token(getpass.getpass('Token PGE (niewidoczny): '))
            if value != getpass.getpass('Powtórz token PGE: '):
                raise PgeAccessError('Wpisane wartości różnią się. Nie zapisano tokenu.')
        save_token(TOKEN_PATH, value)
    except (PgeAccessError, OSError, getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise SystemExit('Nie udało się bezpiecznie zapisać tokenu. Sprawdź uprawnienia i ukryte wejście terminala.') from None
    print('Token zapisano. Usługa zacznie używać go po zaplanowanym restarcie.')


if __name__ == '__main__':
    main()
