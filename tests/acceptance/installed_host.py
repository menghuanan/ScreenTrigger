"""Prepare a disposable ABI test launcher beside a byte-for-byte copy of a Windows installation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", type=Path)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    source = args.version.resolve(strict=True)
    target = args.fixture.resolve()
    if source == target or target.is_relative_to(source) or target.exists():
        parser.error("Use a new directory outside the installation")
    manifest = json.loads((source / "package.json").read_bytes())
    shutil.copytree(source, target)
    files = {item["path"]: item["sha256"] for item in manifest["files"]}
    required = ["ScreenTrigger.exe", "screen_trigger_lib.dll", "resources/python/python.exe"]
    if "recording-desktop-return.exe" in files:
        required.append("recording-desktop-return.exe")
    for name in required:
        expected = files[name]
        if digest(target / name) != expected:
            raise ValueError(f"Unexpected packaged binary {name}")
    python = target / "resources/python"
    launcher = target / "window-fixture.exe"
    shutil.copy2(python / "python.exe", launcher)
    shutil.copy2(python / "python3.dll", target / "python3.dll")
    shutil.copy2(python / "python313.dll", target / "python313.dll")
    # The release host resolves its sidecar relative to the running executable.
    # This test driver loads that same DLL in an ABI process owned by the fixture.
    (target / "python313._pth").write_text("\n".join([
        "resources/python/Lib", "resources/python/DLLs", "resources/python",
        "resources/python/Lib/site-packages", "resources",
        str(Path(__file__).resolve().parents[2] / "tools/probes/windows"), "import site", ""]), "utf8")
    (target / "data-root.txt").write_text(str(target.parent / (target.name+"-data")),"utf8")
    print(json.dumps(dict(launcher=str(launcher), manifest_sha256=digest(target / "package.json"),
                          dll_sha256=digest(target / "screen_trigger_lib.dll"))))


if __name__ == "__main__":
    main()
