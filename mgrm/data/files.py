"""File fingerprints and safety checks, used to recognise a file loaded twice, to prove evidence is unchanged,
and to refuse a workbook built to exhaust the server before it is opened."""

import hashlib
import zipfile
from pathlib import Path

CHUNK = 1 << 20
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # a NetSuite export or a reconciliation workbook is well under this
MAX_WORKBOOK_UNPACKED_BYTES = 200 * 1024 * 1024  # an .xlsx is a zip; genuine ones unpack to a few MB


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def workbook_problem(path: Path) -> str | None:
    """Why this file must not be opened as an Excel workbook, or None.

    The XML inside is parsed with defusedxml (installed alongside openpyxl), which refuses entity tricks.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            unpacked = sum(item.file_size for item in archive.infolist())
    except (zipfile.BadZipFile, OSError):
        return "it is not an Excel workbook (.xlsx)"
    if unpacked > MAX_WORKBOOK_UNPACKED_BYTES:
        return "it unpacks to far more than any genuine workbook, so it was not opened"
    return None
