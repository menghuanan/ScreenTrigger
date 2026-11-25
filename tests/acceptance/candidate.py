"""Load product code from the installed candidate and fixtures from the test suite."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def resources():
    folder = Path(sys.executable).resolve().parents[1]
    if not (folder / "weights").is_dir() or not (folder.parent / "package.json").is_file():
        raise ValueError("Run this check with the installed candidate's resources/python/python.exe")
    import backend.main
    if not Path(backend.main.__file__).resolve().is_relative_to(folder):
        raise ValueError("Unexpected backend import path")
    return folder


def pixel_fixtures():
    spec = importlib.util.spec_from_file_location("vision_fixtures", ROOT / "backend/tests/vision_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, obj):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
