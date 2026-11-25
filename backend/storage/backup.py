"""Portable, content-verified backups of a single JSON catalog generation."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from backend.image.formats import FORMAT_EXTENSIONS
from backend.scripts.domain import source_relative_path
from backend.storage import fsops, generation
from backend.storage.catalog import Catalog, CatalogSnapshot, _targets_from_document
from backend.storage.errors import (StorageError, conflict, data_corrupt, invalid_params,
                                    resource_limit, storage_error)
from backend.storage.lock import DataRootLock, engine_session
from backend.storage.paths import DataRoot
from backend.types.capabilities import TypeValidationError
from backend.types.conditions import validate_script

BACKUP_ID = re.compile(r"b-[a-f0-9]{32}")
MAX_TOTAL_BYTES = 64 * 1024**3
MAX_DOCUMENT = 256 * 1024**2
MAX_IMAGE = 4 * 40_000_000 + 1024**2
CURRENT = "catalog/current.json"


def _io(operation):
    """Normalize real filesystem failures at both the CLI and IPC boundaries."""
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except OSError as exc:
            raise storage_error(f"备份文件操作失败：{exc}") from exc
    return wrapped


def _hash(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def _file(root, relative):
    return Path(root.resolve(relative))


def _size(path, limit):
    if not path.is_file():
        raise data_corrupt(f"缺少文件：{path.name}")
    size = path.stat().st_size
    if size > limit:
        raise resource_limit(f"文件超出备份预算：{path.name}")
    return size


@dataclass
class Contents:
    snapshot: CatalogSnapshot
    pointer: str
    files: dict[str, Path]
    assets: dict[str, str]
    counts: dict


def verify_contents(root: DataRoot) -> Contents:
    catalog = Catalog(root)
    pointer = _file(root, CURRENT)
    _size(pointer, 65536)
    current = catalog.read_current()
    gen = current["generation"]
    if not gen or ntpath.isreserved(gen) or "/" in gen or "\\" in gen or gen in (".", ".."):
        raise data_corrupt("current 的 generation 编号非法")
    prefix = f"catalog/generations/{gen}/"
    files = {CURRENT: pointer}
    total = pointer.stat().st_size
    for name in (*generation.GENERATION_FILES, generation.MANIFEST_NAME):
        path = _file(root, prefix + name)
        total += _size(path, MAX_DOCUMENT)
        files[prefix + name] = path
    snapshot = catalog.load_snapshot()
    manifest = snapshot.manifest
    if (manifest["generation"] != gen or len(manifest["files"]) != len(generation.GENERATION_FILES)
            or set(snapshot.script_metadata) != {"schema_version", "scripts"}):
        raise data_corrupt("generation 与 manifest 或脚本索引不一致")
    scripts = snapshot.script_metadata["scripts"]
    if not isinstance(scripts, list):
        raise data_corrupt("脚本索引必须为数组")
    counts = dict(targets=len(snapshot.targets), groups=len(snapshot.groups),
                  scripts=len(scripts), next_sample_no=snapshot.next_sample_no)
    if counts != manifest["counts"]:
        raise data_corrupt("manifest 实体计数或下一样本号不符")
    assets = {}
    limits = {}
    for target in snapshot.targets:
        image = target["image"]
        rel = f"images/{image['sha256']}.{FORMAT_EXTENSIONS[image['format']]}"
        if rel != image["path"]:
            raise data_corrupt("图像路径与内容哈希不一致")
        assets[rel] = image["sha256"]
        limits[rel] = MAX_IMAGE
    seen = set()
    for document in scripts:
        try:
            script = validate_script(document)
        except TypeValidationError as exc:
            raise data_corrupt(str(exc)) from exc
        if script.id in seen or ntpath.isreserved(script.id) or script.source_path != source_relative_path(script.id, script.source_sha256):
            raise data_corrupt("脚本编号重复或源码路径不符")
        seen.add(script.id)
        assets[script.source_path] = script.source_sha256
        limits[script.source_path] = 1024**2
    for relative, expected in assets.items():
        path = _file(root, relative)
        total += _size(path, limits[relative])
        if total > MAX_TOTAL_BYTES:
            raise resource_limit("备份总字节超出预算")
        if _hash(path) != expected:
            raise data_corrupt(f"资产哈希不符：{relative}")
        files[relative] = path
    if total > MAX_TOTAL_BYTES:
        raise resource_limit("备份总字节超出预算")
    return Contents(snapshot, _hash(pointer), files, assets, counts)


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source, dst.open("xb") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
        target.flush()
        os.fsync(target.fileno())


class BackupService:
    def __init__(self, repository, idle=nullcontext):
        self.repo = repository
        self.root = repository.data_root
        self.idle = idle

    def _destination(self, ident):
        if not isinstance(ident, str) or not BACKUP_ID.fullmatch(ident):
            raise invalid_params("backup_id", "备份编号无效")
        return _file(self.root, f"backups/{ident}")

    def _source(self, ident):
        folder = self._destination(ident)
        if not folder.is_dir():
            raise invalid_params("backup_id", "找不到选定备份")
        return verify_contents(DataRoot(str(folder)))

    @_io
    def list(self):
        parent = _file(self.root, "backups")
        values = (p for p in parent.iterdir() if BACKUP_ID.fullmatch(p.name) and p.is_dir()) if parent.is_dir() else ()
        return {"backups": [p.name for p in sorted(values, key=lambda p: p.stat().st_mtime_ns, reverse=True)]}

    def _save(self, contents):
        ident = "b-" + uuid4().hex
        dst = self._destination(ident)
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dst.parent, prefix=".pending-") as tmp:
            staged = Path(tmp) / "backup"
            for name, source in contents.files.items():
                _copy(source, staged / name)
            verified = verify_contents(DataRoot(str(staged)))
            if verified.pointer != contents.pointer:
                raise conflict("备份期间目录发生变化")
            os.rename(staged, dst)
        return ident

    @_io
    def create(self):
        with self.idle(), DataRootLock(self.root.root):
            contents = verify_contents(self.root)
            ident = self._save(contents)
            return dict(backup_id=ident, generation=contents.snapshot.generation, counts=contents.counts)

    def _current(self):
        pointer = _file(self.root, CURRENT)
        if not pointer.exists():
            generations = _file(self.root, "catalog/generations")
            state = "damaged" if generations.is_dir() and any(generations.iterdir()) else "empty"
            return "missing", state, None
        _size(pointer, 65536)
        signature = _hash(pointer)
        try:
            contents = verify_contents(self.root)
        except StorageError as exc:
            if exc.code != "DATA_CORRUPT":
                raise
            return signature, "damaged", None
        return signature, "valid", contents

    def _recovery_watermark(self):
        """A damaged pointer may have been written after the selected backup."""
        folder = _file(self.root, "catalog/generations")
        if not folder.is_dir():
            raise data_corrupt("原目录缺少 generation，无法确认已分配编号；请将备份恢复到新的空目录")
        highest = 0
        for child in folder.iterdir():
            if not child.is_dir():
                continue
            prefix = f"catalog/generations/{child.name}/"
            manifest = _file(self.root, prefix + generation.MANIFEST_NAME)
            if manifest.is_file():
                _size(manifest, 65536)
                index = generation.parse_manifest(manifest.read_bytes())
                if index["generation"] != child.name or index["counts"]["next_sample_no"] < 1:
                    raise data_corrupt("保留的 generation 编号索引无效")
                highest = max(highest, index["counts"]["next_sample_no"])
            # An interrupted write can leave targets.json before the manifest.
            # Preserve its reservations as well, even when the numbers leave gaps.
            targets = _file(self.root, prefix + generation.TARGETS_NAME)
            if targets.is_file():
                _size(targets, MAX_DOCUMENT)
                doc = generation.decode_json(targets.read_bytes(), what="targets.json")
                records = _targets_from_document(doc)
                if any(not isinstance(row, dict) or type(row.get("sample_no")) is not int
                       or not 0 < row["sample_no"] < doc["next_sample_no"] for row in records):
                    raise data_corrupt("保留的样本与下一编号不一致")
                highest = max(highest, doc["next_sample_no"])
        if not highest:
            raise data_corrupt("已分配编号无法核实；保留原目录，请将备份恢复到新的空目录")
        return highest

    @_io
    def preview(self, backup_id):
        backup = self._source(backup_id)
        with DataRootLock(self.root.root):
            current, state, _ = self._current()
        return dict(backup_id=backup_id, generation=backup.snapshot.generation, counts=backup.counts,
                    expected_backup=backup.pointer, expected_current=current, current_state=state)

    @_io
    def restore(self, backup_id, expected_current, expected_backup):
        with self.idle(), DataRootLock(self.root.root):
            backup = self._source(backup_id)
            current, state, old = self._current()
            if (current, backup.pointer) != (expected_current, expected_backup):
                raise conflict("目录或备份已更改，请重新预览")
            watermark = backup.snapshot.next_sample_no
            if old:
                watermark = max(watermark, old.snapshot.next_sample_no)
            elif state == "damaged":
                watermark = max(watermark, self._recovery_watermark())
            protection = None
            if old:
                protection = self._save(old)
            elif state == "damaged" and current != "missing":
                protection = "damaged-" + uuid4().hex + ".json"
                _copy(_file(self.root, CURRENT), _file(self.root, "backups/" + protection))
            # Old generations and content-addressed assets remain available. Only
            # one atomic pointer replacement makes the fresh generation visible.
            for name, expected in backup.assets.items():
                dst = _file(self.root, name)
                if dst.exists():
                    if _hash(dst) != expected:
                        raise data_corrupt(f"已有同名资产内容不符，保留原件：{name}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=dst.parent) as tmp:
                    staged = Path(tmp) / "asset"
                    _copy(backup.files[name], staged)
                    if _hash(staged) != expected:
                        raise data_corrupt(f"恢复期间资产变化：{name}")
                    os.rename(staged, dst)
            gen = generation.new_generation_id()
            src = backup.snapshot
            docs = {
                generation.TARGETS_NAME: generation.encode_json(generation.targets_document(src.targets, watermark)),
                generation.GROUPS_NAME: generation.encode_json(generation.groups_document(src.groups)),
                generation.SCRIPT_METADATA_NAME: generation.encode_json(src.script_metadata),
            }
            counts = dict(backup.counts, next_sample_no=watermark)
            manifest = generation.build_manifest(gen, list(docs.items()), counts, generation_time())
            catalog = Catalog(self.root)
            catalog.write_generation(gen, docs)
            raw = generation.encode_manifest(manifest)
            catalog.write_manifest(gen, raw)
            catalog.verify_generation(gen, manifest)
            pointer = {"generation": gen, "manifest_sha256": generation.sha256_hex(raw)}
            catalog.activate(pointer)
            self.repo.reload()
            return dict(generation=gen, protection=protection)


def generation_time():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main():
    from backend.targets.repository import Repository
    parser = argparse.ArgumentParser(description="ScreenTrigger 目录备份与恢复")
    parser.add_argument("--data-dir", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    actions.add_parser("create")
    actions.add_parser("preview").add_argument("backup_id")
    restore = actions.add_parser("restore")
    restore.add_argument("backup_id")
    restore.add_argument("--expected-current", required=True)
    restore.add_argument("--expected-backup", required=True)
    args = vars(parser.parse_args())
    service = BackupService(Repository(args.pop("data_dir")))
    try:
        with engine_session(service.root.root):
            result = getattr(service, args.pop("action"))(**args)
    except StorageError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
