"""Verify the documented backup CLI against the installed candidate and a private data root."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate import resources, save, sha
from backend.capture.ports import RawImage
from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.storage.backup import verify_contents
from backend.storage.lock import engine_session
from backend.storage.paths import DataRoot


def main():
    runtime = resources()
    out = Path(sys.argv[1]).resolve()
    out.mkdir(exist_ok=False)
    root = out / "data"
    images = SessionImageStore("backup-fixture")
    repo, targets, scripts = build_catalog_services(SidecarArgs("backup-fixture", data_dir=str(root)), images)
    ref = images.put(RawImage(20,20,bytes([0,80,160]) * 400), "png")
    def target(name):
        return targets.create_target(dict(name=name,image_id=ref.image_id,anchor_condition_id="m",
            conditions=dict(op="all",children=[dict(id="m",type="template",params=dict(threshold=.95))])))
    steps=[]
    def command(*args, error=None):
        cmd=[sys.executable,"-I","-B","-X","utf8","-m","backend.storage.backup","--data-dir",str(root),*args]
        done=subprocess.run(cmd,capture_output=True,encoding="utf8",timeout=45)
        prefix=out/f"attempt-{len(steps):02d}"
        prefix.with_suffix(".out").write_text(done.stdout,"utf8")
        prefix.with_suffix(".err").write_text(done.stderr,"utf8")
        reply=json.loads(done.stdout)
        steps.append(dict(command=cmd,exit_code=done.returncode,response=reply,expected_error=error))
        assert done.returncode==(1 if error else 0) and (not error or reply["code"]==error),steps[-1]
        return reply
    def restore(preview, error=None):
        return command("restore",preview["backup_id"],"--expected-current",preview["expected_current"],
                       "--expected-backup",preview["expected_backup"],error=error)
    try:
        first=target("第一份目标")
        scripts.save_script("源脚本","return 41+1",[])
        initial=verify_contents(DataRoot(str(root)))
        backup=command("create")["backup_id"]
        assert backup in command("list")["backups"]
        preview=command("preview",backup)
        second=target("备份后的写入")
        assert second["sample_no"]>first["sample_no"]
        before=sha(root/"catalog/current.json")
        restore(preview,"CONFLICT")
        assert sha(root/"catalog/current.json")==before
        with engine_session(root):
            busy = command("create",error="STORAGE_ERROR")
            assert busy["retryable"] and "数据目录正在使用" in busy["message"]
        result=restore(command("preview",backup))
        protected=verify_contents(DataRoot(str(root/"backups"/result["protection"])))
        restored=verify_contents(DataRoot(str(root)))
        assert protected.snapshot.targets[-1]["id"]==second["id"]
        assert restored.snapshot.targets==initial.snapshot.targets and restored.assets==initial.assets
        assert restored.snapshot.next_sample_no>second["sample_no"]
        repo.reload()
        after=target("损坏指针前的写入")
        (root/"catalog/current.json").write_bytes(b"damaged-but-preserved")
        damaged=command("preview",backup)
        assert damaged["current_state"]=="damaged"
        result=restore(damaged)
        assert (root/"backups"/result["protection"]).read_bytes()==b"damaged-but-preserved"
        assert verify_contents(DataRoot(str(root))).snapshot.next_sample_no>after["sample_no"]
        repo.reload()
        last=target("恢复后的新目标")
        assert last["sample_no"]>after["sample_no"]
        save(out/"metrics.json",dict(package_manifest=sha(runtime.parent/"package.json"),steps=steps,
            first=first["sample_no"],second=second["sample_no"],after=after["sample_no"],last=last["sample_no"],
            assets=initial.assets))
        print(f"Verified {len(steps)} installed CLI calls, original/protection contents, and sample numbers through {last['sample_no']}.")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
