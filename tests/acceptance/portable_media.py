"""Extract and verify the portable ZIP; attach a separate ABI driver only to a disposable test copy."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


class StandardInfo(ctypes.Structure):
    _fields_=[("allocation",ctypes.c_int64),("end_of_file",ctypes.c_int64),("links",ctypes.c_uint32),
              ("delete_pending",ctypes.c_byte),("directory",ctypes.c_byte)]


def allocated(path):
    import msvcrt
    from ctypes import wintypes
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    query=kernel.GetFileInformationByHandleEx
    query.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    query.restype=wintypes.BOOL
    with path.open("rb") as stream:
        info=StandardInfo()
        if not query(msvcrt.get_osfhandle(stream.fileno()),1,ctypes.byref(info),ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.allocation


def extract(archive,folder):
    folder.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(archive) as kit:
        seen=set()
        for item in kit.infolist():
            target=(folder/item.filename).resolve()
            if not target.is_relative_to(folder) or item.filename.casefold() in seen:
                raise ValueError(f"Unexpected or duplicate ZIP path {item.filename}")
            seen.add(item.filename.casefold())
            target.parent.mkdir(exist_ok=True,parents=True)
            with kit.open(item) as src,target.open("xb") as dst: shutil.copyfileobj(src,dst)
    marker=json.loads((folder/"ScreenTrigger.portable.json").read_bytes())
    assert marker["core_sha256"]==sha(folder/"portable-core.json")
    core=json.loads((folder/"portable-core.json").read_bytes())
    models=json.loads((folder/"resources/manifest/factory-models.json").read_bytes())
    checked=set()
    def verify(path,count,expected):
        assert path.is_file() and path.stat().st_size==count and sha(path)==expected,path
        checked.add(path.relative_to(folder).as_posix())
    for e in core: verify(folder/e["path"],e["bytes"],e["sha256"])
    for model in models["models"]:
        for e in model["files"]:
            verify(folder/"resources/weights"/model["model_id"]/e["name"],e["bytes"],e["sha256"])
    assert checked|{"portable-core.json","ScreenTrigger.portable.json"}=={p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert not (folder/"data-root.txt").exists()
    assert set(marker["factory"])=={e["recognition_model"] for e in models["engines"]}
    disk=logical=0
    for p in folder.rglob("*"):
        if p.is_file():
            disk+=allocated(p)
            logical+=p.stat().st_size
    print(json.dumps(dict(zip_sha256=sha(archive),zip_bytes=archive.stat().st_size,files=len(checked)+2,
        expanded=logical,on_disk=disk,source=marker["source_sha256"],core=marker["core_sha256"],
        selection=marker["factory"],languages=sorted({lang for e in models["engines"] for lang in e["languages"]}))))


def attach(app):
    # The application and its manifest remain byte-for-byte intact in this disposable root.
    python=app/"resources/python"
    for name,dest in (("python.exe","portable-abi-test.exe"),("python3.dll","python3.dll"),("python313.dll","python313.dll")):
        new=app/dest
        if new.exists():raise FileExistsError(new)
        shutil.copyfile(python/name,new)
    pth=app/"python313._pth"
    with pth.open("x",encoding="utf8") as stream:
        stream.write("resources/python/Lib\nresources/python/DLLs\nresources/python\nresources/python/Lib/site-packages\nresources\nimport site\n")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("attach").add_argument("folder",type=Path)
    unpack=sub.add_parser("extract")
    unpack.add_argument("archive",type=Path)
    unpack.add_argument("folder",type=Path)
    a=p.parse_args()
    if a.action=="extract": extract(a.archive.resolve(),a.folder.resolve())
    else: attach(a.folder.resolve())
