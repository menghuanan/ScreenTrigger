"""Inspect explicit source roots or copy one verified root into a fresh portable kit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_root import inspect
from portable_layout import MARKER, metadata_dir
from backend.storage.lock import DataRootLock, engine_session
from backend.storage.paths import DataRoot
from backend.vision.model_library import ModelLibrary, local_path


def inventory(root):
    files = {}
    for path in root.rglob("*"):
        local_path(path)
        if path.is_file() and path.name not in {"engine-session.lock", "writer.lock", ".portable-layout.lock"}:
            with path.open("rb") as stream:
                files[path.relative_to(root)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return files


def migrate(source, app):
    source = local_path(Path(os.path.abspath(source)))
    app = local_path(Path(os.path.abspath(app)))
    marker = json.loads((metadata_dir(app)/MARKER).read_bytes())
    if marker.get("mode") != "portable" or marker.get("version") != "automation-v2" or marker.get("data") != "data":
        raise ValueError("目标不是受支持的便携解压目录")
    dest = local_path(app/"data")
    if dest.exists() or app.is_relative_to(source) or source.is_relative_to(app):
        raise ValueError("请选择另行完整解压、尚未创建 data 的目标目录，并保留原数据根")
    if not inspect(source)["existing"]:
        raise ValueError("未找到可验证的旧库")
    with engine_session(source), DataRootLock(str(source)), TemporaryDirectory(dir=app, prefix=".migrate-") as tmp:
        original = inspect(source)
        hashes = inventory(source)
        stage = Path(tmp)/"data"
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("engine-session.lock", "writer.lock", ".portable-layout.lock"))
        verified = inspect(stage)
        if (any(original.get(k) != verified.get(k) for k in ("generation","scripts","targets"))
                or hashes != inventory(stage)):
            raise ValueError("复制后的记录数、配置或资产哈希不同")
        config = stage/"config.json"
        model_directory = "models"
        if config.is_file():
            cfg=json.loads(config.read_bytes())
            model_directory=cfg.get("recognition",{}).get("model_directory", "models")
        model_path=Path(DataRoot(str(stage)).resolve(model_directory))
        library=ModelLibrary(model_path,app/"resources/weights")
        for model, selection in library.selection["pipelines"].items():
            if selection is not None:
                for component, weight_root in library.roots(model, selection).items():
                    library.catalog.verify(weight_root, (component,))
        # On Windows rename to an existing directory fails, including if another
        # first launch or migration wins after the initial dest check.
        stage.rename(dest)
    return {**verified,"path":str(dest),"files_verified":len(hashes),"source":str(source)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    select=parser.add_mutually_exclusive_group(required=True)
    select.add_argument("--inspect",type=Path,nargs="+",help="Show counts for explicit source directories")
    select.add_argument("--copy-from",type=Path,help="Copy this source to the unopened adjacent data/ directory")
    args=parser.parse_args()
    result=([inspect(local_path(Path(os.path.abspath(p)))) for p in args.inspect] if args.inspect
            else migrate(args.copy_from,Path(__file__).resolve().parents[1]))
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
