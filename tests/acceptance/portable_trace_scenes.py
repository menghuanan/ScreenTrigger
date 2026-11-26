"""Run released ABI operations in disposable kit copies and retain each root's process identity."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

HERE=Path(__file__).resolve().parent


def run(result,label,home,args,success=True):
    logs=home/'qa-logs';logs.mkdir(exist_ok=True)
    with (logs/(label+'.log')).open('xb') as stream:
        process=subprocess.Popen(list(map(str,args)),stdout=stream,stderr=subprocess.STDOUT)
        item={'label':label,'pid':process.pid,'home':str(home),'argv':list(map(str,args))}
        result.setdefault('scenes',[]).append(item)
        try:item['exit']=process.wait(180)
        except subprocess.TimeoutExpired:
            process.terminate();process.wait(15);raise
    if (item['exit']==0)!=success:raise RuntimeError(f'{label} exited {item["exit"]}. See {stream.name}')
    return item


def calibrate(app,output,result):
    from portable_media import attach
    if not (app/'portable-abi-test.exe').exists():attach(app)
    label='calibration-'+uuid4().hex[:8];folder=app/'qa-logs'/label
    item=run(result,label,app,[app/'portable-abi-test.exe','-I','-B','-X','utf8',HERE/'portable_abi.py',folder,'factory'])
    assert json.loads((folder/'result.json').read_bytes())['passed']
    result['windows'].append(item['pid'])
    shutil.copyfile(folder/'result.json',output/'factory.json')


@contextmanager
def mapped(directory):
    drive=next(c+':' for c in 'ZYXWVUTSRQP' if not Path(c+':/').exists())
    alias=Path(drive+'/')
    subprocess.run(['subst',drive,str(directory)],check=True)
    try:
        assert os.path.samefile(alias,directory)
        yield alias
    finally:
        if not os.path.samefile(alias,directory):raise RuntimeError('Drive mapping changed during the run')
        subprocess.run(['subst',drive,'/D'],check=True)


def recovery(app,output,result):
    copy=app.parent/(app.name+'-trace-'+uuid4().hex[:8])
    def abi(label,home,operation,executable_root=None,script_name='trace-old-script',success=True):
        folder=home/'qa-logs'/label
        exe=(executable_root or home)/'portable-abi-test.exe'
        run(result,label,home,[exe,'-I','-B','-X','utf8',HERE/'portable_abi.py',folder,operation,script_name],success)
        if (folder/'result.json').exists():shutil.copyfile(folder/'result.json',output/(label+'.json'))
    abi('saved-before-recovery',app,'save')
    shutil.copytree(app,copy,ignore=shutil.ignore_patterns('qa-logs','profile-telemetry'))
    abi('copied-reopened',copy,'load')
    with mapped(copy.parent) as parent:abi('changed-drive',copy,'load',parent/copy.name)
    with mapped(copy) as root:abi('direct-drive-root',copy,'load',root)
    abi('copy-only-write',copy,'save',script_name='trace-copy-only')
    assert (app/'data/catalog/current.json').read_bytes()!=(copy/'data/catalog/current.json').read_bytes()
    folder=copy/'qa-logs/holder'
    argv=[copy/'portable-abi-test.exe','-I','-B',HERE/'portable_abi.py',folder,'hold']
    with (copy/'qa-logs/holder.log').open('xb') as stream:
        holder=subprocess.Popen(list(map(str,argv)),stdout=stream,stderr=subprocess.STDOUT)
        item={'label':'holder','pid':holder.pid,'home':str(copy),'argv':list(map(str,argv))};result['scenes'].append(item)
        try:
            deadline=time.monotonic()+45
            while not (folder/'ready').exists():
                if holder.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Portable data lock was not acquired')
                time.sleep(.1)
            abi('second-writer-denied',copy,'save',script_name='forbidden',success=False)
        finally:
            folder.mkdir(exist_ok=True);(folder/'release').touch();item['exit']=holder.wait(20)
        assert item['exit']==0
    abi('reopened-after-lock',copy,'load')
    cache=copy/'data/cache'
    assert cache.resolve().is_relative_to(copy.resolve()) and cache.is_dir()
    subprocess.run(['icacls',str(cache),'/deny','*S-1-1-0:(WD)'],check=True)
    try:
        try:
            with (cache/'write-control.tmp').open('xb'):pass
        except PermissionError:pass
        else:raise AssertionError('Read-only control remained writable')
        abi('readonly-cache-denied',copy,'load',success=False)
    finally:
        subprocess.run(['icacls',str(cache),'/remove:d','*S-1-1-0'],check=True)
    abi('reopened-after-cache-repair',copy,'load')


def measure(app,output,result,operation,fixture):
    exe=app/'portable-abi-test.exe'
    if operation=='orb':
        data=fixture.resolve();row=json.loads((data/'manifest.json').read_bytes())['rows'][0]
        for item in (row['sample'],row['scene']):
            with (data/item['file']).open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==item['sha256']
        folder=app/'qa-logs/orb'
        run(result,'bound-orb',app,[exe,'-I','-B','-X','utf8',HERE/'orb_window.py',app/'screen_trigger_lib.dll',data,folder])
        shutil.copyfile(folder/'result.json',output/'bound-orb-result.json')
        shutil.copyfile(app/'qa-logs/bound-orb.log',output/'bound-orb.log')
    elif operation=='fixed_ocr':
        folder=app/'qa-logs/fixed-ocr'
        run(result,'cold-and-warm-fixed-ocr',app,[exe,'-I','-B','-X','utf8',HERE/'portable_trace_ocr.py',folder,fixture])
        shutil.copyfile(folder/'fixed-ocr.json',output/'fixed-ocr.json')
    elif operation=='recovery':recovery(app,output,result)
    else:raise ValueError(operation)
