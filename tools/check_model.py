"""Build the check catalogue from the authoritative Markdown specifications.

No product tests run here. A task's partial tests never satisfy a TEST group.
"""
from pathlib import Path
import re


GROUPS = [(1, 2), (3, 6), (7, 9), (10, 12), (13, 16),
          (17, 19), (20, 22), (23, 23), (24, 26), (27, 29)]
STAGES = {f"TASK-{n:03}": "P0" if n <= 6 else "P1" if n <= 22
          else "P2" if n <= 26 else "P3" for n in range(1, 30)}


def read(root, path):
    return (root / path).read_text(encoding="utf-8-sig")


def ids(text, prefix):
    text = re.sub(rf"{prefix}-(\d{{3}})[–-]{prefix}-(\d{{3}})",
                  lambda m: "/".join(f"{prefix}-{i:03}" for i in
                                     range(int(m[1]), int(m[2]) + 1)), text)
    return list(dict.fromkeys(re.findall(rf"{prefix}-\d{{3}}", text)))


def rows(text, pattern):
    return [tuple(c.strip() for c in line.strip().strip("|").split("|"))
            for line in text.splitlines() if re.match(pattern, line)]


def build(root):
    checks, tasks, tests, requirements, successes, decisions, risks = {}, {}, {}, {}, {}, {}, {}

    def check(key, kind, expected, source, owner, **extra):
        checks[key] = dict(id=key, kind=kind, expected=expected, source=source,
                           owner=owner, allow_na=False, **extra)

    for phase in range(4):
        source = f"docs/agent/任务拆解-P{phase}.md"
        for match in re.finditer(r"^## (TASK-\d{3}) ([^\n]+)\n([\s\S]*?)(?=^## |\Z)",
                                 read(root, source), re.M):
            tid, title, body = match.groups()
            body = body.split("<a id=")[0]
            fields = dict(re.findall(r"\*\*([^*]+)\*\*：([\s\S]*?)(?=\*\*|\n\n|\Z)", body))
            task = dict(id=tid, title=title, stage=f"P{phase}",
                        group=next(f"A{i:02}" for i, (a, b) in enumerate(GROUPS, 1)
                                   if a <= int(tid[-3:]) <= b),
                        dependencies=ids(fields.get("前置", ""), "TASK"),
                        source=source, checks=[], tests=[], requirements=[])
            categories = {"设计": "DESIGN", "编码范围": "OUTPUT", "编码": "OUTPUT",
                          "步骤": "STEPS", "测试/完成": "VERIFY", "测试": "VERIFY", "完成": "DONE"}
            for label, suffix in categories.items():
                for i, clause in enumerate(filter(None, re.split("[；\n]", fields.get(label, ""))), 1):
                    key = f"{tid}.{suffix}.{i:02}"
                    check(key, "task", clause.strip(), source, tid, task=tid)
                    task["checks"].append(key)
            task["tests"] = ids(" ".join(fields.get(x, "") for x in ("测试", "测试/完成")), "TEST")
            # Explicit task-level checks have separate evidence from full acceptance checks.
            common = {
                "BUILD": "实际产物存在；按变更范围运行构建、语法、类型和静态检查，列明命令及结果。纯设计任务说明不适用原因。",
                "CONTRACT": "核对需求/非目标、公开接口、数据/错误契约及文档同步，无越权行为或未完成 TODO 冒充完成。",
                "BRANCHES": "逐项核对正常、异常、误操作、边界、超时、重试、取消；每个相关分支有预期、实际结果和证据。不相关分支逐项说明。",
                "REGRESSION": "列出影响范围；复验原失败用例及依赖/下游契约，说明回归选择与未覆盖项，不能仅以改动行数决定。",
                "UI": "如涉及 UI，检查键盘、焦点恢复、可理解错误、普通/调试日志行为和真实交互；不涉及 UI 说明原因。",
                "HANDOFF": "报告实际变更/产物、任务对应 TEST 子场景、命令/退出码、环境、限制、风险、问题单及是否允许下游开始；不把局部通过计为整组 TEST 通过。",
            }
            for suffix, expected in common.items():
                key = f"{tid}.{suffix}"
                check(key, "task", expected, "docs/agent/检查与闭环.md", tid, task=tid)
                checks[key]["allow_na"] = suffix in ("BUILD", "UI")
                task["checks"].append(key)
            if any(x in task["tests"] for x in ("TEST-005", "TEST-006", "TEST-008")):
                key = f"{tid}.INPUT"
                check(key, "task", "真实专用测试窗口核验停止、释放按键、越界、失焦；mock 仅证明隔离逻辑，不能替代适用的 OS 输入证据。探针/协议子任务明确自身验证边界。",
                      "docs/agent/执行约定.md", tid, task=tid)
                task["checks"].append(key)
            tasks[tid] = task

    source = "docs/agent/验收与测试.md"
    spec = read(root, source)
    for name, given, then in rows(spec, r"\| TEST-\d{3} "):
        tid, title = name.split(" ", 1)
        tests[tid] = dict(id=tid, title=title, checks=[])
        for category, value in (("SCENARIO", given), ("RESULT", then)):
            for i, clause in enumerate(value.split("；"), 1):
                key = f"{tid}.{category}.{i:02}"
                check(key, "acceptance", f"场景：{given}。断言：{then}。本项核验：{clause}", source, "发布验收负责人", test=tid)
                tests[tid]["checks"].append(key)
    # Measurements and true-OS evidence outside the table are first-class requirements.
    measurement_text = spec.split("## 固定数据集与测量方法\n", 1)[1].split("## 发布与文档证据", 1)[0]
    for i, paragraph in enumerate([p.strip() for p in measurement_text.split("\n\n") if p.strip()], 1):
        key = f"MEASURE-{i:03}"
        check(key, "measurement", paragraph, source, "发布验收负责人")

    source = "docs/product/审查与需求追踪.md"
    for uid, description, specification, groups, test_refs in rows(read(root, source), r"\| U\d{2} \|"):
        group_ids = re.findall(r"A\d{2}", groups)
        mapped = [t for t, v in tasks.items() if v["group"] in group_ids]
        requirements[uid] = dict(id=uid, description=description, specification=specification,
                                 tasks=mapped, tests=ids(test_refs, "TEST"), source=source)
        for t in mapped:
            tasks[t]["requirements"].append(uid)
    # Task-specific U references refine group maps and avoid losing cross-group requirements.
    for t, task in tasks.items():
        body = read(root, task["source"]).split(f"## {t} ", 1)[1].split("\n## ", 1)[0]
        for u in re.findall(r"\bU\d{2}\b", body):
            if u in requirements and t not in requirements[u]["tasks"]:
                requirements[u]["tasks"].append(t)
                task["requirements"].append(u)
    source = "docs/product/产品定义.md"
    for sid, expected, refs in rows(read(root, source), r"\| S\d{2} \|"):
        check(sid, "success", expected + "；直接证据：" + refs, source, "产品验收负责人")
        successes[sid] = dict(id=sid, tests=ids(refs, "TEST"))

    source = "docs/decisions/技术决策.md"
    decision_tasks = {
        "DEC-001": [14, 16], "DEC-002": [15, 29], "DEC-003": [3, 4],
        "DEC-004": [11, 22], "DEC-005": [23, 29], "DEC-006": [29],
    }
    for heading, outcome, evidence, scope in rows(read(root, source), r"\| DEC-\d{3} "):
        key, question = heading.split(maxsplit=1)
        if key not in decision_tasks or key in decisions:
            raise ValueError(f"Unknown or duplicate decision ID: {key}")
        expected = "；".join((question, outcome, evidence, scope))
        check(key, "decision", expected + "；证据须含负责人决定或用户确认原文与日期，不能把建议当已确认。", source, "产品/技术负责人")
        decisions[key] = dict(id=key, tasks=[f"TASK-{n:03}" for n in decision_tasks[key]])
    risk_tasks = [[1, 15], [2, 7, 8, 17, 18], [15], [2, 7, 8], [19, 20, 22],
                  [17, 18], [10, 28], [17, 21, 22], [20, 21], [5], [1, 15, 27]]
    for row, affected in zip(rows(read(root, source), r"\| R\d{2} \|"), risk_tasks):
        key = row[0]
        check(key, "risk", "；".join(row[1:]) + "；核验退出条件及剩余风险，保留验证证据。", source, "对应风险负责人")
        risks[key] = dict(id=key, tasks=[f"TASK-{n:03}" for n in affected])
    check("RELEASE.REVIEW", "release", "产品负责人确认用户体验、模型覆盖、建议默认值、范围与安装包；签署当前基线及产物哈希的发布结论。", "docs/product/产品定义.md", "产品负责人")
    return dict(schema_version=1, tasks=tasks, tests=tests, requirements=requirements,
                successes=successes, decisions=decisions, risks=risks, checks=checks)
