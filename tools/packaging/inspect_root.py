"""Read-only inspection of a legacy ScreenTrigger data root during installation."""
import argparse
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend.config.service import _validate_document
from backend.storage.backup import verify_contents
from backend.storage.generation import decode_json
from backend.storage.lock import engine_session, DataRootLock
from backend.storage.paths import DataRoot


def inspect(root):
    root = root.resolve()
    data = DataRoot(str(root))
    config = Path(data.resolve("config.json"))
    catalog = Path(data.resolve("catalog/current.json"))
    if not catalog.exists() and (root / "catalog/generations").is_dir() and any((root / "catalog/generations").iterdir()):
        raise ValueError(f"目录已有 generation，缺少 current 指针：{root}")
    if not config.exists() and not catalog.exists():
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"目录含有未识别的数据，无法确认覆盖范围：{root}")
        return {"path": str(root), "existing": False}
    if config.exists():
        _validate_document(decode_json(config.read_bytes(), what="config.json"), "DATA_CORRUPT")
    contents = verify_contents(data) if catalog.exists() else None
    return {"path": str(root), "existing": True,
            "generation": contents.snapshot.generation if contents else None,
            "targets": contents.counts["targets"] if contents else 0,
            "scripts": contents.counts["scripts"] if contents else 0}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--copy-to", type=Path)
    args = p.parse_args()
    if args.copy_to:
        root = args.root.resolve()
        destination = args.copy_to.resolve()
        if destination.parent != root.parent or destination.exists():
            p.error("the upgrade backup must be a new sibling directory")
        with engine_session(root), DataRootLock(str(root)):
            info = inspect(root)
            for child in root.rglob("*"):
                if child.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError(f"Linked data cannot be copied: {child}")
            with tempfile.TemporaryDirectory(dir=root.parent, prefix=".upgrade-") as tmp:
                staged = Path(tmp) / "snapshot"
                shutil.copytree(root, staged, ignore=shutil.ignore_patterns("writer.lock", "engine-session.lock"))
                checked = inspect(staged)
                if any(info[key] != checked[key] for key in ("generation", "targets", "scripts")):
                    raise ValueError("Upgrade backup mismatch")
                staged.rename(destination)
    print(json.dumps(inspect(args.root), ensure_ascii=True))
