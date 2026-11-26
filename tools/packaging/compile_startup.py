"""Compile startup modules with the staged interpreter before hashing the offline kit."""
import importlib.util
from pathlib import Path
import py_compile
import sys

from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.vision.registry import ProviderRegistry
from backend.vision.ports import SessionPixelReader


def main(resources, data, portable=False):
    if Path(sys.executable).resolve() != resources / "python/python.exe":
        raise ValueError("Use the staged interpreter to produce compatible bytecode")
    args = SidecarArgs("build-offline-startup", data_dir=str(data))
    images = SessionImageStore(args.session_id)
    repository, _, _ = build_catalog_services(args, images)
    try:
        # Discover library imports in the interpreter collecting sys.modules.
        # At runtime these same factories execute in isolated provider processes.
        vision = ProviderRegistry(SessionPixelReader(images))
        try:
            vision.initialize_all(str(data / "models"))
            status, detail = vision.capability_reason("ocr")
            if status != "available" and not portable:
                raise RuntimeError(f"Offline OCR could not start: {detail}")
            sources = set((resources / "backend").rglob("*.py"))
            for module in tuple(sys.modules.values()):
                filename = getattr(module, "__file__", None)
                if filename:
                    path = Path(filename).resolve()
                    if path.is_relative_to(resources) and path.suffix == ".py" and path.is_file():
                        sources.add(path)
        finally:
            vision.close_all()
    finally:
        repository.close()

    for source in sorted(sources):
        cache = importlib.util.cache_from_source(str(source))
        # Installation verifies hashes of both files in the immutable version directory.
        py_compile.compile(str(source), cfile=cache, dfile=source.relative_to(resources).as_posix(),
                           doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    print(f"Compiled {len(sources)} startup modules with the staged Python", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(), "--portable" in sys.argv[3:])
