"""Compile startup modules with the staged interpreter before hashing the offline kit."""
import importlib.util
from pathlib import Path
import py_compile
import sys

from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.vision.registry import build_default_vision_service


def main(resources, data):
    if Path(sys.executable).resolve() != resources / "python/python.exe":
        raise ValueError("Use the staged interpreter to produce compatible bytecode")
    args = SidecarArgs("build-offline-startup", data_dir=str(data))
    images = SessionImageStore(args.session_id)
    repository, _, _ = build_catalog_services(args, images)
    try:
        vision = build_default_vision_service(args, image_store=images, repository=repository)
        try:
            status, detail = vision.capability_state("ocr")
            if status != "available":
                raise RuntimeError(f"Offline OCR could not start: {detail}")
            sources = set((resources / "backend").rglob("*.py"))
            for module in tuple(sys.modules.values()):
                filename = getattr(module, "__file__", None)
                if filename:
                    path = Path(filename).resolve()
                    if path.is_relative_to(resources) and path.suffix == ".py" and path.is_file():
                        sources.add(path)
        finally:
            vision.close()
    finally:
        repository.close()

    for source in sorted(sources):
        cache = importlib.util.cache_from_source(str(source))
        # Installation verifies hashes of both files in the immutable version directory.
        py_compile.compile(str(source), cfile=cache, dfile=source.relative_to(resources).as_posix(),
                           doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    print(f"Compiled {len(sources)} startup modules with the staged Python", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve())
