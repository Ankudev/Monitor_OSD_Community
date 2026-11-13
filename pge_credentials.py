"""Poświadczenie PGE przekazywane przez systemd, nigdy przez publiczną konfigurację."""
import os
from pathlib import Path
import stat

from scan_diagnostics import SourceContractError

CREDENTIAL_NAME = 'community-pge-power-outage-token'
TOKEN_LIMIT = 4096


class PgeAccessError(SourceContractError):
    def __init__(self, message, code='pge_access'):
        super().__init__(message, code=code)


def enabled(profile):
    if profile != 'live':
        return False
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:
        return False
    try:
        (Path(directory) / CREDENTIAL_NAME).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise PgeAccessError('PGE: nie można sprawdzić poświadczenia Community.') from None
    return True


def validate_token(value):
    # Dokumentacja nie określa formatu; dopuszczamy widoczne znaki ASCII nagłówka.
    if not isinstance(value, str) or not 1 <= len(value) <= TOKEN_LIMIT or any(
            not 33 <= ord(char) <= 126 for char in value):
        raise PgeAccessError('PGE: token jest pusty lub ma niepoprawny format.')
    return value


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise PgeAccessError('PGE: wymagany jest prywatny katalog bez dowiązań.')
    if os.name == 'posix':
        info = path.stat()
        if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o077:
            raise PgeAccessError('PGE: niebezpieczne uprawnienia katalogu.')
        for parent in path.parents:
            info = parent.stat()
            # /tmp dopuszczamy wyłącznie jako przodka prywatnego katalogu testowego.
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
                raise PgeAccessError('PGE: niebezpieczne uprawnienia ścieżki.')
    return path


def check_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PgeAccessError('PGE: poświadczenie musi być zwykłym plikiem bez dowiązań.')
    if os.name == 'posix' and (info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077):
        raise PgeAccessError('PGE: niebezpieczne uprawnienia pliku.')


def read_token():
    try:
        directory = private_directory(os.environ.get('CREDENTIALS_DIRECTORY', ''))
        path = directory / CREDENTIAL_NAME
        check_file(path.lstat())
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            check_file(os.fstat(stream.fileno()))
            value = stream.read(TOKEN_LIMIT + 1).decode('ascii')
        return validate_token(value)
    except PgeAccessError:
        raise
    except (OSError, ValueError):
        raise PgeAccessError('PGE: nie można bezpiecznie odczytać poświadczenia.') from None
