"""Trusted local OCR components and a revisioned, crash-recoverable selection."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import TemporaryDirectory
from uuid import uuid4

from backend.storage.fsops import write_atomic_bytes
from backend.storage.paths import is_remote_volume
from .providers.ocr_models import OcrModelCatalog, REPO_CATALOG_PATH
from .types import RecognitionError


def local_path(path):
    """Refuse remote paths and every reparse component, including in-root junctions."""
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\") or ".." in path.parts or is_remote_volume(path):
        raise RecognitionError("INVALID_PARAMS", "须选择本机绝对路径", "directory")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RecognitionError("INVALID_PARAMS", "模型路径含重解析点", "directory")
    return path


class ModelLibrary:
    def __init__(self, metadata, factory, catalog=None):
        self.catalog = catalog or OcrModelCatalog.load(REPO_CATALOG_PATH)
        self.metadata = local_path(metadata)
        self.factory = local_path(factory)
        self.store = local_path(self.metadata / "library")
        self.pointer = local_path(self.store / "current.json")
        self.objects = local_path(self.store / "objects")
        self.selection = self._read()
        temporary = local_path(Path(str(self.pointer) + ".tmp"))
        temporary.unlink(missing_ok=True)
        # The host holds the data root's process-lifetime writer lock.
        self.collect(self.object_refs(self.selection))

    def components(self, model):
        engine = self.catalog.engine_for(model)
        if engine is None:
            raise RecognitionError("MODEL_UNAVAILABLE", "不支持的离线 OCR 模型", "model_id")
        return (engine.detection_model, engine.recognition_model)

    def object_id(self, component):
        entry = self.catalog.entry(component)
        files = [(item.name, item.bytes, item.sha256) for item in entry.files]
        return hashlib.sha256(json.dumps([component, files]).encode()).hexdigest()

    def _read(self):
        if not self.pointer.exists():
            declaration = Path(REPO_CATALOG_PATH).with_name("factory-models.json")
            if declaration.is_file():
                try:
                    media = json.loads(declaration.read_bytes())
                    selected = {engine["recognition_model"] for engine in media["engines"]}
                    if media["format"] != 1 or selected - set(self.catalog.recognition_models()):
                        raise ValueError("未知版本或管线")
                except (KeyError, TypeError, ValueError) as exc:
                    raise RecognitionError("DATA_CORRUPT", f"出厂模型清单损坏: {exc}") from exc
            else:
                selected = {model for model in self.catalog.recognition_models()
                            if any((self.factory / c).is_dir() for c in self.components(model))}
            return {"schema_version": 1, "revision": 0, "pipelines": {
                model: "factory" if model in selected else None
                for model in self.catalog.recognition_models()}}
        try:
            state = json.loads(self.pointer.read_bytes())
            selections = state["pipelines"]
            if (state["schema_version"] != 1 or type(state["revision"]) is not int or state["revision"] < 0
                    or set(selections) != set(self.catalog.recognition_models())
                    or not all(self.valid_source(model, value) for model, value in selections.items())):
                raise ValueError("目录内容或格式不兼容")
            return state
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RecognitionError("DATA_CORRUPT", f"模型库指针损坏: {exc}") from exc

    def valid_source(self, model, value):
        if value is None or value == "factory":
            return True
        return (isinstance(value, dict) and set(value) == set(self.components(model))
                and all(isinstance(name, str) and re.fullmatch(self.object_id(key) + r"(?:-[a-f0-9]{32})?", name)
                        for key, name in value.items()))

    def roots(self, model, source):
        return {key: str(local_path(self.factory if source == "factory" else self.objects / source[key]))
                for key in self.components(model)}

    def object_refs(self, selection):
        return {name for source in selection["pipelines"].values() if isinstance(source, dict)
                for name in source.values()}

    def collect(self, keep):
        if not self.objects.exists():
            return
        known = {self.object_id(c) for m in self.catalog.recognition_models() for c in self.components(m)}
        for path in self.objects.iterdir():
            if path.name not in keep and (path.name.split("-")[0] in known or path.name.startswith("stage-")):
                local_path(path)
                shutil.rmtree(path)

    def prepare(self, model, directory, cancel):
        source = local_path(directory)
        if not source.is_dir():
            raise RecognitionError("MODEL_UNAVAILABLE", "所选离线介质不可读", "directory")
        self.objects.mkdir(parents=True, exist_ok=True)
        refs = {}
        for component in self.components(model):
            digest = self.object_id(component)
            present = self.selection["pipelines"][model]
            current = present.get(component, digest) if isinstance(present, dict) else digest
            dest = local_path(self.objects / current)
            with TemporaryDirectory(prefix="stage-", dir=self.objects) as stage:
                payload = Path(stage) / "payload"
                out = payload / component
                out.mkdir(parents=True)
                for item in self.catalog.entry(component).files:
                    cancel.throw_if_cancelled()
                    original = local_path(source / component / item.name)
                    if not original.is_file() or original.stat().st_size != item.bytes:
                        raise RecognitionError("DATA_CORRUPT", f"缺少文件或长度不符: {component}/{item.name}")
                    result = hashlib.sha256()
                    remaining = item.bytes
                    with original.open("rb") as inp, (out / item.name).open("xb") as staged:
                        while remaining:
                            cancel.throw_if_cancelled()
                            data = inp.read(min(1 << 20, remaining))
                            if not data:
                                raise RecognitionError("DATA_CORRUPT", "复制期间模型文件变短")
                            staged.write(data)
                            result.update(data)
                            remaining -= len(data)
                        if inp.read(1) or result.hexdigest() != item.sha256:
                            raise RecognitionError("DATA_CORRUPT", f"模型文件哈希不符: {component}/{item.name}")
                        staged.flush()
                        os.fsync(staged.fileno())
                if dest.exists():
                    try:
                        self.catalog.verify(str(dest), (component,))
                    except RecognitionError:
                        # A previous reader can still own this physical object.
                        dest = self.objects / (digest + "-" + uuid4().hex)
                if not dest.exists():
                    payload.rename(dest)
                refs[component] = dest.name
        return refs

    def commit(self, model, source, revision):
        if self.selection["revision"] != revision:
            raise RecognitionError("CONFLICT", "模型库修订已改变，请重新预览", "expected_revision")
        if self.selection["pipelines"][model] == source:
            return
        next_state = {"schema_version": 1, "revision": revision + 1,
                      "pipelines": {**self.selection["pipelines"], model: source}}
        local_path(self.pointer)
        local_path(Path(str(self.pointer) + ".tmp"))
        write_atomic_bytes(str(self.pointer), json.dumps(next_state, sort_keys=True).encode())
        self.selection = next_state
