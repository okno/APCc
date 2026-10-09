#!/usr/bin/env python3
"""Build the Linux installer archive using an explicit, auditable file list."""
import hashlib
from pathlib import Path
import re
import tarfile


ROOT = Path(__file__).resolve().parents[1]
version = re.search(r'^VERSION = "([0-9.]+)"$', (ROOT / 'app.py').read_text(), re.M).group(1)
destination = ROOT / 'dist'
destination.mkdir(exist_ok=True)
archive_path = destination / f'apcc-{version}-linux.tar.gz'
files = [ROOT / name for name in ('app.py', 'install.sh', 'README.md', 'LICENSE', 'SECURITY.md')]
files += sorted((ROOT / 'static').glob('*'))
files += sorted((ROOT / 'docs').glob('*.md'))
files += sorted((ROOT / 'docs' / 'assets').glob('*'))
with tarfile.open(archive_path, 'w:gz') as archive:
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise SystemExit('Unexpected package member: ' + path.relative_to(ROOT).as_posix())
        info = archive.gettarinfo(str(path), 'apcc/' + path.relative_to(ROOT).as_posix())
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        info.mode = 0o755 if path.name == 'install.sh' else 0o644
        info.mtime = 0
        with path.open('rb') as stream:
            archive.addfile(info, stream)
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(destination / 'SHA256SUMS').write_text(digest + '  ' + archive_path.name + '\n')
print('Built ' + archive_path.relative_to(ROOT).as_posix())
print('SHA256 ' + digest)
