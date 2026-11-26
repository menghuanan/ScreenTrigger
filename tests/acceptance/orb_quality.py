"""Generate independent labelled UI tiles, then measure the frozen ORB provider."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from time import perf_counter

import cv2 as cv
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.tests import vision_fixtures as vf
from backend.vision.cancel import NullCancelToken
from backend.vision.providers.orb import OrbProvider


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, obj):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=1)


def tile(rng):
    """One original per query. Independent artwork with lettering and fine geometry."""
    image = np.full((100, 120, 3), rng.integers(30, 105), dtype=np.uint8)
    for n in range(45):
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        point = tuple(int(v) for v in rng.integers([4, 4], [116, 96]))
        if n % 2:
            cv.circle(image, point, int(rng.integers(2, 10)), color, -1, cv.LINE_AA)
        else:
            cv.line(image, point, (int(rng.integers(120)), int(rng.integers(100))), color, 2, cv.LINE_AA)
    cv.putText(image, f"{rng.integers(65536):04X}", (8, 62), cv.FONT_HERSHEY_DUPLEX,
               .85, (245, 245, 245), 2, cv.LINE_AA)
    return image


def place(scene, sample, center, angle, scale):
    h, w = sample.shape[:2]
    affine = cv.getRotationMatrix2D((w/2, h/2), angle, scale)
    affine[:, 2] += np.asarray(center) - [w/2, h/2]
    shape = (scene.shape[1], scene.shape[0])
    warped = cv.warpAffine(sample, affine, shape)
    mask = cv.warpAffine(np.full((h,w),255,np.uint8), affine, shape)
    scene[mask > 0] = warped[mask > 0]
    corners = cv.transform(np.float32([[[0,0],[w,0],[w,h],[0,h]]]), affine)[0]
    lo, hi = np.rint(corners.min(axis=0)).astype(int), np.rint(corners.max(axis=0)).astype(int)
    return [*map(int,lo), *map(int,hi-lo)]


def generate(folder, split, seed):
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    for n in range(400 if split == "holdout" else 64):
        rng = np.random.default_rng([seed, n])
        sample = tile(rng)
        scene = np.full((216, 452, 3), rng.integers(50, 180, 3), np.uint8)
        positive = n % 2 == 0
        kind = n // 2 % 5
        if not positive and kind == 1:
            # A separate low-contrast original for every group, including across splits.
            level = int(rng.integers(40, 180))
            sample[:] = cv.resize(rng.integers(level,level+7,(4,5,3),dtype=np.uint8),(120,100))
        place(scene, tile(rng), (340,110), -9, .97)
        truths = []
        if positive:
            angle, scale = float(rng.uniform(-26,26)), float(rng.uniform(.75,1.2))
            truths.append(place(scene, sample, (105,106), angle, scale))
            if kind == 2:
                truths.append(place(scene, sample, (340,110), -angle, scale))
        elif kind == 2:
            # A small piece of genuine matching texture amid an unrelated item.
            distractor = tile(rng)
            distractor[:15,:22] = sample[:15,:22]
            place(scene, distractor, (108,110), 0, 1)
        elif kind != 0:
            place(scene, tile(rng), (105,106), 13, 1.03)
        else:
            level = int(rng.integers(60,150))
            scene[:] = cv.resize(rng.integers(level,level+4,(3,5,3),dtype=np.uint8),(452,216))
        group = f"{split}-{seed}-{n:03}"
        names = {}
        for label, image in (("sample",sample),("scene",scene)):
            path = folder / f"{group}-{label}.png"
            ok, encoded = cv.imencode(".png", image)
            if not ok: raise OSError("PNG encoding failed")
            path.write_bytes(encoded.tobytes())
            names[label] = {"file":path.name, "sha256":sha(path)}
        rows.append(dict(group=group, positive=positive, truth=truths, **names))
    save(folder / "manifest.json", dict(split=split, seed=seed, rows=rows, origin="Project-generated UI artwork"))


def overlap(a, b):
    x, y, w, h = a
    xx, yy, ww, hh = b
    area = max(0, min(x+w, xx+ww)-max(x,xx)) * max(0,min(y+h,yy+hh)-max(y,yy))
    return area / (w*h + ww*hh - area)


def distinct_sources(folder, tuning):
    manifest = json.loads((folder/"manifest.json").read_bytes())
    calibration = json.loads((tuning/"manifest.json").read_bytes())
    if manifest["split"] != "holdout" or calibration["split"] != "tune":
        raise ValueError("Expected independent holdout and tuning manifests")
    for key in ("sample", "scene"):
        references = {row[key]["sha256"] for row in calibration["rows"]}
        if any(row[key]["sha256"] in references for row in manifest["rows"]):
            # Empty backgrounds may be identical. They must also belong to separate
            # source groups, so detect contamination before the first prediction.
            raise ValueError(f"Shared {key} bytes across tuning and holdout")
    if {r["group"] for r in manifest["rows"]} & {r["group"] for r in calibration["rows"]}:
        raise ValueError("Shared source group")


def measure(folder, output, tuning=None):
    output.mkdir(parents=True,exist_ok=False)
    manifest = json.loads((folder/"manifest.json").read_bytes())
    if manifest["split"] == "holdout":
        if tuning is None:
            raise ValueError("A held-out run requires its calibration manifest for the split check")
        distinct_sources(folder,tuning)
    reader = vf.MemoryPixelReader()
    provider = OrbProvider(reader)
    provider.initialize(None)
    freeze = dict(manifest=sha(folder/"manifest.json"), implementation=sha(ROOT/"backend/vision/providers/orb.py"),
                  evaluator=sha(Path(__file__)), cv=cv.__version__, numpy=np.__version__, rng_seed=42,
                  min_inliers=8,min_inlier_ratio=.45,max_reprojection_error=3.0,split=manifest["split"])
    save(output/"freeze.json", freeze)
    cv.setRNGSeed(freeze["rng_seed"])
    totals = dict(TP=0,FP=0,FN=0,TN=0)
    with (output/"predictions.jsonl").open("x",encoding="utf-8") as stream:
        for row in manifest["rows"]:
            for key in ("sample","scene"):
                path = folder / row[key]["file"]
                if sha(path) != row[key]["sha256"]: raise ValueError(path)
                pixels = cv.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv.IMREAD_COLOR)
                h,w = pixels.shape[:2]
                raw = vf.raw_image(w,h,pixels.tobytes())
                reader.add(key,raw)
            frame = vf.make_frame(raw,"scene",frame_id=row["group"])
            start = perf_counter()
            result = provider.recognize(frame,{"template_image_id":"sample"},NullCancelToken())
            elapsed = 1000 * (perf_counter()-start)
            if len(result.candidates)>1: raise ValueError("multiple predictions")
            box = score = center_error = None
            iou = 0
            if result.candidates:
                candidate = result.candidates[0]
                b = candidate.bbox
                box = [b.x,b.y,b.width,b.height]
                score = candidate.confidence
                if row["truth"]:
                    best = max(row["truth"],key=lambda label:overlap(box,label))
                    iou = overlap(box,best)
                    center_error = math.hypot(box[0]+box[2]/2-best[0]-best[2]/2,
                                              box[1]+box[3]/2-best[1]-best[3]/2)
            hit = bool(box and iou>=.5)
            totals["TP"] += hit
            totals["FP"] += bool(box and not hit)
            totals["FN"] += bool(row["positive"] and not hit)
            totals["TN"] += bool(not row["positive"] and not box)
            stream.write(json.dumps(dict(group=row["group"],truth=row["truth"],box=box,score=score,
                                         iou=iou,center_error=center_error,geometry=provider.last_measurement,
                                         elapsed_ms=elapsed))+"\n")
    tp,fp,fn = (totals[key] for key in ("TP","FP","FN"))
    precision = tp/(tp+fp) if tp+fp else None
    recall = tp/(tp+fn)
    summary = dict(**totals,precision=precision,recall=recall,passed=precision is not None and precision>=.95 and recall>=.9)
    save(output/"metrics.json",summary)
    print(json.dumps(summary))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action",required=True)
    create = sub.add_parser("generate")
    create.add_argument("folder",type=Path)
    create.add_argument("split",choices=("tune","holdout"))
    create.add_argument("seed",type=int)
    check = sub.add_parser("measure")
    check.add_argument("folder",type=Path)
    check.add_argument("output",type=Path)
    check.add_argument("--tuning",type=Path)
    split = sub.add_parser("verify-split")
    split.add_argument("folder",type=Path)
    split.add_argument("tuning",type=Path)
    args = parser.parse_args()
    if args.action == "generate":
        generate(args.folder,args.split,args.seed)
    elif args.action == "verify-split":
        distinct_sources(args.folder,args.tuning)
    else:
        sys.exit(measure(args.folder,args.output,args.tuning))
