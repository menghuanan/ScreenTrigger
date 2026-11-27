"""Paths shared by compact portable media, migration, and the archive verification tools."""
from pathlib import Path

UI = Path("resources/ui")
METADATA = Path("resources/manifest")
MARKER = "ScreenTrigger.portable.json"
CORE = "portable-core.json"
ENTRY = "ScreenTrigger.exe"
NATIVE_HOST = "screen_trigger_lib.dll"


def metadata_dir(app):
    """Also locate original flat portable kits when inspecting or migrating old media."""
    return app / METADATA if (app / METADATA / MARKER).is_file() or (app / UI).is_dir() else app


def host_path(app):
    return (app / UI if metadata_dir(app) != app else app) / NATIVE_HOST
