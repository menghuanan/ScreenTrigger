"""Exercise real disk, anonymous-pipe and named-pipe handles under the ordinary workload token."""
import ctypes
import msvcrt
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

PAYLOAD=b'ScreenTrigger trace handle control\n'


def file_type(handle):
    return ctypes.windll.kernel32.GetFileType(ctypes.c_void_p(handle))


def worker(action,value,ready=None):
    if action=='inherited':
        assert file_type(msvcrt.get_osfhandle(1))==int(value)
        os.write(1,PAYLOAD)
    elif action=='external':
        folder=Path(value)
        with (folder/'negative-control.bin').open('xb',buffering=0) as stream:
            stream.write(PAYLOAD);os.fsync(stream.fileno())
        # Ordinary creation, write, rename and deletion also exercise NTFS's
        # update of its own volume metadata. All files belong to this control.
        for index in range(96):
            path=folder/f'file-control-{index}.tmp'
            with path.open('xb',buffering=0) as stream:
                stream.write(bytes(4096));os.fsync(stream.fileno())
            renamed=path.with_suffix('.checked');path.rename(renamed);renamed.unlink()
    elif action=='named':
        with Listener(value,family='AF_PIPE') as server:
            Path(ready).touch(exist_ok=False)
            with server.accept() as pipe:
                assert file_type(pipe.fileno())==3 and pipe.poll(10) and pipe.recv_bytes()==PAYLOAD
    else:
        raise ValueError(action)


def exercise(output,result):
    args=[sys.executable,'-I','-B',str(Path(__file__).resolve())]
    with (output/'inherited-control.bin').open('xb') as stream:
        disk=subprocess.Popen([*args,'inherited','1'],stdout=stream)
        assert disk.wait(20)==0
    result['inherited_control']=disk.pid
    assert (output/'inherited-control.bin').read_bytes()==PAYLOAD
    pipe=subprocess.Popen([*args,'inherited','3'],stdout=subprocess.PIPE)
    assert file_type(msvcrt.get_osfhandle(pipe.stdout.fileno()))==3
    received,_=pipe.communicate(timeout=20)
    assert pipe.returncode==0 and received==PAYLOAD
    result['pipe_control']=pipe.pid
    result['pipe_transfer']={'reader_type':3,'writer_type':3,'payload_verified':True}
    address=r'\\.\pipe\ScreenTrigger-QA-'+uuid4().hex
    ready=output/'named-control-ready'
    named=subprocess.Popen([*args,'named',address,str(ready)]);result['named_pipe_control']=named.pid
    try:
        deadline=time.monotonic()+15
        while not ready.exists():
            if named.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Named pipe listener did not start')
            time.sleep(.05)
        with Client(address,family='AF_PIPE') as writer:
            assert file_type(writer.fileno())==3
            writer.send_bytes(PAYLOAD)
        assert named.wait(10)==0
    finally:
        if named.poll() is None:named.terminate();named.wait(10)
    result['named_pipe_transfer']={'address':address,'reader_type':3,'writer_type':3,'payload_verified':True}
    external=subprocess.Popen([*args,'external',str(output)])
    assert external.wait(45)==0 and (output/'negative-control.bin').read_bytes()==PAYLOAD
    result['external_control']=external.pid


if __name__=='__main__':worker(*sys.argv[1:])
