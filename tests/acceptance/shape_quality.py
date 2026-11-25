"""Per-provider detector measurements on reproducible generated, labelled images."""
import hashlib
import json
from pathlib import Path
import random
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import resources, pixel_fixtures
import numpy as np
vf = pixel_fixtures()
from backend.vision.cancel import NullCancelToken
from backend.vision.providers import ContourProvider, TemplateProvider

def iou(left, right):
    x, y, w, h = left
    xx, yy, ww, hh = right
    overlap = max(0, min(x+w, xx+ww)-max(x, xx)) * max(0, min(y+h, yy+hh)-max(y, yy))
    return overlap / (w*h + ww*hh - overlap)

def score(rows, threshold):
    tp = fp = fn = 0
    for row in rows:
        candidates = [c["bbox"] for c in row["detected"] if c["score"] >= threshold]
        hit = row["truth"] and any(iou(row["truth"], c) >= .5 for c in candidates)
        tp += int(bool(hit))
        fp += len(candidates) - int(bool(hit))
        fn += int(bool(row["truth"]) and not hit)
    return dict(tp=tp,fp=fp,fn=fn,precision=tp/(tp+fp) if tp+fp else 0,recall=tp/(tp+fn))

def run(name, out):
    reader = vf.MemoryPixelReader()
    if name == "template":
        size = 16
        ref = vf.distinctive_patch(size,size,seed=91)
        provider = TemplateProvider(reader)
    else:
        size = 24
        mask = vf.circle_mask(size,10)
        ref = vf.striped_patch(mask,period=4,orient="v",low=60,high=255,border_bgr=255,border=2)
        provider = ContourProvider(reader)
    reader.add("ref", vf.raw_image(size,size,ref))
    def collect(seed, pairs, floor):
        rng = random.Random(seed)
        rows = []
        for n in range(pairs*2):
            positive = n % 2 == 0
            gray = rng.randrange(0,36)
            canvas = vf.Canvas(100,64,(gray,gray,gray))
            point = (rng.randint(2,27),rng.randint(2,37))
            if name == "template":
                patch = vf.distinctive_patch(size,size,seed=rng.getrandbits(32),low=rng.randrange(25,50),high=rng.randrange(190,230))
                distractor = np.frombuffer(patch,dtype=np.uint8).reshape(size,size,3).copy()
                distractor[:,::3] = 255 - distractor[:,::3]
                distractor = distractor.tobytes()
            else:
                mask = vf.circle_mask(size,rng.choice([9.5,10,10.5]))
                patch = vf.striped_patch(mask,period=rng.randint(3,6),orient="h",low=70,high=245,border_bgr=255,border=2)
                mask2 = vf.star_mask(size,10,rng.choice([3.0,4.0,5.0]),points=rng.choice([3,4,5]))
                distractor = vf.striped_patch(mask2,period=4,orient="h",low=70,high=245,border_bgr=255,border=2)
            if positive:
                canvas.paste(*point,patch,size,size)
            canvas.paste(rng.randint(54,73),rng.randint(2,37),distractor,size,size)
            raw = canvas.to_raw()
            reader.add("scene",raw)
            frame = vf.make_frame(raw,"scene",frame_id=f"{seed}-{n}")
            result = provider.recognize(frame,{"template_image_id":"ref","params":{"threshold":floor}},NullCancelToken())
            detected = [dict(bbox=[c.bbox.x,c.bbox.y,c.bbox.width,c.bbox.height],score=c.confidence) for c in result.candidates]
            rows.append(dict(index=n,truth=[*point,size,size] if positive else None,sha256=frame.image_ref.sha256,detected=detected))
        return rows
    with TemporaryDirectory() as model_dir:
        provider.initialize(model_dir)
        grid = [.75,.80,.85,.90,.93,.95,.97,.98,.99]
        tune = collect(196721,32,grid[0])
        def selection_key(t):
            stat = score(tune,t)
            p,r = stat["precision"],stat["recall"]
            return (2*p*r/(p+r) if p+r else 0,-abs(t-.85))
        choice = max(grid,key=selection_key)
        test = collect(728149,200,choice)
    result=dict(provider=name,reference_sha256=hashlib.sha256(ref).hexdigest(),selection="maximum calibration F1, ties closest to .85",grid=grid,calibration_seed=196721,measurement_seed=728149,threshold=choice,calibration=score(tune,choice),measurement=score(test,choice),calibration_rows=tune,measurement_rows=test)
    with (out/(name+".json")).open("x",encoding="utf8") as file:
        json.dump(result,file,ensure_ascii=False,indent=1)
    return {k:result[k] for k in ("provider","threshold","calibration","measurement","reference_sha256")}

if __name__ == "__main__":
    output = Path(sys.argv[1]); output.mkdir(exist_ok=False)
    resources()
    aggregate = [run(name,output) for name in ("template","contour")]
    with (output/"metrics.json").open("x",encoding="utf8") as file:
        json.dump(aggregate,file,indent=2)
    print(json.dumps(aggregate,indent=2))
    raise SystemExit(int(any(x["measurement"]["precision"] < .95 or x["measurement"]["recall"] < .90 for x in aggregate)))
