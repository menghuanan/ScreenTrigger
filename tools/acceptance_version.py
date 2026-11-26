"""Select the acceptance catalogue and its writable namespace."""
from pathlib import Path

import check_model


FIRST = "v1"
AUTOMATION = "automation-v2"
VERSIONS = (FIRST, AUTOMATION)
RC16_SHA256 = "2654243c6cb4d0f9c89b1fd609d1e7e6d94829259dda932abd4ca99c71ad0a10"


def catalog(root, version):
    if version == FIRST:
        return check_model.build(root)
    if version != AUTOMATION:
        raise ValueError(f"Unknown acceptance version: {version}")
    from automation_catalog import build
    return build(root)


def owned_path(root, directory, name):
    """Keep selected inputs/outputs in the named tree, including through junctions."""
    directory = root.resolve() / directory
    path = directory / name
    if directory.resolve() != directory or path.resolve() != path or not path.is_relative_to(directory):
        raise ValueError(f"Path outside version namespace: {path}")
    return path


def report_path(root, version, name):
    if version == FIRST:
        return root / "reports" / name
    return owned_path(root, f"reports/{version}", name)


def ledger_path(root, version):
    if version == FIRST:
        return root / "docs/agent/check-state.json"
    return report_path(root, version, "check-state.json")


def raw_ref(root, version, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return False
    folder = Path("reports") / version / "raw"
    path = Path(ref["path"])
    if path.is_absolute() or not path.is_relative_to(folder) or ".." in path.parts:
        return False
    owned_path(root, folder, path.relative_to(folder))
    return True


def candidate_ref(root, version, ref):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return False
    path = Path(ref["path"])
    if (path.parent != Path("dist") / version or not path.name.startswith(f"ScreenTrigger-{version}-")
            or path.suffix != ".zip" or ref.get("sha256") == RC16_SHA256):
        return False
    owned_path(root, path.parent, path.name)
    return True
