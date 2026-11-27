"""Rebuild portable WinUI resource maps from Chinese and language-neutral SDK resources."""
from collections import Counter
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


def makepri_tool(nuget, lockfile):
    packages = next(iter(json.loads(lockfile.read_text("utf-8-sig"))["dependencies"].values()))
    version = packages["Microsoft.Windows.SDK.BuildTools"]["resolved"]
    tool, = (nuget / "microsoft.windows.sdk.buildtools" / version / "bin").glob("*/x64/makepri.exe")
    return tool.resolve()


def run(tool, *args):
    result = subprocess.run([tool, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(result.stdout.decode("utf-16-le", errors="replace"))


def select_chinese(tree):
    for resource in tree.iter("NamedResource"):
        for candidate in resource.findall("Candidate"):
            language = candidate.find("./QualifierSet/Qualifier[@name='Language']")
            value = candidate.findtext("Value", "").replace("\\", "/").lower()
            if ((language is not None and language.get("value", "").lower() not in {"zh", "zh-hans", "zh-cn"})
                    or (candidate.get("type") == "Path" and value == "microsoft.ui.xaml/assets/map.html")):
                resource.remove(candidate)
            elif language is not None:
                language.set("scoreAsDefault", "1.0")


def contents(tree):
    """Compare URI, qualifiers and actual string, file path or embedded XBF bytes."""
    result = Counter()
    for resource in tree.iter("NamedResource"):
        for candidate in resource.findall("Candidate"):
            qualifiers = tuple(sorted((q.get("name"), q.get("value")) for q in candidate.iter("Qualifier")))
            result[resource.get("uri"), candidate.get("type"), qualifiers,
                   candidate.findtext("Value"), candidate.findtext("Base64Value")] += 1
    return result


def compact_pri(tool, source, dest):
    # The SDK recognizes a detailed resource dump via the .pri.xml suffix.
    with TemporaryDirectory(dir=dest.parent) as tmp:
        work = Path(tmp).resolve()
        dump = work / "selected.pri.xml"
        run(tool, "dump", "/if", source.resolve(), "/of", dump, "/dt", "detailed")
        tree = ET.parse(dump)
        name = tree.getroot().find("ResourceMap").attrib["name"]
        select_chinese(tree)
        selected = contents(tree)
        if not selected:
            raise ValueError(f"No Chinese or neutral resources in {source.name}")
        tree.write(dump, encoding="utf-8", xml_declaration=True)
        config = work / "config.xml"
        config.write_text(
            '<resources targetOsVersion="10.0.0" majorVersion="1"><index root="" startIndexAt="selected.pri.xml">'
            '<default><qualifier name="Language" value="zh-CN"/></default>'
            '<indexer-config type="PRIINFO" emitStrings="true" emitPaths="true" emitEmbeddedData="true"/>'
            '</index></resources>', encoding="utf-8")
        run(tool, "new", "/pr", work, "/cf", config, "/of", dest.resolve(), "/in", name)
        verified = work / "verified.xml"
        run(tool, "dump", "/if", dest.resolve(), "/of", verified, "/dt", "detailed")
        if contents(ET.parse(verified)) != selected:
            raise ValueError(f"Rebuilt resource map differs from its selected resources: {source.name}")
