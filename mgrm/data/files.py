"""File fingerprints, used to recognise a file loaded twice and to prove evidence is unchanged."""

import hashlib
from pathlib import Path

CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
