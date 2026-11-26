"""Acceptance catalogue for the automation extension, independent of the RC16 catalogue."""
from graphlib import TopologicalSorter
import re

from check_model import ids, read


VERSION = "automation-v2"
ENTRY = "docs/agent/自动化扩展-开发入口.md"
INPUT = "docs/agent/自动化扩展-坐标录制与输入.md"
VISION = "docs/agent/自动化扩展-文字导出与视觉算法.md"
MEDIA = "docs/agent/自动化扩展-模型启动与离线发行.md"
PROCEDURE = "docs/agent/检查与闭环.md"
HANDOFF = "提交实施范围、实际入口、工程取值与依据、命令、环境、逐项预期及实际、原始记录哈希和审查者；复验原失败用例与相关回归，列出限制及剩余问题。"


def table(text, first_column):
    """Read one named specification table. A malformed or missing table is an error."""
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if line.startswith(f"| {first_column} |")]
    if len(headers) != 1:
        raise ValueError(f"Expected one table headed {first_column!r}")
    index = headers[0]
    width = len(lines[index].strip("|").split("|"))
    if index + 1 >= len(lines) or not re.fullmatch(r"[| :\-]+", lines[index + 1]):
        raise ValueError(f"Missing separator in {first_column}")
    result = {}
    for line in lines[index + 2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != width or not all(cells) or cells[0] in result:
            raise ValueError(f"Malformed or duplicate row in {first_column}: {line}")
        result[cells[0]] = cells[1:]
    if not result:
        raise ValueError(f"Empty table: {first_column}")
    return result


def section(text, heading):
    start = re.search(rf"^(#+) {re.escape(heading)}$", text, re.M)
    if not start:
        raise ValueError(f"Missing heading: {heading}")
    body = text[start.end():]
    end = re.search(rf"^#{{1,{len(start[1])}}} ", body, re.M)
    return body[:end.start() if end else None].strip()


def numbered_rows(text, heading, numbers):
    entries = table(text, heading)
    if set(entries) != {f"{n:03}" for n in numbers}:
        raise ValueError(f"Unexpected IDs in {heading}: {list(entries)}")
    return entries


def build(root):
    documents = {path: read(root, path) for path in (ENTRY, INPUT, VISION, MEDIA)}
    entry = documents[ENTRY]
    tasks, tests, checks = {}, {}, {}
    for number, (title, dependencies, expected, group) in numbered_rows(entry, "任务号", range(30, 45)).items():
        tid = f"TASK-{number}"
        if dependencies != "无" and not re.fullmatch(r"\d{3}(?:、\d{3})*", dependencies):
            raise ValueError(f"Invalid dependencies for {tid}: {dependencies}")
        prerequisites = [] if dependencies == "无" else [f"TASK-{d}" for d in dependencies.split("、")]
        tasks[tid] = dict(id=tid, title=title, stage=VERSION, group=VERSION,
                          dependencies=prerequisites, source=ENTRY, checks=[], tests=[f"TEST-{group}"],
                          requirements=[], deliverable=expected)
    for number, (title, old_groups) in numbered_rows(entry, "测试组号", range(17, 25)).items():
        gid = f"TEST-{number}"
        tests[gid] = dict(id=gid, title=title, source=ENTRY, checks=[],
                          regression_groups=ids(old_groups, "TEST"))
    for item in tasks.values():
        if set(item["dependencies"]) - tasks.keys() or set(item["tests"]) - tests.keys():
            raise ValueError(f"Unregistered task or test dependency: {item['id']}")
        if len(set(item["dependencies"])) != len(item["dependencies"]):
            raise ValueError(f"Duplicate dependency: {item['id']}")
    tuple(TopologicalSorter({tid: item["dependencies"] for tid, item in tasks.items()}).static_order())

    def add(owner, suffix, expected, source=ENTRY, *, package=False, affects=()):
        item = tasks.get(owner) or tests.get(owner)
        key = f"{owner}.{suffix}"
        if key in checks or not expected.strip():
            raise ValueError(f"Duplicate or empty check: {key}")
        is_task = owner in tasks
        kind = "task" if is_task else "acceptance"
        checks[key] = dict(id=key, kind=kind, expected=expected, source=source, owner=owner,
                           allow_na=False, requires_artifact=package)
        checks[key]["task" if is_task else "test"] = owner
        if affects:
            if set(affects) - tasks.keys():
                raise ValueError(f"Unregistered affected task: {key}")
            checks[key]["affected_tasks"] = list(affects)
        item["checks"].append(key)

    def cases(owner, suffix, source, cases, *, package=False, affects=()):
        for i, expected in enumerate(cases, 1):
            add(owner, f"{suffix}.{i:02}", expected, source, package=package, affects=affects)

    add("TASK-030", "CATALOG", "显式选版得到本表的任务、测试及硬依赖，账本、候选包和报告各自归属对应版本；验证首版默认命令。")
    add("TASK-030", "ISOLATION", "缺本卡证据、引用旧包回执及沿用旧问题处置或豁免均不能通过新版单卡；保留负向实测。")
    add("TASK-030", "FIXTURES", "生成可溯源的 RC16 旧 Lua、旧配置、安装版数据副本和可运行的专窗测试入口。")
    add("TASK-030", "RC16", "对保留的原源码与原包重新执行旧版 release 审计，核对 RC16 源码基线、包 SHA-256 和全部 29 卡签核。")
    add("TASK-030", "HANDOFF", "开发手册给出可运行的新版 baseline、单卡、测试组、release 命令。核对文档检查、工具回归、基线既有问题和原始记录。")
    for tid, item in tasks.items():
        if tid == "TASK-030":
            continue
        package = tid in ("TASK-039", "TASK-040", "TASK-044")
        clauses = [s for s in re.split(r"[；。]", item["deliverable"]) if s.strip()]
        cases(tid, "VERIFY", ENTRY, clauses, package=package)
        add(tid, "HANDOFF", HANDOFF, PROCEDURE)

    # Each group has its own evidence. Old group identifiers are regression references only.
    for gid, group in tests.items():
        regressions = "、".join(group["regression_groups"])
        add(gid, "REGRESSION", f"在新版实测「{group['title']}」，复验相关旧行为并引用本次实测原始记录。回归入口 {regressions}。")

    geometry = [" → ".join([given, *expected]) for given, expected in table(documents[INPUT], "已知输入").items()]
    cases("TEST-017", "GEOMETRY", INPUT, geometry, affects=("TASK-031",))
    cases("TASK-031", "GEOMETRY", INPUT, geometry)
    input_groups = {
        "TEST-001、TEST-005": ("TEST-017", "PICK"),
        "TEST-005、TEST-006": ("TEST-017", "REPLAY"),
        "TEST-005、TEST-007、TEST-008": ("TEST-018", "RECORD"),
        "TEST-006、TEST-008": ("TEST-019", "BACKGROUND"),
        "TEST-005、TEST-008、TEST-015": ("TEST-020", "POLICY"),
    }
    input_rows = table(documents[INPUT], "回归位置")
    if input_rows.keys() != input_groups.keys():
        raise ValueError("Input regression rows and test group mapping differ")
    for row, (gid, suffix) in input_groups.items():
        add(gid, suffix, input_rows[row][0], INPUT)
    add("TASK-032", "PICK", input_rows["TEST-001、TEST-005"][0], INPUT)
    scope_rules = table(documents[INPUT], "录制范围")
    cases("TASK-033", "SCOPE", INPUT, (" ".join([label, *rules]) for label, rules in scope_rules.items()))
    add("TASK-034", "PLAYBACK", input_rows["TEST-005、TEST-007、TEST-008"][0], INPUT)
    add("TASK-035", "POLICY", input_rows["TEST-005、TEST-008、TEST-015"][0], INPUT)
    add("TASK-036", "CAPTURE", "在切换到另一前台窗口及完全遮挡两种状态，对照多个实时目标帧号；最小化、关闭或捕获失败时不得交付旧帧。", INPUT)
    add("TASK-037", "BACKGROUND", input_rows["TEST-006、TEST-008"][0], INPUT)

    vision_groups = {
        "导出字符": ("TEST-021", "ENCODING"), "横排顺序": ("TEST-021", "ORDER"),
        "写入可靠性": ("TEST-021", "RECOVERY"), "文字监控": ("TEST-021", "MONITOR"),
        "四语 OCR 质量": ("TEST-021", "OCR"), "ORB 数据与质量": ("TEST-023", "ORB"),
        "YOLO 契约": ("TEST-023", "CONTRACT"), "性能与离线": ("TEST-023", "OFFLINE"),
        "回归旧功能": ("TEST-023", "COMPAT"),
    }
    vision_rows = table(documents[VISION], "用例")
    if vision_rows.keys() != vision_groups.keys():
        raise ValueError("Vision acceptance rows and test group mapping differ")
    vision_owners = {"ORB": ("TASK-042",), "CONTRACT": ("TASK-043",),
                     "OFFLINE": ("TASK-038", "TASK-039", "TASK-042"),
                     "COMPAT": ("TASK-039", "TASK-042")}
    for row, (gid, suffix) in vision_groups.items():
        add(gid, suffix, vision_rows[row][0], VISION, affects=vision_owners.get(suffix, ()),
            package=row in ("四语 OCR 质量", "YOLO 契约", "性能与离线", "回归旧功能"))
        if gid == "TEST-021" and suffix != "OCR":
            add("TASK-041", suffix, vision_rows[row][0], VISION)
    add("TASK-042", "ORB", vision_rows["ORB 数据与质量"][0], VISION)
    vectors = table(documents[VISION], "合成向量")
    vector_cases = [" ".join([label, *result]) for label, result in vectors.items()]
    scoring = section(documents[VISION], "ORB 留出集计分").splitlines()
    cases("TASK-043", "VECTOR", VISION, vector_cases)
    cases("TEST-023", "VECTOR", VISION, vector_cases, affects=("TASK-043",))
    cases("TASK-042", "SCORING", VISION, scoring)
    cases("TEST-023", "SCORING", VISION, scoring, affects=("TASK-042",))

    media_rows = table(documents[MEDIA], "用例")
    media_owners = {"介质与离线": ("TASK-039",), "启动时钟": ("TASK-040",), "脚本夹具": ("TASK-040",),
                   "冷热样本": ("TASK-040",), "性能影响": ("TASK-038", "TASK-040"),
                   "能力一致性": ("TASK-038",), "模型变更": ("TASK-038",), "数据恢复": ("TASK-039",)}
    for i, (case, result) in enumerate(media_rows.items(), 1):
        add("TEST-022", f"MEDIA.{i:02}", " ".join([case, *result]), MEDIA,
            package=True, affects=media_owners[case])
    add("TASK-038", "MODEL", media_rows["模型变更"][0], MEDIA)
    add("TASK-038", "CAPABILITIES", media_rows["能力一致性"][0], MEDIA)
    portable = table(documents[MEDIA], "情形")
    cases("TASK-039", "PORTABLE", MEDIA, (" ".join([case, *result]) for case, result in portable.items()), package=True)
    sampling = section(documents[MEDIA], "验收与原始记录").split("\n|", 1)[0]
    samples = [line for line in sampling.splitlines() if line.strip()]
    cases("TASK-040", "TIMING", MEDIA, samples, package=True)
    cases("TEST-022", "TIMING", MEDIA, samples, package=True, affects=("TASK-040",))
    integration = [line[2:] for line in section(entry, "专项验收与集成退出").splitlines() if line.startswith("- ")]
    if len(integration) < 3:
        raise ValueError("Missing final package integration scenarios")
    cases("TASK-044", "FLOW", ENTRY, integration, package=True)
    cases("TEST-024", "FLOW", ENTRY, integration, package=True, affects=("TASK-044",))

    key = "RELEASE.REVIEW"
    checks[key] = dict(id=key, kind="release", expected="确认全部本版任务、测试组与同一最终便携包的复验，签署该源码基线与介质 SHA-256 的独立发布结论。",
                       source=ENTRY, owner="发布验收负责人", allow_na=False, requires_artifact=True)
    return dict(schema_version=1, version=VERSION, tasks=tasks, tests=tests, checks=checks,
                requirements={}, successes={}, decisions={}, risks={})
