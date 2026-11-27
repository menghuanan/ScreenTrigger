"""File-level exclusions for the Chinese desktop product with offline OCR and Lua 5.4."""
import json
from pathlib import Path
import shutil

from portable_layout import ENTRY, UI

# These features have no entry in ScreenTrigger.User. Framework, networking, text
# editing, accessibility, native recording, and system-browser activation stay intact.
UNUSED_UI = {
    "createdump.exe", "mscordbi.dll", "microsoft.diasymreader.native.amd64.dll",
    "microsoft.ui.designer.dll", "restartagent.exe",
    "microsoft.web.webview2.core.dll", "microsoft.web.webview2.core.projection.dll", "webview2loader.dll",
    "microsoft.security.authentication.oauth.projection.dll",
    "microsoft.windows.applicationmodel.background.projection.dll",
    "microsoft.windows.applicationmodel.background.universalbgtask.dll",
    "microsoft.windows.appnotifications.projection.dll", "microsoft.windows.appnotifications.builder.projection.dll",
    "microsoft.windows.badgenotifications.projection.dll", "microsoft.windows.pushnotifications.projection.dll",
    "pushnotificationslongrunningtask.proxystub.dll",
    "microsoft.ui.xaml/assets/map.html",
}
# Explicitly retain numpy.testing and pandas._testing: they expose runtime imports.
VENDOR_TEST_PACKAGES = {"pandas", "numpy", "shapely", "fsspec", "opt_einsum", "colorama", "future", "certifi"}
SDK_EXTENSIONS = {".lib", ".exp", ".pdb", ".pyi", ".h", ".hpp"}
STDLIB_TOOLS = {"ensurepip", "idlelib", "test", "tkinter", "turtledemo", "venv"}


def unused_ui(relative):
    name = relative.as_posix().lower()
    if relative.suffix.lower() == ".mui":
        return len(relative.parts) == 2 and relative.parts[0].lower() != "zh-cn"
    # Compiled C#/WinRT projections and the embedded activation manifest implement
    # the desktop interfaces. WinMDs describe those interfaces for development tools.
    return (relative.suffix.lower() == ".winmd" or name in UNUSED_UI
            or (name.startswith("mscordaccore") and name.endswith(".dll")))


def stage_ui(publish, files, stage):
    files = set(files)
    portable_entry = Path("portable") / ENTRY
    if portable_entry not in files:
        raise ValueError("需要使用便携配置发布的 ScreenTrigger.User 启动程序")
    for source in sorted(files):
        if source == Path(ENTRY) or unused_ui(source):
            continue
        dest = stage / (ENTRY if source == portable_entry else UI / source)
        if source.name == "ScreenTrigger.pri":
            # MRTCore's unpackaged base-directory mode looks for resources.pri.
            dest = stage / UI / "resources.pri"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.name == "ScreenTrigger.deps.json":
            deps = json.loads((publish / source).read_bytes())
            for target in deps["targets"].values():
                for library in target.values():
                    for kind in ("runtime", "native", "runtimeTargets"):
                        if kind in library:
                            library[kind] = {key: value for key, value in library[kind].items()
                                             if not unused_ui(Path(Path(key).name))}
            dest.write_text(json.dumps(deps, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        else:
            shutil.copyfile(publish / source, dest)


def unused_cpython(parts):
    return (parts[0].lower() == "pythonw.exe"
            or (len(parts) > 1 and parts[0].lower() == "lib" and parts[1].lower() in STDLIB_TOOLS))


def prune_wheels(site):
    """Only operates on pip's private staging directory, before bytecode and hashes."""
    for parent, directories, filenames in site.walk():
        relative = parent.relative_to(site).parts
        for name in list(directories):
            if ((not relative and name in {"bin", "libfuturize", "libpasteurize", "pypdfium2_cli", "ujson-stubs"})
                    or (name == "tests" and relative and relative[0] in VENDOR_TEST_PACKAGES)
                    or (relative == ("Crypto",) and name == "SelfTest")):
                shutil.rmtree(parent / name)
                directories.remove(name)
        for name in filenames:
            file = parent / name
            if (file.suffix.lower() in SDK_EXTENSIONS
                    or (relative == ("lupa",) and file.suffix == ".pyd" and not name.startswith("lua54."))):
                file.unlink()
