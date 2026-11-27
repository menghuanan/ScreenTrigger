import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from portable_layout import ENTRY, MARKER, METADATA, NATIVE_HOST, UI, host_path, metadata_dir
from portable_payload import prune_wheels, stage_ui, unused_cpython, unused_ui
from portable_pri import contents, select_chinese


class PortablePayloadTests(unittest.TestCase):
    def test_archive_uses_private_dependencies_and_both_marker_formats(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            market = root / "publish"
            market.mkdir()
            app = root / "app"
            self.assertEqual(metadata_dir(app), app)
            self.assertEqual(host_path(app), app / NATIVE_HOST)
            files = [Path(name) for name in (ENTRY, "portable/" + ENTRY, NATIVE_HOST,
                     "ScreenTrigger.deps.json", "zh-CN/example.mui", "ar-SA/example.mui", "createdump.exe")]
            for path in files:
                dest = market / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(str(path).encode())
            deps = {"targets": {"win-x64": {"framework": {"native": {"dir/createdump.exe": {}, NATIVE_HOST: {}}}}}}
            (market / "ScreenTrigger.deps.json").write_text(json.dumps(deps))
            stage_ui(market, files, app, None)
            self.assertEqual((app / ENTRY).read_bytes(), (market / "portable" / ENTRY).read_bytes())
            self.assertEqual(set(p.name for p in app.iterdir()), {ENTRY, "resources"})
            self.assertTrue((app / UI / "zh-CN/example.mui").is_file())
            self.assertFalse((app / UI / "ar-SA").exists())
            self.assertEqual(metadata_dir(app), app / METADATA)
            self.assertEqual(host_path(app), app / UI / NATIVE_HOST)
            self.assertFalse((app / METADATA / MARKER).is_file())
            clean = json.loads((app / UI / "ScreenTrigger.deps.json").read_bytes())
            self.assertEqual(set(clean["targets"]["win-x64"]["framework"]["native"]), {NATIVE_HOST})

    def test_python_runtime_and_licenses_survive_development_filter(self):
        with TemporaryDirectory() as tmp:
            site = Path(tmp)
            keep = {"numpy/testing/__init__.py", "pandas/_testing/__init__.py", "lupa/lua54.cp313.pyd",
                    "demo/LICENSE.h", "paddle/libs/runtime.dll"}
            remove = {"numpy/linalg/tests/test_calc.py", "lupa/lua51.cp313.pyd", "demo/include.h", "bin/tool.exe"}
            for name in keep | remove:
                dest = site / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.touch()
            prune_wheels(site)
            self.assertEqual({p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()}, keep)
            self.assertTrue(unused_cpython(("Lib", "ensurepip", "__init__.py")))
            self.assertFalse(unused_cpython(("Lib", "ctypes", "__init__.py")))
            self.assertFalse(unused_ui(Path("Microsoft.UI.Xaml.Phone.dll")))

    def test_pri_retains_neutral_binary_and_chinese_value(self):
        tree = ET.fromstring('<ResourceMap><NamedResource uri="item"><Candidate type="EmbeddedData">'
                             '<Base64Value>WEJG</Base64Value></Candidate></NamedResource></ResourceMap>')
        resource = tree.find("NamedResource")
        for code in ("AR-SA", "ZH-CN"):
            candidate = ET.SubElement(resource, "Candidate", type="String")
            qualifiers = ET.SubElement(candidate, "QualifierSet")
            ET.SubElement(qualifiers, "Qualifier", name="Language", value=code)
            ET.SubElement(candidate, "Value").text = code
        select_chinese(tree)
        self.assertEqual(len(contents(tree)), 2)
        self.assertEqual(resource.find(".//Qualifier").get("scoreAsDefault"), "1.0")
        self.assertEqual(resource.find(".//Value").text, "ZH-CN")
        self.assertEqual(resource.find(".//Base64Value").text, "WEJG")
