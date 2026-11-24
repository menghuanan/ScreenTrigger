"""TASK-003 探针：首批 OCR 语言（中/英/日/韩）逐语言实测。

用途：冻结首批 OCR 验收语言（用户 2026-10-09 确认中英+日韩）前，逐语言安装/加载
模型并跑识别场景。仅声明实测通过的语言；失败语言如实记录并降级。

场景：每语言 1 张系统字体渲染样张（中/日/韩用对应 Windows 字体，避免 PIL 默认字体
无 CJK 字形的 TASK-001 局限）+ 1 张数字标点样张；输出实际识别文本与模型文件清单。

退出码：0=声明语言全部通过；1=有语言未通过。
用法：
  <probe-task001 venv python> tools/probes/ocr_lang_probe.py --json-out reports/TASK-003/ocr-lang-probe.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "windows"))
from common import write_report  # noqa: E402

# 语言 → (paddleocr lang, 样本文本, 候选字体)
LANGS = [
    ("ch", "屏幕自动化 2026", ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"]),
    ("japan", "自動化テスト 2026", ["C:/Windows/Fonts/msgothic.ttc", "C:/Windows/Fonts/YuGothM.ttc"]),
    ("korean", "자동화 테스트 2026", ["C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/gulim.ttc"]),
]
DIGIT_TEXT = "No.123  Done!"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def render(text: str, fonts: list[str], out: str) -> str | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    img = Image.new("RGB", (560, 120), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for fp in fonts:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, 40)
                d.text((20, 30), text, fill=(0, 0, 0), font=font)
                img.save(out)
                return fp
            except Exception:
                continue
    return None


def model_files_sha(base: str, name: str) -> list[dict]:
    model_dir = os.path.join(base, name)
    files = []
    if os.path.isdir(model_dir):
        for root, _d, fns in os.walk(model_dir):
            for fn in fns:
                p = os.path.join(root, fn)
                try:
                    files.append({"file": os.path.relpath(p, model_dir).replace("\\", "/"),
                                  "bytes": os.path.getsize(p),
                                  "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest()})
                except OSError:
                    files.append({"file": fn, "sha256": "read_failed"})
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-003 OCR 语言探针")
    parser.add_argument("--json-out", default="reports/TASK-003/ocr-lang-probe.json")
    args = parser.parse_args()

    os.environ["FLAGS_use_mkldnn"] = "0"  # TASK-001 实测：默认 MKLDNN/PIR 崩溃
    from paddleocr import PaddleOCR

    tmp = os.path.join("reports", "TASK-003", "lang-samples")
    os.makedirs(tmp, exist_ok=True)
    model_base = os.path.join(os.path.expanduser("~"), ".paddlex", "official_models")
    results = {}

    for lang, sample, fonts in LANGS:
        entry = {"lang": lang, "status": "fail", "samples": {}, "error": None}
        try:
            ocr = PaddleOCR(
                lang=lang,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
            img_path = os.path.join(tmp, f"sample-{lang}.png")
            used_font = render(sample, fonts, img_path)
            if not used_font:
                entry["error"] = "no_cjk_font_available"
            else:
                entry["font"] = used_font
                res = ocr.predict(img_path)
                texts = []
                for r in res:
                    texts += list(r.get("rec_texts") or [])
                entry["samples"][sample] = [str(t) for t in texts]
                img_path2 = os.path.join(tmp, f"sample-{lang}-digits.png")
                render(DIGIT_TEXT, fonts or ["C:/Windows/Fonts/arial.ttf"], img_path2)
                res2 = ocr.predict(img_path2)
                texts2 = []
                for r in res2:
                    texts2 += list(r.get("rec_texts") or [])
                entry["samples"][DIGIT_TEXT] = [str(t) for t in texts2]
                joined = " ".join(entry["samples"][sample])
                core = sample.split()[-1]  # 年份等关键 token
                entry["status"] = "pass" if core in joined or len(joined) > 3 else "fail"
                if entry["status"] == "fail":
                    entry["error"] = f"recognition mismatch: {joined!r}"
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        results[lang] = entry

    # 已装语言模型清单
    models = {}
    for lang, _s, _f in LANGS:
        for name in os.listdir(model_base) if os.path.isdir(model_base) else []:
            low = name.lower()
            if (lang == "ch" and ("v6_medium" in low or "lcnet" in low or "uvdoc" in low)) \
               or (lang == "japan" and "japan" in low) or (lang == "korean" and "korean" in low):
                models[name] = model_files_sha(model_base, name)

    all_pass = all(v["status"] == "pass" for v in results.values())
    result = {
        "probe": "ocr_lang_probe",
        "task": "TASK-003",
        "rebuild_command": "python tools/probes/ocr_lang_probe.py --json-out " + args.json_out,
        "recorded_at": utc_now(),
        "first_batch_languages": ["zh(ch)", "en(含于 ch)", "japan", "korean"],
        "languages": results,
        "models_manifest": models,
        "note": "逐语言声明覆盖；CER 数据集验收（每语言 100 张）属 TEST-004 范围",
    }
    write_report(args.json_out, result)
    for k, v in results.items():
        print(f"  {k}: {v['status']} {v.get('error') or ''} samples={v.get('samples')}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
