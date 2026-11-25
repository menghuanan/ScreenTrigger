"""Measure the installed OCR provider against frozen font, frame and reference hashes."""
import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import pixel_fixtures, resources, save, sha
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from backend.vision.cancel import NullCancelToken
from backend.vision.providers.ocr import OCREngineProvider


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        next_row = [i]
        for j, y in enumerate(b, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j-1] + (x != y)))
        row = next_row
    return row[-1]


def render_sample(sample, font):
    image = Image.new("RGB", (720, 140), sample.get("background", "white"))
    draw = ImageDraw.Draw(image)
    if "rectangle" in sample:
        draw.rectangle(sample["rectangle"], outline=sample["outline"], width=2)
    if sample["reference"]:
        draw.text((24, 40), sample["reference"], fill="black",
                  font=ImageFont.truetype(str(font), sample["font_size"]))
    return np.asarray(image)[:, :, ::-1].tobytes()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("output", type=Path)
    p.add_argument("--languages", nargs="+", default=["zh", "en", "ja", "ko"])
    p.add_argument("--supplemental", action="store_true")
    args = p.parse_args()
    installed = resources()
    args.output.mkdir(exist_ok=False)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(args.output.resolve() / "cache")
    fixed = Path(__file__).parent / "fixtures" / ("ocr-extra.json" if args.supplemental else "ocr.json")
    fixture = json.loads(fixed.read_bytes())
    vf = pixel_fixtures()
    reader = vf.MemoryPixelReader()
    provider = OCREngineProvider(reader, library_root=str(installed / "weights"))
    provider.initialize(str(args.output / "metadata"))
    summary = {}
    try:
        for lang in (fixture if args.supplemental else args.languages):
            group = fixture[lang]
            font = Path(os.environ["WINDIR"]) / "Fonts" / group["font"]
            assert sha(font) == group["font_sha256"], f"Font changed: {font}"
            rows = []
            # Five independent warm-ups precede all timed trials.
            warm = group["samples"][0]
            warm_image = vf.raw_image(720, 140, render_sample(warm, font))
            reader.add("src", warm_image)
            for _ in range(5):
                provider.recognize(vf.make_frame(warm_image), {"model_id": group["model"]}, NullCancelToken())
            for sample in group["samples"]:
                label = sample["reference"]
                raw = vf.raw_image(720,140,render_sample(sample, font))
                assert hashlib.sha256(raw.data).hexdigest() == sample["image_sha256"]
                reader.add("src",raw)
                frame = vf.make_frame(raw)
                start = time.perf_counter()
                blocks = provider.recognize(frame, {"model_id":group["model"]}, NullCancelToken()).text_blocks
                elapsed = (time.perf_counter()-start)*1000
                actual = "".join(block.text for block in blocks)
                reference = unicodedata.normalize("NFC", label)
                errors = distance(reference, unicodedata.normalize("NFC", actual))
                rows.append(dict(**sample, actual=actual, blocks=[asdict(b) for b in blocks],
                                 errors=errors, characters=len(reference), latency_ms=elapsed))
            latencies = sorted(r["latency_ms"] for r in rows)
            errors = sum(r["errors"] for r in rows)
            total = sum(r["characters"] for r in rows)
            summary[lang] = dict(samples=len(rows), errors=errors, characters=total, cer=errors/total if total else None,
                                 empty_false_positives=sum(bool(r["actual"]) for r in rows if not r["reference"]),
                                 over_5s=sum(r["latency_ms"] > 5000 for r in rows),
                                 latency_ms={str(p):latencies[math.ceil(len(latencies)*p/100)-1] for p in (50,95,99,100)})
            save(args.output / (lang+".json"), rows)
            print(lang, json.dumps(summary[lang]), flush=True)
        save(args.output / "metrics.json", dict(fixture_sha256=sha(fixed),
             package_manifest_sha256=sha(installed.parent / "package.json"), scores=summary))
        return int(any(v["over_5s"] or (not args.supplemental and v["cer"] > .10) for v in summary.values()))
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
