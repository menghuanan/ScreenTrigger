"""Verify the candidate, its installed contents, previous roots and the selected shortcut."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from candidate import save, sha


def read_json(path):
    return json.loads(path.read_text("utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive","program","prior_files","profile","output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    receipt = read_json(args.program/"installation.json")
    version = args.program/"versions"/receipt["versions"][-1]
    raw = (version/"package.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == receipt["version"]
    manifest = json.loads(raw)
    files = manifest["files"]
    paths = {item["path"] for item in files}
    assert len(paths)==len(files)
    with zipfile.ZipFile(args.archive) as archive:
        assert archive.read("package.json")==raw
        assert set(archive.namelist())=={"Install.ps1","package.json"}|{"app/"+p for p in paths}
    for item in files:
        path = version/item["path"]
        assert path.resolve().is_relative_to(version.resolve())
        assert path.stat().st_size==item["bytes"] and sha(path)==item["sha256"],path
    required = ["ScreenTrigger.exe","screen_trigger_lib.dll","Microsoft.UI.Xaml.dll","coreclr.dll",
                "resources/python/python.exe","resources/python/LICENSE.txt",
                "resources/manifest/release-cp313-win-x64.txt","resources/licenses/VC/vc_redist.x64.exe"]
    installed_names = {p.casefold() for p in paths}
    assert all(p.casefold() in installed_names for p in required)
    weights = read_json(version/"resources/manifest/models.catalog.json")
    engines = {e[field] for e in weights["engines"] for field in ("detection_model","recognition_model")}
    checked_models = {}
    for entry in weights["models"]:
        if entry["model_id"] not in engines:
            continue
        model = version/"resources/weights"/entry["model_id"]
        checked_models[entry["model_id"]] = {f["name"]:sha(model/f["name"]) for f in entry["files"]}
        assert all(checked_models[entry["model_id"]][f["name"]]==f["sha256"] for f in entry["files"])
    assert len(checked_models) == 4
    roots = {}
    transient_locks = []
    for old in read_json(args.prior_files):
        original = Path(old["Path"])
        assert original.is_relative_to(args.profile.resolve())
        assert sha(original)==old["Hash"].lower()
        name,*parts = original.relative_to(args.profile.resolve()).parts
        if parts in (["catalog", "engine-session.lock"], ["catalog", "writer.lock"]):
            transient_locks.append(dict(root=name, path="/".join(parts), sha256=old["Hash"].lower()))
            continue
        if name not in roots:
            backups = list(args.profile.glob(name+".pre-upgrade-*"))
            assert backups,name
            roots[name]=dict(files=0,backup_dirs=[p.name for p in backups])
        for backup in roots[name]["backup_dirs"]:
            assert sha(args.profile.joinpath(backup,*parts))==old["Hash"].lower()
        roots[name]["files"]+=1
    assert len(roots)==2
    pins=(version/"resources/manifest/release-cp313-win-x64.txt").read_text("utf8").splitlines()
    pins=[s.split()[0] for s in pins if "==" in s]
    assert len(pins)==73
    third_party = sorted({p.split("/")[2] for p in paths if p.startswith("resources/licenses/")})
    report=dict(archive=str(args.archive),sha256=sha(args.archive),archive_bytes=args.archive.stat().st_size,
        package_manifest=receipt["version"],installed_bytes=sum(f["bytes"] for f in files),
        verified_files=len(files),installed_version=str(version.resolve()),entry=manifest["entry"],
        data_root=receipt["dataRoot"],wheel_pins=pins,third_party_notices=third_party,
        models=checked_models,prior_roots=roots,transient_locks=transient_locks,
        active_root_matches=(version/"data-root.txt").read_text("utf8")==receipt["dataRoot"])
    assert report["active_root_matches"]
    save(args.output,report)
    print(f"Verified {len(files)} files, {len(engines)} models, {len(pins)} wheels and both previous user data roots.")


if __name__=="__main__":
    main()
