"""Verify the release interpreter, Python source hashes, and filenames recorded in startup caches."""
import argparse
import hashlib
import importlib.util
import json
import marshal
from pathlib import Path
import sys


def main(output):
    resources = Path(sys.executable).resolve().parents[1]
    manifest = (resources.parent / "package.json").read_bytes()
    files = {f["path"]: f for f in json.loads(manifest)["files"]}
    cache_paths = sorted(p for p in files if p.endswith(".pyc"))
    if not cache_paths:
        raise ValueError("No compiled startup modules in the installed release")
    checks = []
    for relative in cache_paths:
        cache = resources.parent / relative
        source = Path(importlib.util.source_from_cache(str(cache)))
        source_rel = source.relative_to(resources).as_posix()
        assert "resources/"+source_rel in files
        data = cache.read_bytes()
        assert data[:4] == importlib.util.MAGIC_NUMBER
        assert int.from_bytes(data[4:8], "little") == 1
        assert data[8:16] == importlib.util.source_hash(source.read_bytes())
        assert hashlib.sha256(data).hexdigest() == files[relative]["sha256"]
        assert marshal.loads(data[16:]).co_filename == source_rel
        checks.append(dict(source=source_rel, source_sha256=files["resources/"+source_rel]["sha256"],
                           compiled_sha256=files[relative]["sha256"], bytes=len(data)))
    assert any(row["source"].endswith("paddleocr/__init__.py") for row in checks)
    assert any(row["source"]=="backend/main.py" for row in checks)
    with output.open("x",encoding="utf8") as file:
        json.dump(dict(interpreter=sys.version,manifest=hashlib.sha256(manifest).hexdigest(),
                       files=checks,bytes=sum(row["bytes"] for row in checks)),file,indent=2)
    print(f"{len(checks)} compiled files match the staged interpreter, corresponding sources, and release manifest")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    main(parser.parse_args().output)
