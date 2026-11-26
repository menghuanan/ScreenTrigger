"""Run existing component assertions against the installed backend, grouped by acceptance scenario."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from candidate import ROOT, resources, save, sha

GROUPS = {
    "TEST-001": ["capture_commands","capture_service","capture_qa_boundaries","image_import","import_qa_boundaries"],
    "TEST-002": ["lua_vm_real","vision_ocr_models","vision_ocr_degrade"],
    "TEST-003": ["targets_domain","targets_repository","targets_service","target_commands"],
    "TEST-004": ["vision_condition_evaluator","vision_providers_color","vision_providers_template","vision_providers_contour","vision_ocr_provider","vision_textmatch"],
    "TEST-005": ["input_proxy","script_hostapi"],
    "TEST-006": ["frame_registration","capture_gdi_flag"],
    "TEST-007": ["execution_runtime","script_commands"],
    "TEST-008": ["lua_cancel","vision_worker_cancel","vision_jobs","inference_lifecycle"],
    "TEST-009": ["debug_service"],
    "TEST-010": ["json_bridge","script_hostapi","scripts_contract"],
    "TEST-011": ["monitor_scheduler"],
    "TEST-012": ["catalog_backup","storage_fsops_lock","targets_repository","config_service","sqlite_adapter"],
    "TEST-013": ["history_store","config_service"],
    "TEST-014": ["vision_ocr_degrade"],
    "TEST-015": ["vision_cache","vision_isolation","monitor_scheduler"],
    "TEST-016": ["envelope","ipc_contract","p0_wire_boundaries","types_contract"],
}


def bind_external_tests():
    # A spawned interpreter inherits the mutated sys.path of the current test.
    # Legacy fixtures add the checkout root to find helper modules. Remove it
    # before importing even backend.main in the candidate identity check.
    checkout = ROOT.resolve()
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != checkout]
    installed_backend = Path(sys.executable).resolve().parents[1] / "backend"
    import backend
    backend.__path__ = [str(installed_backend)]
    install = resources()
    # Some legacy test modules prepend the checkout to sys.path. Freeze this
    # namespace package's product path before loading their external fixtures.
    backend.__path__ = [str(install / "backend")]
    tests = ROOT / "backend/tests"
    spec = importlib.util.spec_from_file_location("backend.tests", tests/"__init__.py",
                                                  submodule_search_locations=[str(tests)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["backend.tests"] = package
    backend.tests = package
    spec.loader.exec_module(package)
    return install, tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--modules", nargs="+")
    options = parser.parse_args()
    output = options.output
    output.mkdir(exist_ok=False)
    install, tests = bind_external_tests()
    by_module = {}
    modules = options.modules or sorted({name for names in GROUPS.values() for name in names})
    for module in modules:
        suite = unittest.defaultTestLoader.loadTestsFromName("backend.tests.test_"+module)
        with (output/(module+".log")).open("x",encoding="utf8") as stream:
            result = unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
        by_module[module] = dict(cases=result.testsRun,failed=[test.id() for test,_ in result.failures],
            errors=[test.id() for test,_ in result.errors],skipped=[(test.id(),why) for test,why in result.skipped],
            fixture=sha(tests/("test_"+module+".py")),log_sha256=sha(output/(module+".log")))
    unexpected = [name for name, mod in sys.modules.items() if (name=="backend" or name.startswith("backend."))
                  and not name.startswith("backend.tests") and getattr(mod,"__file__",None)
                  and not Path(mod.__file__).resolve().is_relative_to(install)]
    tested = {group:[n for n in names if n in by_module] for group,names in GROUPS.items()}
    save(output/"metrics.json",dict(manifest=sha(install.parent/"package.json"),groups=tested,modules=by_module,
                                   unexpected_backend_modules=unexpected))
    failures = sum(len(m["failed"])+len(m["errors"]) for m in by_module.values())
    print(json.dumps(dict(cases=sum(m["cases"] for m in by_module.values()),failed_or_error=failures,
                        skipped=sum(len(m["skipped"]) for m in by_module.values()),unexpected=unexpected)))
    return int(bool(failures or unexpected))


if __name__ == "__mp_main__":
    # Windows spawn must resolve the same fixture factory inside a provider child.
    bind_external_tests()
elif __name__=="__main__":
    raise SystemExit(main())
