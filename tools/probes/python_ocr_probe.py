"""TASK-001 探针：Python 3.13.x + 离线 OCR + OpenCV/Pillow 兼容性验证。

只使用 Python 标准库即可运行（输出环境矩阵与 unavailable 结论）；
候选依赖安装进隔离 venv 后运行完整 OCR 场景。

用途（对应 docs/agent/任务拆解-P0.md TASK-001）：
1. 固定 OS/Python/架构/CPU/GPU 环境矩阵；
2. 检查候选包是否提供 cp313/win_amd64 可用实现；
3. 可用时执行 OCR 场景：空图、无字、多行、旋转、混合语言、数字、标点、缺模型；
4. 输出版本矩阵、模型 manifest、许可证、替代方案与能力清单 JSON，供 hello 使用。

退出码：0=全部场景 PASS；1=存在 FAIL；2=栈不可用（有效探针结论，非工具故障）。

用法：
  python tools/probes/python_ocr_probe.py --json-out reports/TASK-001/probe-result.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.vision.providers.ocr_models import REPO_CATALOG_PATH, OcrModelCatalog, resolve_library_root
from backend.vision.types import RecognitionError

# 候选依赖与用途、许可证（与 requirements-probe.txt.template 保持一致）
CANDIDATES = [
    {"import": "paddlepaddle", "dist": "paddlepaddle", "role": "OCR 推理运行时", "license": "Apache-2.0"},
    {"import": "paddleocr", "dist": "paddleocr", "role": "OCR 引擎封装", "license": "Apache-2.0"},
    {"import": "cv2", "dist": "opencv-python", "role": "图像处理（模板/轮廓/颜色）", "license": "Apache-2.0"},
    {"import": "numpy", "dist": "numpy", "role": "数组运算", "license": "BSD-3-Clause"},
    {"import": "PIL", "dist": "pillow", "role": "图像编解码", "license": "HPND/MIT-CMU"},
]

REQUIRED_FOR_OCR = ["paddlepaddle", "paddleocr", "numpy", "pillow"]

# 包版本、模型版本分开记录（TASK-001.DESIGN.02）；模型 manifest 在模型实际安装后填充。
MODEL_MANIFEST_PLACEHOLDER = {
    "family": None,
    "model_name": None,
    "languages": [],
    "files": [],
    "note": "模型未安装或未离线部署时保持 null，不得虚构版本",
}

ALTERNATIVES = [
    {"name": "Tesseract (pytesseract)", "note": "D04 备选；需外部 exe，分发与许可另行评估"},
    {"name": "ONNX Runtime + PP-OCR 导出模型", "note": "D04 备选；推理自包含，模型需导出"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_environment() -> dict:
    env = {
        "os": platform.platform(),
        "os_version": platform.version(),
        "python": sys.version.replace("\n", " "),
        "python_version_tuple": list(sys.version_info[:3]),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "gpu": None,
        "recorded_at": utc_now(),
    }
    try:
        out = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            capture_output=True, text=True, timeout=15,
        )
        gpus = [ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()]
        env["gpu"] = gpus
    except Exception:
        pass  # wmic 在较新 Win11 已移除，走 PowerShell 兜底
    if not env["gpu"]:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=30,
            )
            gpus = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            env["gpu"] = gpus or "detection_failed: powershell returned empty"
        except Exception as exc:
            env["gpu"] = f"detection_failed: {exc}"
    return env


def check_packages() -> list[dict]:
    rows = []
    for cand in CANDIDATES:
        row = {
            "dist": cand["dist"], "role": cand["role"], "license_declared": cand["license"],
            "version": None, "available": False, "reason": None,
        }
        try:
            row["version"] = importlib_metadata.version(cand["dist"])
            row["available"] = True
        except importlib_metadata.PackageNotFoundError:
            row["reason"] = "not_installed"
        except Exception as exc:
            row["reason"] = f"metadata_error: {exc}"
        rows.append(row)
    return rows


def normalize_ocr_error(exc: Exception) -> str:
    """供应商异常归一为 MODEL_UNAVAILABLE / RECOGNITION_FAILED（07-实现细节清单 §2）。"""
    if isinstance(exc, RecognitionError):
        return exc.code
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "no such file" in lowered or "not found" in lowered or "missing" in lowered or "load" in lowered:
        return "MODEL_UNAVAILABLE"
    return "RECOGNITION_FAILED"


def make_images():
    """生成场景输入图（无第三方库时用 PPM 手写位图，保证探针在最小环境也能跑缺模型场景）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        have_pil = True
    except Exception:
        have_pil = False

    scenes = {}
    if have_pil:
        white = (255, 255, 255)
        black = (0, 0, 0)
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
        font = ImageFont.truetype(str(font_path), 24) if font_path.is_file() else ImageFont.load_default(size=24)
        img = Image.new("RGB", (200, 200), white)
        scenes["empty"] = img  # 纯色空图
        img = Image.new("RGB", (400, 200), white)
        scenes["no_text"] = img  # 有噪声形状但无文字
        d = ImageDraw.Draw(scenes["no_text"])
        d.rectangle([20, 20, 120, 120], outline=black)
        img = Image.new("RGB", (600, 300), white)
        d = ImageDraw.Draw(img)
        for i, line in enumerate(["first line", "second line", "third line"]):
            d.text((30, 40 + i * 60), line, fill=black, font=font)
        scenes["multiline"] = img
        img = Image.new("RGB", (600, 300), white)
        d = ImageDraw.Draw(img)
        d.text((100, 100), "rotated sample", fill=black, font=font)
        scenes["rotated"] = img.rotate(30, expand=True, fillcolor=white)
        img = Image.new("RGB", (600, 200), white)
        d = ImageDraw.Draw(img)
        d.text((30, 80), "ScreenTrigger 2026 自动化 screen 自动", fill=black, font=font)
        scenes["mixed_lang"] = img
        img = Image.new("RGB", (400, 200), white)
        d = ImageDraw.Draw(img)
        d.text((30, 80), "0123456789", fill=black, font=font)
        scenes["digits"] = img
        img = Image.new("RGB", (400, 200), white)
        d = ImageDraw.Draw(img)
        d.text((30, 80), "Hello, world! 你好吗？", fill=black, font=font)
        scenes["punctuation"] = img
    return scenes, have_pil


def run_ocr_scenarios(result: dict, library: str) -> None:
    """OCR 场景矩阵。栈不可用时逐场景标 unavailable（MODEL_UNAVAILABLE）。"""
    rows = {}
    stack_ok = all(
        r["available"] for r in result["packages"] if r["dist"] in REQUIRED_FOR_OCR
    )
    if not stack_ok:
        missing = [r["dist"] for r in result["packages"]
                   if r["dist"] in REQUIRED_FOR_OCR and not r["available"]]
        for scene in ["empty", "no_text", "multiline", "rotated", "mixed_lang", "digits", "punctuation"]:
            rows[scene] = {"status": "unavailable", "error": "MODEL_UNAVAILABLE",
                           "reason": f"missing packages: {missing}"}
        for scene in ("missing_model", "damaged_model"):
            rows[scene] = {"status": "NOT_RUN", "reason": "安装推理栈后需单独验证模型预检"}
        result["ocr_scenarios"] = rows
        return

    # 完整栈已安装：先记录默认配置事实，再用已验证可用的配置执行场景
    scenes, _ = make_images()
    temp = tempfile.TemporaryDirectory(prefix="st-probe-")
    tmpdir = temp.name
    result.setdefault("runtime_notes", {})
    try:
        catalog = OcrModelCatalog.load(REPO_CATALOG_PATH)
        engine = next(e for e in catalog.engines if "zh" in e.languages)
        catalog.verify(library, (engine.detection_model, engine.recognition_model))
        local_models = dict(text_detection_model_name=engine.detection_model,
                            text_detection_model_dir=os.path.join(library, engine.detection_model),
                            text_recognition_model_name=engine.recognition_model,
                            text_recognition_model_dir=os.path.join(library, engine.recognition_model))
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR
        # 默认配置（MKLDNN 开启）兼容性检查：paddle 3.3.1 Windows PIR onednn 已知不兼容
        try:
            _digits_path = os.path.join(tmpdir, "_default_cfg.png")
            scenes["digits"].save(_digits_path)
            _default_ocr = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                **local_models,
            )
            _default_ocr.predict(_digits_path)
            result["runtime_notes"]["mkldnn_default"] = "pass（默认配置可推理）"
        except Exception as exc:
            result["runtime_notes"]["mkldnn_default"] = (
                f"fail: {normalize_ocr_error(exc)}: {str(exc)[:200]} —— 需 enable_mkldnn=False 绕过，"
                "该事实计入依赖锁与安装文档"
            )
        # 工作配置：禁用 MKLDNN + 裁剪文档预处理流水线
        ocr = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
            **local_models,
        )
        expected_tokens = {"multiline": ("first", "second", "third"), "rotated": ("rotated",),
                           "mixed_lang": ("ScreenTrigger", "自动"), "digits": ("0123456789",),
                           "punctuation": ("Hello", ",", "world", "!", "你好吗")}
        for name, img in scenes.items():
            path = os.path.join(tmpdir, f"{name}.png")
            img.save(path)
            try:
                res = ocr.predict(path)
                texts = []
                for r in res:
                    texts += list(r.get("rec_texts") or [])
                texts = [str(t) for t in texts]
                joined = " ".join(texts)
                matched = (not texts if name in ("empty", "no_text")
                           else all(token in joined for token in expected_tokens[name]))
                rows[name] = {"status": "pass" if matched else "fail", "texts": texts,
                              "expected_tokens": expected_tokens.get(name, ()), "error": None}
            except Exception as exc:
                rows[name] = {"status": "fail", "error": normalize_ocr_error(exc),
                              "detail": f"{type(exc).__name__}: {exc}"[:400]}
        # Exercise the same preflight used before starting offline inference.
        rec = engine.recognition_model
        for scene, expected in (("missing_model", "MODEL_UNAVAILABLE"), ("damaged_model", "DATA_CORRUPT")):
            if scene == "damaged_model":
                copy = Path(tmpdir) / rec
                shutil.copytree(Path(library) / rec, copy)
                with (copy / "inference.pdiparams").open("r+b") as weights:
                    first = weights.read(1)
                    weights.seek(0)
                    weights.write(bytes([first[0] ^ 1]))
            try:
                catalog.verify(tmpdir, (rec,))
                rows[scene] = {"status": "fail", "error": None}
            except RecognitionError as exc:
                rows[scene] = {"status": "pass" if exc.code == expected else "fail",
                               "error": exc.code, "detail": str(exc)}
    except Exception as exc:
        for name in ["empty", "no_text", "multiline", "rotated", "mixed_lang", "digits", "punctuation"]:
            rows[name] = {"status": "fail", "error": normalize_ocr_error(exc),
                          "detail": f"{type(exc).__name__}: {exc}"[:400]}
        for name in ("missing_model", "damaged_model"):
            rows[name] = {"status": "NOT_RUN", "detail": "栈初始化失败，未单独验证模型预检"}
    finally:
        result["ocr_scenarios"] = rows
        temp.cleanup()


def build_capability_list(result: dict) -> dict:
    """能力清单：available/unavailable 显式声明，供 hello/capability 查询使用。"""
    ocr_ok = (
        result["ocr_scenarios"] is not None
        and all(v["status"] == "pass" for k, v in result["ocr_scenarios"].items() if k != "missing_model")
    )
    return {
        "ocr": "available" if ocr_ok else "unavailable",
        "ocr_reason": None if ocr_ok else "见 ocr_scenarios 与 packages 缺失原因",
        "opencv": next(("available" if r["available"] else "unavailable"
                        for r in result["packages"] if r["dist"] == "opencv-python"), "unavailable"),
        "pillow": next(("available" if r["available"] else "unavailable"
                        for r in result["packages"] if r["dist"] == "pillow"), "unavailable"),
        "lua54": "separate_probe",  # 由 lua54_probe.py 判定
    }


def collect_model_manifest(base: str) -> dict:
    """扫描本机已下载的 PaddleX 官方模型目录，生成模型 manifest（family/名称/文件 sha256）。"""
    manifest = {"base_dir": base, "models": []}
    if not os.path.isdir(base):
        manifest["note"] = "模型目录不存在（未下载）"
        return manifest
    for name in sorted(os.listdir(base)):
        model_dir = os.path.join(base, name)
        if not os.path.isdir(model_dir):
            continue
        files = []
        for root, _dirs, fnames in os.walk(model_dir):
            for fn in fnames:
                p = os.path.join(root, fn)
                try:
                    files.append({"file": os.path.relpath(p, model_dir).replace("\\", "/"),
                                  "bytes": os.path.getsize(p), "sha256": sha256_file(p)})
                except OSError:
                    files.append({"file": fn, "bytes": None, "sha256": "read_failed"})
        manifest["models"].append({"name": name, "license": "Apache-2.0（PaddlePaddle 官方模型，分发条款以官方声明为准）",
                                   "files": files})
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-001 Python/OCR 兼容探针")
    parser.add_argument("--json-out", default="reports/TASK-001/probe-result.json")
    parser.add_argument("--model-library", default=None, help="已下载并可按仓库目录校验的离线权重根")
    args = parser.parse_args()
    library, _ = resolve_library_root(args.model_library)

    result = {
        "probe": "python_ocr_probe",
        "task": "TASK-001",
        "rebuild_command": subprocess.list2cmdline([sys.executable, __file__, "--json-out", args.json_out,
                                                    "--model-library", library]),
        "recorded_at": utc_now(),
        "environment": collect_environment(),
        "packages": check_packages(),
        "model_manifest": dict(MODEL_MANIFEST_PLACEHOLDER),
        "offline_test": {
            "status": "NOT_RUN",
            "reason": "本命令使用本地模型路径；测试机网络状态须单独取证",
        },
        "ocr_scenarios": None,
        "alternatives": ALTERNATIVES,
    }

    run_ocr_scenarios(result, library)
    result["capability"] = build_capability_list(result)
    result["model_manifest"] = collect_model_manifest(library)
    result["limitations"] = [
        "本探针使用含中文的检测/识别模型对。每个样本的识别文本见 ocr_scenarios。",
        "多语言 CER 和真实断网安装须在对应数据集和环境另行验证。",
    ]

    os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # 摘要输出（stdout 只保留摘要，traceback 写报告，不污染管道）
    print(f"[python_ocr_probe] capability={result['capability']['ocr']}")
    for k, v in (result["ocr_scenarios"] or {}).items():
        print(f"  scenario {k}: {v['status']} {v.get('error') or ''}")
    statuses = {v["status"] for v in (result["ocr_scenarios"] or {}).values()}
    if "fail" in statuses:
        return 1
    if (sys.version_info[:2] != (3, 13) or platform.system() != "Windows"
            or platform.machine().lower() != "amd64" or sys.maxsize <= 2**32):
        return 2
    if result["capability"]["ocr"] == "unavailable":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
