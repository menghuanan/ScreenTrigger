"""Two independent native hosts on the same interactive desktop. No generated action sends input."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'tools/probes/windows'))
from verify_p1_runtime import Host,Fault

def worker(pipe,dll,folder):
    host=None;rid=execution=None
    try:
        folder.mkdir();host=Host(dll,folder);host.session=host.call('start')['session_id'];host.request('hello')
        disp=next(d['id'] for d in host.request('get_display_list')['displays'] if d['primary'])
        script=host.save('wait(25); return 1')
        pipe.send(['ready',host.session])
        while True:
            cmd=pipe.recv()
            try:
                if cmd=='exit':break
                if cmd=='crash':os._exit(44)
                if cmd=='record':
                    s=host.request('start_recording',context={'display_id':disp});rid=s['recording_id'];reply=s['status']
                elif cmd=='cancel':
                    reply=host.request('cancel_recording',recording_id=rid)['status'];rid=None
                elif cmd=='play':
                    execution=host.request('execute_script',script_id=script,display_id=disp)['execution_id']
                    until=time.monotonic()+8
                    while time.monotonic()<until:
                        state=host.state(execution)
                        if state['status'] in ('running','failed','cancelled'):break
                        time.sleep(.025)
                    reply=state.get('error',{}).get('code') if state['status']=='failed' else state['status']
                elif cmd=='stop':
                    host.request('stop_script',execution_id=execution);reply=host.until(execution,'cancelled')['status']
                elif cmd=='list':
                    reply=len(host.request('list_scripts',offset=0,limit=100)['items'])
                pipe.send(['ok',reply])
            except Fault as exc:pipe.send(['fault',exc.code])
    except Exception as exc:pipe.send(['error',str(exc)])
    finally:
        if host:host.close()

def run(dll,folder):
    folder.mkdir();clients=[];rows=[]
    def invoke(i,cmd,expected):
        c,p=clients[i];c.send(cmd);assert c.poll(10),(i,cmd)
        actual=c.recv();rows.append(dict(host=i,command=cmd,expected=expected,actual=actual))
        assert actual==expected,rows[-1]
    try:
        for i in (0,1):
            c,other=mp.Pipe();p=mp.Process(target=worker,args=(other,dll,folder/str(i)));p.start();clients.append((c,p))
            assert c.poll(55);state=c.recv();assert state[0]=='ready',state
        invoke(0,'record',['ok','recording'])
        invoke(1,'record',['fault','BUSY'])
        invoke(1,'list',['ok',1])
        c,p=clients[1];c.send('play');assert c.poll(10)
        result=c.recv();rows.append(dict(host=1,command='input while recording',actual=result,expected='BUSY'))
        assert result in (['ok','BUSY'],['fault','BUSY']),result
        invoke(0,'cancel',['ok','cancelled'])
        invoke(0,'play',['ok','running']);invoke(1,'play',['ok','running'])
        invoke(0,'stop',['ok','cancelled'])
        invoke(0,'record',['fault','BUSY'])
        invoke(1,'stop',['ok','cancelled'])
        invoke(0,'record',['ok','recording'])
        c,p=clients[0];c.send('crash');p.join(5);assert p.exitcode==44
        rows.append(dict(host=0,command='abnormal exit while recording',exit_code=p.exitcode))
        invoke(1,'record',['ok','recording']);invoke(1,'cancel',['ok','cancelled'])
    finally:
        for c,p in clients:
            if p.is_alive():c.send('exit')
        for c,p in clients:
            p.join(5)
            if p.is_alive():p.terminate();p.join()
        (folder/'observations.json').write_text(json.dumps(rows,indent=2),encoding='utf8')
if __name__=='__main__':run(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
