"""Buduje paczkę Community ze sprawdzonych plików Git i osobnego buildu SNUT."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run([sys.executable, str(ROOT/'tools/check_public_release.py')], cwd=ROOT, check=True)
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().strip('\0').split('\0')
    if not names or not (ROOT/'.deployment/snut-build/HOMER-LICENSE').is_file():
        raise ValueError('Wymagany indeks Git i zbudowany SNUT z licencją.')
    output = ROOT/'.deployment/community-0.1.0-alpha.tgz'
    files = {name: (ROOT/name).read_bytes().replace(b'\r\n', b'\n') if (ROOT/name).suffix not in {'.png', '.zip'} else (ROOT/name).read_bytes() for name in names}
    files.update({'snut/'+p.relative_to(ROOT/'.deployment/snut-build').as_posix(): p.read_bytes()
                  for p in (ROOT/'.deployment/snut-build').rglob('*') if p.is_file()})
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
    with tarfile.open(output, 'w:gz') as archive:
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(name); entry.size = len(data); entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    with tarfile.open(output) as archive:
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == digest
    print(output, hashlib.sha256(output.read_bytes()).hexdigest(), len(manifest), 'pliki')


if __name__ == '__main__':
    main()
