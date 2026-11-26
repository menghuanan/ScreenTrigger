"""Use CPython's standard ZIP importer for portable, file-independent libraries."""
from pathlib import Path
import zipfile


# These pure packages work through zipimport/importlib.resources. Paddle, OpenCV
# and other libraries that locate native code or data beside __file__ stay loose.
ZIP_PACKAGES = {"modelscope", "networkx", "baidubce", "tzdata", "huggingface_hub"}


def placement(relative):
    if relative.startswith("site-packages/"):
        name = relative.removeprefix("site-packages/")
        package = name.partition("/")[0]
        if package not in ZIP_PACKAGES and not package.endswith(".dist-info"):
            return None
        archive = "wheel-data.zip"
    else:
        name = relative
        # venv copies launcher templates and ensurepip invokes an embedded wheel.
        if name.partition("/")[0] in {"venv", "ensurepip", "sitecustomize.py"}:
            return None
        archive = "python313.zip"
    path = Path(name)
    if path.parent.name == "__pycache__" and path.suffix == ".pyc":
        # The packaged compile step produces CPython 3.13 level-zero bytecode.
        suffix = ".cpython-313.pyc"
        if not path.name.endswith(suffix):
            raise ValueError(f"Unexpected bytecode in the offline interpreter: {name}")
        name = (path.parent.parent / (path.name.removesuffix(suffix) + ".pyc")).as_posix()
    return archive, name


def collect(python):
    library = python / "Lib"
    # Store the standard library uncompressed so startup codecs work before zlib
    # is available. The outer distribution compresses both ZIPs for delivery.
    with zipfile.ZipFile(python / "python313.zip", "x") as stdlib, zipfile.ZipFile(
        python / "wheel-data.zip", "x", zipfile.ZIP_DEFLATED, compresslevel=1
    ) as wheels:
        targets = {"python313.zip": stdlib, "wheel-data.zip": wheels}
        written = set()
        former_parents = set()
        for source in sorted(library.rglob("*")):
            if source.is_file() and (entry := placement(source.relative_to(library).as_posix())):
                archive, name = entry
                if entry in written:
                    raise ValueError(f"Duplicate portable module path: {archive}/{name}")
                written.add(entry)
                targets[archive].write(source, name)
                source.unlink()
                parent = source.parent
                while parent != library and parent not in former_parents:
                    former_parents.add(parent)
                    parent = parent.parent
    # An empty .dist-info directory in the staging tree can shadow the archived
    # METADATA when a packager verifies the staged interpreter before zipping it.
    for directory in sorted(former_parents, key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    (python / "python313._pth").write_text(
        "python313.zip\nLib\nDLLs\n.\nLib/site-packages\nwheel-data.zip\n..\nimport site\n", encoding="ascii"
    )
