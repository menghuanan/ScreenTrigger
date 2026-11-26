"""Review each process lifetime against its own portable root and Windows volume identity."""
from collections import Counter
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path


def volume(path):
    drive=path.drive
    device=ctypes.create_unicode_buffer(32768)
    filesystem=ctypes.create_unicode_buffer(256)
    serial=wintypes.DWORD()
    kernel=ctypes.windll.kernel32
    if not kernel.QueryDosDeviceW(drive,device,len(device)):
        raise ctypes.WinError()
    if not kernel.GetVolumeInformationW(drive+'\\',None,0,ctypes.byref(serial),None,None,filesystem,len(filesystem)):
        raise ctypes.WinError()
    return {'drive':drive,'device':device.value,'filesystem':filesystem.value,'serial':serial.value}


def native(name,vol):
    if name[:2].casefold()==vol['drive'].casefold() and name[2:3]=='\\':
        name=vol['device']+name[2:]
    return name.casefold()


def write_class(event,home,vol,control_names):
    name=native(event['path'],vol)
    if not name:return 'unresolved'
    if name.startswith(native(str(home),vol)+'\\'):return 'application'
    if name.startswith('\\device\\namedpipe\\'):return 'ipc'
    # MS-FSCC 5.3 identifies these two exact NTFS volume-root names. The
    # control must have reproduced this same, fully named keyed event.
    metadata={vol['device'].casefold()+'\\$mft',vol['device'].casefold()+'\\$logfile'}
    signature=(name,event['ioFlags'],event['extraFlags'])
    if (vol['filesystem'].upper()=='NTFS' and name in metadata and signature in control_names
            and event['id']==16 and event['ioFlags']==0x60043 and event['extraFlags']==0
            and event['size']>0 and event['size']%4096==0 and event['via'] in
            {'classic-final-live-name','classic-name-lifetime','file-name-key','open-file-object','payload'}):
        return 'ntfs_metadata'
    return 'external'


def classify(output,app,processes,events):
    final=events[-1];ready=next(e['rootPid'] for e in events if e['kind']=='ready')
    assert processes['observer_exit']==0 and final['kind']=='complete' and final['workload_finished'] and final['lost']==final['rundownLost']==0
    corrections={e['eventNumber']:e for e in events if e['kind']=='resolved'}
    def complete(e):
        corr=corrections.get(e['eventNumber']) if not e['path'] else None
        return {**e,'path':corr['path'],'via':corr['via']} if corr else e
    homes={p:app for p in processes['windows']}
    homes.update({s['pid']:Path(s['home']) for s in processes.get('scenes',[])})
    volumes={home:volume(home) for home in set(homes.values())}
    default_vol=volumes[app]
    control_roots={'inherited_control':'inherited','pipe_control':'pipe','named_pipe_control':'named_pipe','external_control':'outside'}
    control_pids={processes[k]:label for k,label in control_roots.items() if k in processes}
    pending=dict(homes);tracking={};active_controls={};starts=[];ends=[];package_records=[];controls=[];network=[]
    for e in events:
        pid=e.get('pid');kind=e['kind']
        if kind=='start':
            # Each ABI workload is a direct child of the normal orchestrator.
            # The WinUI PID was independently sampled from its launch helper.
            home=pending.pop(pid,None) if e['parent']==ready or pid==processes['windows'][0] else None
            home=home or tracking.get(e['parent'])
            if home:tracking[pid]=home;starts.append(pid)
            control=(control_pids.get(pid) if e['parent']==ready else None) or active_controls.get(e['parent'])
            if control:active_controls[pid]=control
        elif kind=='stop':
            if pid in tracking:tracking.pop(pid);ends.append(pid)
            active_controls.pop(pid,None)
        elif kind=='file':
            row=complete(e)
            if pid in tracking:package_records.append((tracking[pid],row))
            if pid==ready or pid in active_controls:controls.append((active_controls.get(pid,'orchestrator'),row))
        elif kind=='network' and pid in tracking:network.append(e)
    assert not tracking and not pending and Counter(starts)==Counter(ends),'Incomplete workload process lifetime'
    net_control=[e for e in events if e['kind']=='network' and e['pid']==ready]
    signatures={(native(r['path'],default_vol),r['ioFlags'],r['extraFlags']) for label,r in controls
                if label in ('orchestrator','outside') and r['id']==16 and r['path'] and r['size']>0 and r['size']%4096==0}
    disk_control=[r for _,r in controls if r['path'].lower().endswith('\\write-control.bin')]
    inherited=[r for label,r in controls if label=='inherited' and r['id']==16 and r['path'].lower().endswith('\\inherited-control.bin')]
    outside=[r for label,r in controls if label=='outside' and r['id']==16 and r['path'].lower().endswith('\\negative-control.bin')]
    rejected=[r for r in outside if write_class(r,app,default_vol,signatures)=='external']
    categories=Counter();folders=Counter();exceptions=[];metadata=[];examples={}
    for home,row in package_records:
        category=write_class(row,home,volumes[home],signatures);categories[category]+=1
        if category in ('unresolved','external'):exceptions.append(row)
        elif category=='ntfs_metadata':metadata.append(row)
        elif category=='application':
            name=native(row['path'],volumes[home]);prefix=native(str(home/'data'),volumes[home])+'\\'
            if name.startswith(prefix):
                rel=name[len(prefix):];top=rel.split('\\')[0];folders[top]+=1
                if row['id']==16 and '.write-' not in rel:
                    tag='execution_logs' if top=='history' and rel.endswith('.jsonl') else top
                    examples.setdefault(tag,row)
    pipe_control=processes['pipe_transfer']
    named_control=processes['named_pipe_transfer']
    passed=bool(not exceptions and not network and disk_control and net_control and inherited and outside and len(rejected)==len(outside)
                and all(c['reader_type']==c['writer_type']==3 and c['payload_verified'] for c in (pipe_control,named_control)) and categories['application'])
    with (output/'events.jsonl').open('rb') as f:fingerprint=hashlib.file_digest(f,'sha256').hexdigest()
    answer={'package':processes['zip'],'roots':{str(p):v for p,v in volumes.items()},'file_network_passed':passed,'pids':starts,
        'counts':{k:categories[k] for k in ('application','ntfs_metadata','ipc','unresolved','external')},'files':len(package_records),
        'network':network,'network_control':len(net_control),'write_control':len(disk_control),'write_control_events':disk_control,'disk_inherited':inherited,'outside_control':rejected,
        'pipe_transfer':pipe_control,'named_pipe_transfer':named_control,'pipe_disk_events':[r for label,r in controls if label in ('pipe','named_pipe')],
        'ntfs_control':[r for label,r in controls if label in ('orchestrator','outside') and write_class(r,app,default_vol,signatures)=='ntfs_metadata'],
        'ntfs_metadata':metadata,'unresolved_or_external':exceptions,'data_directories':dict(folders),'data_write_examples':examples,
        'lost':final['lost'],'rundown_lost':final['rundownLost'],'raw_sha256':fingerprint,
        'peak_rss':max((sum(p['working_set'] for p in sample['tree']) for sample in processes['samples']),default=0)}
    return answer


def review(output,app,processes,events):
    positive=classify(output,app,processes,events)
    # Repeat the complete decision on the same raw bytes, this time assigning
    # the intentional external writer to the application's physical root.
    writer={'label':'ordinary-external-write-control','pid':processes['external_control'],'home':str(app)}
    expanded={**processes,'scenes':[*processes.get('scenes',[]),writer]}
    negative=classify(output,app,expanded,events)
    rejected=[e for e in negative['unresolved_or_external'] if e['id']==16 and e['path'].lower().endswith('\\negative-control.bin')]
    negative_ok=bool(not negative['file_network_passed'] and rejected)
    positive['negative_replay']={'file_network_passed':negative['file_network_passed'],'rejected_write_events':rejected,
                                'external_events':negative['counts']['external'],'control_was_detected':negative_ok}
    positive['file_network_passed']=positive['file_network_passed'] and negative_ok
    return positive


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Replay a completed scoped ETW capture including the external-write negative control.')
    parser.add_argument('output',type=Path)
    folder=parser.parse_args().output.resolve()
    proc=json.loads((folder/'processes.json').read_bytes())
    rows=[json.loads(s) for s in (folder/'events.jsonl').read_text('utf8').splitlines()]
    checked=review(folder,Path(proc['root']),proc,rows)
    print(json.dumps({k:checked[k] for k in ('file_network_passed','counts','network_control','negative_replay')},indent=2))
    if not checked['file_network_passed']:raise SystemExit(1)
