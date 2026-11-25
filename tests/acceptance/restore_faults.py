"""Inject filesystem errors and abrupt process exit into the installed recovery implementation."""
import argparse
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from candidate import resources, save, sha
from backend.capture.ports import RawImage
from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.storage import backup
from backend.storage.catalog import Catalog
from backend.storage.errors import StorageError
from backend.storage.lock import engine_session
from backend.storage.paths import DataRoot
from backend.targets.repository import Repository, CatalogChange


def seed(root, name, color, watermark):
    images=SessionImageStore(name)
    repo, targets, scripts=build_catalog_services(SidecarArgs(name,data_dir=str(root)),images)
    try:
        ref=images.put(RawImage(20,20,bytes(color)*400),"png")
        targets.create_target(dict(name=name,image_id=ref.image_id,anchor_condition_id="anchor",
            conditions=dict(op="all",children=[dict(id="anchor",type="template",params=dict(threshold=.9))])))
        scripts.save_script(name,"return "+str(watermark),[])
        repo.commit(CatalogChange(next_sample_no=watermark), repo.read_snapshot().generation)
    finally:
        repo.close()


def worker(root, ident, stage, mode):
    repo=Repository(str(root))
    service=backup.BackupService(repo)
    p=service.preview(ident)
    def stop():
        if mode=="crash":
            os._exit(77)
        code=errno.ENOSPC if mode=="disk-full" else errno.EACCES
        raise OSError(code,os.strerror(code))
    try:
        with engine_session(root), ExitStack() as stack:
            if stage=="asset":
                original=backup._copy
                def copy_asset(src, dst):
                    if dst.name=="asset":
                        if mode=="crash": original(src,dst)
                        stop()
                    return original(src,dst)
                stack.enter_context(patch.object(backup,"_copy",copy_asset))
            else:
                method={"generation":"write_generation","manifest":"write_manifest","pointer":"activate",
                        "after-pointer":"activate"}[stage]
                original=getattr(Catalog,method)
                def write(target,*args):
                    if stage=="after-pointer" or (mode=="crash" and stage!="pointer"):
                        original(target,*args)
                    stop()
                stack.enter_context(patch.object(Catalog,method,write))
            service.restore(ident,p["expected_current"],p["expected_backup"])
    except StorageError as exc:
        print(json.dumps(exc.to_dict(),ensure_ascii=False))
        return 1
    finally:
        repo.close()
    return 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("output",type=Path)
    p.add_argument("--worker",nargs=3,metavar=("BACKUP","STAGE","MODE"))
    args=p.parse_args()
    install=resources()
    if args.worker:
        return worker(args.output,*args.worker)
    out=args.output.resolve()
    out.mkdir(exist_ok=False)
    source=out/"recovery-source"
    seed(source,"备份目标",(11,45,223),12)
    src_repo=Repository(str(source))
    ident=backup.BackupService(src_repo).create()["backup_id"]
    src_repo.close()
    source_state=backup.verify_contents(DataRoot(str(source)))
    rows=[]
    cases=[(s,m) for s in ("asset","generation","manifest","pointer") for m in ("disk-full","permission","crash")]
    cases.append(("after-pointer","crash"))
    for stage,mode in cases:
        name=stage+"-"+mode
        root=out/name
        seed(root,"原目录目标",(200,31,82),48)
        shutil.copytree(source/"backups"/ident,root/"backups"/ident)
        previous=backup.verify_contents(DataRoot(str(root)))
        cmd=[sys.executable,"-I","-B","-X","utf8",__file__,str(root),"--worker",ident,stage,mode]
        proc=subprocess.run(cmd,capture_output=True,encoding="utf8",timeout=35)
        (out/(name+".out")).write_text(proc.stdout,"utf8")
        (out/(name+".err")).write_text(proc.stderr,"utf8")
        assert proc.returncode==(77 if mode=="crash" else 1),(name,proc.stderr)
        if mode!="crash": assert json.loads(proc.stdout)["code"]=="STORAGE_ERROR"
        with engine_session(root):
            now=backup.verify_contents(DataRoot(str(root)))
            expected=source_state if stage=="after-pointer" else previous
            assert now.snapshot.targets==expected.snapshot.targets and now.assets==expected.assets,name
            assert now.snapshot.next_sample_no==48
            assert (previous.pointer==now.pointer)==(stage!="after-pointer")
            repo=Repository(str(root))
            repo.commit(CatalogChange(next_sample_no=49),now.snapshot.generation)
            repo.close()
        rows.append(dict(stage=stage,fault=mode,exit_code=proc.returncode,before=previous.pointer,
                         reopened=now.pointer,next_sample_no=now.snapshot.next_sample_no,
                         output=sha(out/(name+".out")),stderr=sha(out/(name+".err"))))
    save(out/"metrics.json",dict(manifest=sha(install.parent/"package.json"),observations=rows))
    print("Verified",len(rows),"injected failures, restart integrity and new writes.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
