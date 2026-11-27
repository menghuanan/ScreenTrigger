"""Build the Windows x64 offline kit from a Release publish and verified media."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from portable_layout import CORE, MARKER, METADATA, UI
from portable_payload import prune_wheels, stage_ui, unused_cpython
from portable_pri import makepri_tool

ROOT = Path(__file__).resolve().parents[2]
PYTHON_SHA256 = "9ac15cfa6cab1115c83d48f2af55c554efa4d1bb044bbc4ab1c9d17ad426e16c"
RUNTIME = "10.0.15"
TOOLCHAIN = ROOT / ".venv/probe-toolchain"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def user_publish_files(ui):
    """Stage only the files emitted by the user WinUI project's Publish target."""
    inventory = ui / "ScreenTrigger.User.files"
    if not inventory.is_file():
        raise ValueError("请选择 ScreenTrigger.User 的 Offline 发布目录")
    allowed_pages = {p.relative_to(ROOT / "desktop/ScreenTrigger.User").with_suffix(".xbf").as_posix().lower()
                     for p in (ROOT / "desktop/ScreenTrigger.User").rglob("*.xaml")}
    allowed_pages.add("selection/overlaywindow.xbf")
    selected = {}
    for line in inventory.read_text(encoding="utf-8-sig").splitlines():
        relative, sha = line.rsplit("|", 1)
        path = Path(relative)
        if path.is_absolute() or path.drive or ".." in path.parts or not path.name:
            raise ValueError(f"Invalid user publish path: {relative}")
        if path.suffix.lower() == ".pdb":
            continue
        key = path.as_posix().lower()
        if ("devpanel" in key or "contracttests" in key or "selectionprobe" in key or
                (path.suffix.lower() == ".xbf" and key not in allowed_pages)):
            raise ValueError(f"Development UI in user publish: {relative}")
        if digest(ui / path) != sha.lower():
            raise ValueError(f"User publish file changed: {relative}")
        selected[key] = path
    # The Release XAML compiler embeds the pages in ScreenTrigger.pri.
    if not {"screentrigger.exe", "screentrigger.dll", "screentrigger.client.dll", "screentrigger.pri"} <= selected.keys():
        raise ValueError("Incomplete ScreenTrigger.User publish")
    return selected.values()


def prepare_engine(stage, args, pipelines=None):
    resources = stage / "resources"
    python = resources / "python"
    if digest(args.cpython) != PYTHON_SHA256:
        raise ValueError("CPython 3.13.14 x64 package hash mismatch")
    with zipfile.ZipFile(args.cpython) as archive:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if len(parts) < 2 or parts[0] != "tools" or info.is_dir():
                continue
            if parts[1].lower() in ("include", "libs", "scripts") or "site-packages" in parts:
                continue
            if pipelines is not None and unused_cpython(parts[1:]):
                continue
            destination = python.joinpath(*parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    lock = ROOT / "manifest/release-cp313-win-x64.txt"
    env = dict(os.environ, TMP=str(stage.parent), TEMP=str(stage.parent), TMPDIR=str(stage.parent))
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--no-index", "--no-cache-dir", "--no-deps", "--no-compile",
                    "--require-hashes", "--find-links", str(args.wheels), "-r", str(lock),
                    "--target", str(python / "Lib/site-packages")], check=True, env=env)
    # Paddle's C++ headers are used by its development SDK and exceed legacy
    # Windows path limits when installed under a per-user application directory.
    headers = python / "Lib/site-packages/paddle/include"
    if headers.is_dir():
        shutil.rmtree(headers)
    if pipelines is not None:
        prune_wheels(python / "Lib/site-packages")
    # _pth also controls multiprocessing children, even when launched without -I.
    (python / "python313._pth").write_text("Lib\nDLLs\n.\nLib/site-packages\n..\nimport site\n", encoding="ascii")
    for project in ("backend", "schema", "models"):
        shutil.copytree(ROOT / project, resources / project,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache"))
    for name in ("models.catalog.json", "release-cp313-win-x64.txt", "native-dev-toolchain.json"):
        copy(ROOT / "manifest" / name, resources / "manifest" / name)
    catalog = json.loads((ROOT / "manifest/models.catalog.json").read_text("utf-8"))
    engines = catalog["engines"]
    if pipelines is not None:
        engines = [p for p in engines if p["recognition_model"] in pipelines]
        if {p["recognition_model"] for p in engines} != set(pipelines):
            raise ValueError("Unknown portable OCR pipeline")
    required = {pair[key] for pair in engines for key in ("detection_model", "recognition_model")}
    for model in catalog["models"]:
        if model["model_id"] in required:
            for item in model["files"]:
                rel = Path(model["model_id"]) / item["name"]
                src = args.models / rel
                if src.stat().st_size != item["bytes"] or digest(src) != item["sha256"]:
                    raise ValueError(f"model hash mismatch: {rel}")
                copy(src, resources / "weights" / rel)
    ui_dir = stage if pipelines is None else stage / UI
    for dll in args.crt.glob("*.dll"):
        copy(dll, ui_dir / dll.name)
        copy(dll, python / dll.name)
    for name in ("msvcp140.dll", "vcruntime140_1.dll"):
        if not (ui_dir / name).is_file():
            raise ValueError(f"missing app-local x64 VC runtime: {name}")
    if pipelines is None:
        copy(args.crt.parent.parent / "vc_redist.x64.exe", resources / "licenses/VC/vc_redist.x64.exe")
    else:
        license_doc = ROOT / "tools/packaging/licenses/Visual-C-V14-License-Redistributable_and_Runtime_ENU.docx"
        copy(license_doc, resources / "licenses/VC" / license_doc.name)
    # Preserve wheel dist-info, model cards, .NET/NuGet and statically linked crate notices.
    components = json.loads((ROOT / "desktop/ScreenTrigger.User/packages.lock.json").read_text("utf-8-sig"))
    packages = next(iter(components["dependencies"].values()))
    for name, ver in [(n, v["resolved"]) for n, v in packages.items() if v["type"] != "Project"] + [("Microsoft.NETCore.App.Runtime.win-x64", RUNTIME)]:
        package = args.nuget / name.lower() / ver.lower()
        for file in package.iterdir():
            if file.is_file() and (file.name.lower().startswith(("license", "third", "notice")) or file.suffix == ".nuspec"):
                copy(file, resources / "licenses" / name / file.name)
    graph = subprocess.check_output(
        ["cargo", "metadata", "--format-version", "1", "--locked", "--offline",
         "--filter-platform", "x86_64-pc-windows-msvc", "--manifest-path", str(ROOT / "native/host/Cargo.toml")],
        env=dict(os.environ, CARGO_HOME=str(args.cargo)))
    for crate in json.loads(graph)["packages"]:
        if not crate["source"]:
            continue
        prefix = crate["name"] + "-" + crate["version"]
        directory = Path(crate["manifest_path"]).parent
        for file in directory.iterdir():
            if file.is_file() and file.name.lower().startswith(("license", "licence", "copying", "notice")):
                copy(file, resources / "licenses/rust" / prefix / file.name)
    copy(ROOT / "tools/packaging/inspect_root.py", resources / "inspect_root.py")
    if pipelines is None:
        copy(ROOT / "tools/packaging/Uninstall.ps1", stage / "Uninstall.ps1")
        copy(ROOT / "tools/packaging/SetupPaths.ps1", stage / "SetupPaths.ps1")
    else:
        copy(ROOT / "tools/packaging/portable_data.py", resources / "portable_data.py")
        copy(ROOT / "tools/packaging/portable_layout.py", resources / "portable_layout.py")
        copy(ROOT / "tools/packaging/便携版使用说明.txt", stage / "便携版使用说明.txt")
        factory = {"format": 1, "engines": engines,
                   "models": [m for m in catalog["models"] if m["model_id"] in required]}
        (resources / "manifest/factory-models.json").write_text(json.dumps(factory, ensure_ascii=False), encoding="utf8")
    build_data = stage.parent / "startup-data"
    subprocess.run([str(python / "python.exe"), "-I", "-B", "-X", "utf8",
                    str(ROOT / "tools/packaging/compile_startup.py"), str(resources), str(build_data),
                    *(["--portable"] if pipelines is not None else [])],
                   check=True, cwd=stage, stdin=subprocess.DEVNULL,
                   env=dict(env, ST_OCR_MODELS_DIR=str(resources / "weights"),
                            PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True",
                            PADDLE_PDX_CACHE_HOME=str(build_data / "paddlex")))
    if pipelines is not None:
        from portable_modules import collect
        collect(python)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ui", type=Path, default=ROOT / "dist/p3/ui")
    p.add_argument("--cpython", type=Path, default=TOOLCHAIN / "python.3.13.14.nupkg")
    p.add_argument("--wheels", type=Path, default=TOOLCHAIN / "wheels")
    p.add_argument("--models", type=Path, default=TOOLCHAIN / "models")
    p.add_argument("--nuget", type=Path, default=TOOLCHAIN / "nuget")
    p.add_argument("--cargo", type=Path, default=TOOLCHAIN / "cargo")
    p.add_argument("--crt", required=True, type=Path, help="x64/Microsoft.VC145.CRT from MSVC 14.52.37025 Redist")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--portable", action="store_true", help="Produce an automation-v2 extract-and-run kit")
    p.add_argument("--pipeline", action="append", help="Factory recognition model ID, repeatable; default is all")
    args = p.parse_args()
    if args.pipeline and not args.portable:
        p.error("--pipeline applies to portable builds")
    if sys.version_info[:3] != (3, 13, 14) or sys.maxsize < 2**32:
        p.error("a CPython 3.13.14 x64 build environment is required")
    config = json.loads((args.ui / "ScreenTrigger.runtimeconfig.json").read_text("utf-8"))
    frameworks = config["runtimeOptions"].get("includedFrameworks", [])
    if not any(f["name"] == "Microsoft.NETCore.App" and f["version"] == RUNTIME for f in frameworks):
        p.error("publish the Offline profile with .NET " + RUNTIME)
    for name in ("ScreenTrigger.exe", "screen_trigger_lib.dll", "recording-desktop-return.exe",
                 "coreclr.dll", "Microsoft.UI.Xaml.dll"):
        if not (args.ui / name).is_file():
            p.error("missing published file: " + name)
    out = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        p.error("use a new output filename to preserve the previous candidate")
    if args.portable:
        if out.parent != ROOT / "dist/automation-v2" or not out.name.startswith("ScreenTrigger-automation-v2-") or out.suffix != ".zip":
            p.error("portable builds require dist/automation-v2/ScreenTrigger-automation-v2-<candidate>.zip")
        sys.path.insert(0, str(ROOT / "tools"))
        from run_acceptance import baseline
        source_hash = baseline(ROOT)["sha256"]
        trusted = json.loads((ROOT / "manifest/models.catalog.json").read_bytes())
        selected = args.pipeline if args.pipeline is not None else trusted["recognition_models"]
        if len(set(selected)) != len(selected) or set(selected) - set(trusted["recognition_models"]):
            p.error("unknown or duplicate factory selection")
    with tempfile.TemporaryDirectory(dir=out.parent, prefix="build-") as tmp:
        stage = Path(tmp) / "app"
        ui_files = user_publish_files(args.ui)
        if args.portable:
            makepri = makepri_tool(args.nuget, ROOT / "desktop/ScreenTrigger.User/packages.lock.json")
            stage_ui(args.ui, ui_files, stage, makepri)
        else:
            for relative in ui_files:
                if relative.parts[0] != "portable":
                    copy(args.ui / relative, stage / relative)
        prepare_engine(stage, args, selected if args.portable else None)
        files = sorted(path for path in stage.rglob("*") if path.is_file())
        entries = [{"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size,
                    "sha256": digest(path)} for path in files]
        manifest = {"format": 1, "entry": "ScreenTrigger.exe", "runtime": RUNTIME,
                    "python_sha256": PYTHON_SHA256, "files": entries}
        raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        artifact = Path(tmp) / "kit.zip"
        with zipfile.ZipFile(artifact, "x", zipfile.ZIP_DEFLATED, compresslevel=2) as archive:
            if args.portable:
                core = [e for e in entries if not e["path"].startswith("resources/weights/")]
                inventory = json.dumps(core, separators=(",", ":")).encode()
                marker = dict(format=2, version="automation-v2", mode="portable", data="data",
                              source_sha256=source_hash, core_sha256=hashlib.sha256(inventory).hexdigest(),
                              factory=selected)
                archive.writestr((METADATA / MARKER).as_posix(), json.dumps(marker))
                archive.writestr((METADATA / CORE).as_posix(), inventory)
            else:
                archive.writestr("package.json", raw)
                archive.write(ROOT / "tools/packaging/Install.ps1", "Install.ps1")
            for path, entry in zip(files, entries):
                archive.write(path, entry["path"] if args.portable else "app/" + entry["path"])
        if args.portable and baseline(ROOT)["sha256"] != source_hash:
            raise ValueError("Portable build source changed before sealing the archive")
        os.rename(artifact, out)
    print(json.dumps({"path": str(out), "sha256": digest(out), "bytes": out.stat().st_size,
                      "installed_bytes": sum(x["bytes"] for x in entries), "files": len(entries)}))


if __name__ == "__main__":
    main()
