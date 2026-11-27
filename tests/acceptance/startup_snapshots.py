"""Prepare the three unstarted script data snapshots with the package's real catalog services."""
import ctypes
import hashlib
import json
from pathlib import Path
import sys
from PIL import Image, ImageChops


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def main(out, display):
    from backend.main import SidecarArgs, build_catalog_services
    from backend.config.defaults import default_config
    from backend.image.store import SessionImageStore
    import backend.main
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/packaging"))
    from portable_layout import MARKER, metadata_dir
    app = Path(sys.executable).resolve().parents[2]
    assert Path(backend.main.__file__).resolve().is_relative_to(app / "resources/backend")
    assert not ctypes.windll.shell32.IsUserAnAdmin()
    out.mkdir(exist_ok=False, parents=True)
    suite = Path(__file__).resolve().parent
    sys.path.insert(0, str(suite))
    from ocr_quality import render_sample
    table = json.loads((suite / "fixtures/ocr.json").read_bytes())
    groups = {}
    for kind in ("lua", "zh", "ko"):
        base = out / kind
        data = base / "data"
        data.mkdir(parents=True)
        config = default_config()
        config["hotkeys"].update(start_selection="Ctrl+Shift+F6", stop_script="Ctrl+Shift+F7", pause_script="Ctrl+Shift+F8")
        save(data / "config.json", config)
        timing = data / "diagnostics"
        timing.mkdir()
        (timing / "startup-timing.enabled").touch(exist_ok=False)
        script = "log('ST-040/first')\n"
        expected = 40
        images = []
        if kind == "lua":
            script += "log('ST-040/result:40')\nreturn 40"
        else:
            group = table[kind]
            sample = group["samples"][0]
            import os
            font = Path(os.environ["WINDIR"]) / "Fonts" / group["font"]
            assert sha(font) == group["font_sha256"]
            pixels = render_sample(sample, font)
            assert hashlib.sha256(pixels).hexdigest() == sample["image_sha256"]
            image = Image.frombytes("RGB", (720,140), pixels, "raw", "BGR")
            media = data / "fixtures"
            media.mkdir()
            expected = sample["reference"]
            for index in range(21):
                path = media / f"{index:02d}.png"
                moved = ImageChops.offset(image, index, 0)
                moved.save(path)
                images.append(dict(file=path.relative_to(data).as_posix(), sha256=sha(path),
                                   rgb_sha256=hashlib.sha256(moved.tobytes()).hexdigest(), expected=expected))
            assert len({r["rgb_sha256"] for r in images}) == len(images)
            area = "{space='screen_physical',display_id=" + json.dumps(display) + ",x=100,y=150,width=720,height=140}"
            script += "local blocks=ocr(nil," + area + ",'" + group["model"] + "')\n"
            script += "if not blocks[1] then error('no text') end\nlog('ST-040/ocr:'..blocks[1].text)\nreturn blocks[1].text"
        repository, _, service = build_catalog_services(SidecarArgs("startup-fixture", data_dir=str(data)), SessionImageStore("startup-fixture"))
        try:
            name = "ST-040 " + kind
            frozen = service.save_script(name, script, [])
            assert frozen["source_sha256"] == hashlib.sha256(script.encode("utf8")).hexdigest()
        finally:
            repository.close()
        groups[kind] = dict(name=name, script_id=frozen["id"], source_sha256=frozen["source_sha256"],
                            expected=expected, images=images, model_id=table[kind]["model"] if kind != "lua" else None,
                            snapshot={f.relative_to(data).as_posix():sha(f) for f in sorted(data.rglob("*")) if f.is_file()})
    save(out / "snapshots.json", dict(groups=groups, archive_source=json.loads((metadata_dir(app)/MARKER).read_bytes())["source_sha256"],
         executable=str(sys.executable), fixture=sha(suite/"fixtures/ocr.json"), display=display))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), sys.argv[2])
