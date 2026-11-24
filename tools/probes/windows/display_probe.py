"""TASK-002 探针：DPI awareness、显示器拓扑、虚拟屏幕负坐标能力。

场景（对应任务卡 STEPS.01）：
1. Per-Monitor V2 awareness 设置与验证；
2. 逐显示器物理 rect、effective DPI、dpi_scale、primary 标记；
3. 虚拟屏幕包围盒（负坐标能力：本机单屏时如实记录 origin，多屏负坐标行为 NOT_RUN）；
4. 100/125/150/200/300% 缩放矩阵：本机无法改系统缩放，记录当前实际 scale 并把矩阵标记 NOT_RUN。

退出码：0=当前环境可测场景全过；1=有 FAIL；2=环境不可测。
用法：python tools/probes/windows/display_probe.py --json-out reports/TASK-002/display-probe-result.json
"""

from __future__ import annotations

import argparse
import sys
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from common import (base_environment, set_per_monitor_v2, write_report)  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-002 DPI/显示器探针")
    parser.add_argument("--json-out", default="reports/TASK-002/display-probe-result.json")
    args = parser.parse_args()

    awareness = set_per_monitor_v2()
    env = base_environment(awareness)
    monitors = env["monitors"]
    vs = env["virtual_screen"]

    scenarios = {}

    # 场景1：Per-Monitor V2 生效
    scenarios["pmv2_awareness"] = {
        "expected": "当前线程的 DPI context 等于 PER_MONITOR_AWARE_V2",
        "actual": {"awareness": awareness, "per_monitor_v2": env["per_monitor_v2"]},
        "pass": env["per_monitor_v2"],
    }

    # 场景2：逐显示器 DPI/物理 rect
    valid = bool(monitors) and all(
        m["physical_rect"]["w"] > 0 and m["dpi"] > 0 for m in monitors
    )
    scenarios["per_monitor_dpi"] = {
        "expected": "每显示器 physical_rect 与 effective dpi>0",
        "actual": monitors,
        "pass": valid,
    }

    # 场景3：虚拟屏幕（负坐标能力事实）
    has_negative_origin = vs["x"] < 0 or vs["y"] < 0
    scenarios["virtual_screen"] = {
        "expected": "虚拟屏幕包围盒与主显示器一致或含负坐标",
        "actual": {**vs, "negative_origin": has_negative_origin,
                   "note": "单屏时 origin 通常 (0,0)；负坐标行为需多屏环境，NOT_RUN"},
        "pass": vs["w"] > 0 and vs["h"] > 0,
    }

    # 场景4：DPI 缩放矩阵
    scales = sorted({m["dpi_scale"] for m in monitors})
    matrix_needed = {1.0, 1.25, 1.5, 2.0, 3.0}
    covered = [s for s in matrix_needed if any(abs(m["dpi_scale"] - s) < 0.01 for m in monitors)]
    scenarios["dpi_scale_matrix"] = {
        "expected": "100/125/150/200/300% 混合 DPI 像素误差≤1",
        "actual": {
            "current_scales": scales,
            "available_scales": sorted(covered),
            "status": "NOT_RUN",
            "reason": "显示器清点只记录当前拓扑；各缩放下的像素误差需实际拖选动态目标测量",
        },
        "pass": None,
    }

    result = {
        "probe": "display_probe",
        "task": "TASK-002",
        "rebuild_command": "python tools/probes/windows/display_probe.py --json-out " + args.json_out,
        "environment": env,
        "scenarios": scenarios,
    }
    write_report(args.json_out, result)
    ok = all(v["pass"] is not False for v in scenarios.values())
    for k, v in scenarios.items():
        status = {True: "PASS", False: "FAIL", None: "NOT_RUN"}[v["pass"]]
        print(f"  {k}: {status}")
    print(f"[display_probe] monitors={len(monitors)} scales={scales} virtual={vs}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
