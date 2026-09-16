"""Non-secret build identity for diagnostics and reproducible validation."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

VERSION = "3.2.1-rr-price-fix"
STRATEGY_VERSION = "ANEWME-V3"


@lru_cache(maxsize=1)
def build_identity():
    root = Path(__file__).resolve().parents[1]
    files = [root / 'config.py']
    for folder in ('core', 'broker', 'watch', 'storage', 'rules'):
        files.extend(p for p in (root / folder).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.txt', '.md'))
    manifest = {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                for p in sorted(files)}
    fingerprint = sha256(repr(sorted(manifest.items())).encode()).hexdigest()
    return {'version': VERSION, 'source_sha256': fingerprint, 'manifest': manifest}
