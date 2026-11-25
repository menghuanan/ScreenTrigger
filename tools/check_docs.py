"""Dependency-free documentation and audit-contract validator (Python 3.13)."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

from check_model import build, ids

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def anchors(text):
    result = set(re.findall(r'<a\s+id="([^"]+)"', text))
    counts = {}
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.M):
        slug = re.sub(r"[^\w\s\-\u0080-\uffff]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        result.add(slug if count == 0 else f"{slug}-{count}")
        counts[slug] = count + 1
    return result


def validate(root=ROOT):
    errors = []
    documents = [root / "README.md", root / "AGENTS.md", *sorted((root / "docs").rglob("*.md"))]
    # The immutable v1 originals keep their historical links and IDs. Validate the
    # newer archive's index and relocated references like other project documents.
    documents = [p for p in documents if not p.is_relative_to(root / "docs/archive/v1")]
    texts = {p: p.read_text(encoding="utf-8-sig") for p in documents}
    try:
        model = build(root)
    except (ValueError, KeyError, IndexError) as exc:
        return {"status": "FAIL", "errors": [f"Cannot parse check catalogue: {exc}"]}
    known = set(model["tasks"]) | set(model["tests"]) | set(model["checks"])
    known |= {f"{p}{i:02}" for p, count in (("A", 10), ("U", 19), ("S", 9), ("D", 12), ("R", 11)) for i in range(1, count + 1)}
    counts = {"documents": len(documents), "links": 0, "json_examples": 0,
              "archive_hashes": 0, "checks": len(model["checks"])}
    for path, text in texts.items():
        rel = path.relative_to(root).as_posix()
        for obsolete in re.findall(r"\bT(?:\d{2}|xx)\b", text):
            errors.append(f"{rel}: ambiguous legacy ID {obsolete}")
        if re.search(r"^#{1,6} .*\\n", text, re.M):
            errors.append(f"{rel}: literal newline in heading")
        for ref in re.findall(r"\b(?:(?:TASK|TEST)-\d{3}(?:\.[A-Z]+(?:\.\d{2})?)?|DEC-\d{3}|[AUSDR]\d{2})\b", text):
            if ref not in known:
                errors.append(f"{rel}: undefined typed reference {ref}")
        opened = None
        buffer = []
        for number, line in enumerate(text.splitlines(), 1):
            fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
            if fence:
                if opened is None:
                    opened = (fence[1], fence[2].strip(), number)
                    buffer = []
                elif fence[1][0] == opened[0][0] and len(fence[1]) >= len(opened[0]) and not fence[2].strip():
                    if opened[1] == "json":
                        counts["json_examples"] += 1
                        try:
                            json.loads("\n".join(buffer))
                        except ValueError as exc:
                            errors.append(f"{rel}:{opened[2]} invalid JSON: {exc}")
                    opened = None
                else:
                    buffer.append(line)
            elif opened:
                buffer.append(line)
        if opened:
            errors.append(f"{rel}:{opened[2]} unclosed fence")
        # Local Markdown links (including anchors) are checked, not code-like path examples.
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)", text):
            if re.match(r"[a-zA-Z][\w+.-]*:", target):
                continue
            target = unquote(target.strip("<>"))
            filename, _, fragment = target.partition("#")
            dest = (path.parent / filename).resolve() if filename else path.resolve()
            counts["links"] += 1
            if not dest.is_relative_to(root.resolve()) or not dest.is_file():
                errors.append(f"{rel}: missing/outside local link {target}")
            elif fragment and dest.suffix == ".md" and fragment not in anchors(dest.read_text(encoding="utf-8-sig")):
                errors.append(f"{rel}: missing anchor {target}")
    manifest = root / "docs/archive/v1/manifest.json"
    try:
        archived = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if len(archived) != 11:
            errors.append("Archive manifest must preserve all 11 original documents")
        for entry in archived:
            p = (manifest.parent / entry["path"]).resolve()
            if not p.is_relative_to(manifest.parent.resolve()) or not p.is_file() or digest(p) != entry["sha256"].lower():
                errors.append(f"Archive SHA mismatch: {entry['path']}")
            counts["archive_hashes"] += 1
    except (OSError, ValueError, KeyError) as exc:
        errors.append(f"Archive manifest invalid: {exc}")
    expected = {"tasks": 29, "tests": 16, "requirements": 19, "successes": 9, "decisions": 6, "risks": 11}
    for kind, count in expected.items():
        if len(model[kind]) != count:
            errors.append(f"Expected {count} {kind}, found {len(model[kind])}")
    index = texts[root / "docs/agent/任务拆解.md"]
    indexed = {}
    for line in index.splitlines():
        if re.match(r"\| TASK-\d{3} \|", line):
            columns = [v.strip() for v in line.split("|")]
            indexed[columns[1]] = ids(columns[3], "TASK")
    for tid, task in model["tasks"].items():
        if set(indexed.get(tid, [])) != set(task["dependencies"]):
            errors.append(f"{tid}: index/card dependency mismatch")
        if not task["requirements"] or not task["tests"] or not any(".OUTPUT." in c for c in task["checks"]):
            errors.append(f"{tid}: missing requirement, test mapping or output checks")
        for test in task["tests"]:
            if test not in model["tests"]:
                errors.append(f"{tid}: unknown test {test}")
    visited, visiting = set(), set()

    def visit(tid):
        if tid in visiting:
            errors.append(f"Dependency cycle: {tid}")
            return
        if tid in visited:
            return
        if tid not in model["tasks"]:
            errors.append(f"Unknown dependency: {tid}")
            return
        visiting.add(tid)
        for dep in model["tasks"][tid]["dependencies"]:
            visit(dep)
        visiting.remove(tid)
        visited.add(tid)
    for tid in model["tasks"]:
        visit(tid)
    for uid, requirement in model["requirements"].items():
        if not requirement["tasks"] or not requirement["tests"]:
            errors.append(f"{uid}: incomplete traceability")
    return {"status": "FAIL" if errors else "PASS", "counts": counts, "errors": errors,
            "limits": ["Static checks do not execute product code or verify visual rendering.",
                       "Passing documents is not task acceptance or release approval."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.root.resolve())
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
