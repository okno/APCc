#!/usr/bin/env python3
"""Check publishable files without printing matched secrets.

An optional private deny list is a JSON string array stored OUTSIDE the repo.
It lets a maintainer check deployment-specific identifiers before publishing.
"""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git', '__pycache__', 'node_modules', '.venv', 'dist', '.pytest_cache'}
PRIVATE_NAMES = {'config.json', 'settings.json', 'auth.json', 'upsd.users',
                 'audit.jsonl', '.env', '.netrc', '.git-credentials', 'tls.key'}
PATTERNS = {
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'GitHub credential': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'local user path': rb'(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/mnt/[a-z]/Users/|/home/[^ /\n]+/)',
}


def files():
    for path in sorted(ROOT.rglob('*')):
        if any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file():
            yield path


def png_metadata(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    offset = 8
    while offset + 12 <= len(data):
        size = struct.unpack('>I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        if kind in (b'tEXt', b'zTXt', b'iTXt', b'eXIf'):
            return True
        offset += 12 + size
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deny-file', type=Path, help='private JSON string array outside this repository')
    args = parser.parse_args()
    deny = []
    if args.deny_file:
        resolved = args.deny_file.resolve()
        if ROOT == resolved or ROOT in resolved.parents:
            parser.error('The private deny list must remain outside the repository.')
        deny = json.loads(resolved.read_text())
        if not isinstance(deny, list) or not all(isinstance(item, str) and item for item in deny):
            parser.error('The deny list must be a JSON array of nonempty strings.')
    failures, count = [], 0
    for path in files():
        count += 1
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        if path.is_symlink() or path.name in PRIVATE_NAMES or path.suffix in ('.pem', '.key', '.sqlite3', '.pcap'):
            failures.append((name, 'private or unexpected file'))
        if png_metadata(data):
            failures.append((name, 'PNG contains text/EXIF metadata; review before publishing'))
        if path.suffix == '.png':
            continue
        for label, pattern in PATTERNS.items():
            if re.search(pattern, data):
                failures.append((name, label))
        lowered = data.lower()
        if any(item.lower().encode() in lowered for item in deny):
            failures.append((name, 'deployment-specific identifier'))
    if (ROOT / '.git').exists():
        result = subprocess.run(['git', 'log', '--format=%an <%ae>|%cn <%ce>'], cwd=ROOT,
                                text=True, capture_output=True)
        if result.returncode == 0 and any(item.lower() in result.stdout.lower() for item in deny):
            failures.append(('.git history', 'deployment-specific author identity'))
    if failures:
        for name, reason in failures:
            print(f'FAIL {name}: {reason}')
        raise SystemExit(1)
    print(f'PASS: {count} publishable files checked; no forbidden identifiers or private material found.')


if __name__ == '__main__':
    main()
