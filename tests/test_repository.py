"""The repository holds code and documentation only, never company data or secrets."""

import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".parquet", ".json.gz", ".pdf", ".msg", ".eml"}
SECRET_NAMES = {".env"}
GIT_INSTALL_LOCATIONS = (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe")


def git_executable() -> str:
    found = shutil.which("git") or next((p for p in GIT_INSTALL_LOCATIONS if Path(p).exists()), None)
    assert found, "Git is not installed or not on PATH, so the repository cannot be checked"
    return found


def tracked_files() -> list[Path]:
    output = subprocess.run(
        [git_executable(), "ls-files", "--cached"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [Path(line) for line in output.splitlines() if line]


def test_repository_is_reachable():
    assert len(tracked_files()) > 20


def test_no_financial_data_files_are_tracked():
    assert [str(p) for p in tracked_files() if p.suffix.lower() in DATA_SUFFIXES] == []


def test_no_secrets_are_tracked():
    assert [str(p) for p in tracked_files() if p.name in SECRET_NAMES] == []
