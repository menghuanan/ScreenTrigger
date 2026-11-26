"""Use two owned Win32 windows to compare real foreground messages with session-scoped actions."""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import struct
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/acceptance'))
sys.path.insert(0,str(ROOT/'tools/probes/windows'))
import common
from verify_p1_runtime import Host, user, wait
from recording_layout import EditorLayout

class Packet(ct.Structure):
    _fields_=[('kind',ct.c_size_t),('count',wt.DWORD),('data',ct.c_void_p)]

class InputWindow(common.MiniWindow):
    def __init__(self,*args,participate=True):
        self.receiver,self.token=None,None
        self.keys=[]
        super().__init__(*args,popup=True)
        user.SetPropW.argtypes=[wt.HWND,wt.LPCWSTR,ct.c_void_p]
        if participate: assert user.SetPropW(self.hwnd,'ScreenTrigger.RecordingInput.v1',1)
        user.SendMessageW.argtypes=[wt.HWND,wt.UINT,ct.c_size_t,ct.c_ssize_t]
        self.edit=None if participate else common.create_edit_child(self.hwnd,'',20,20,300,125)
        self.truth=[]
    def pump(self,ms):
        deadline=time.monotonic()+ms/1000; m=common.MSG()
        while time.monotonic()<deadline:
            while user.PeekMessageW(ct.byref(m),None,0,0,1):
                if self.edit and m.hwnd==self.edit and m.message in (0x100,0x101,0x104,0x105) and m.wParam in (0x11,0x76,0x75,0x1c,0x41):
                    self.truth.append((m.wParam,m.message in (0x100,0x104)))
                user.TranslateMessage(ct.byref(m));user.DispatchMessageW(ct.byref(m))
            time.sleep(.001)
    def _wndproc(self,h,msg,w,l):
        if msg==0x4a:
            packet=Packet.from_address(l)
            if packet.kind==0x53545231 and packet.count==16:
                self.receiver,self.token=w,ct.string_at(packet.data,16)
                return 1
        if msg in (0x100,0x101,0x104,0x105):
            down=msg in (0x100,0x104)
            self.keys.append((w,down))
            if self.receiver and self.token:
                payload=self.token+struct.pack('<HHBBBB',w,(l>>16)&255,(l>>24)&1,int(down),0,1)
                blob=ct.create_string_buffer(payload)
                packet=Packet(0x53545232,len(payload),ct.addressof(blob))
                user.SendMessageW(self.receiver,0x4a,h,ct.addressof(packet))
        return super()._wndproc(h,msg,w,l)

def target(pipe,title,x):
    common.set_per_monitor_v2();win=InputWindow(title,x,250,460,260,participate=x==160)
    layout=EditorLayout(user,win.edit or win.hwnd)
    user.SetWindowPos(win.hwnd,-1,x,250,460,260,0x10)
    try:
        pipe.send(win.hwnd)
        while True:
            win.pump(4)
            if pipe.poll():
                op=pipe.recv()
                if op=='stop':break
                if op=='reset':win.truth.clear();win.keys.clear()
                if op=='password':user.SendMessageW(win.edit,0xcc,ord('*'),0)
                if op=='english':pipe.send(layout.select(9));continue
                user.GetFocus.restype=wt.HWND
                pipe.send({'offered':win.token.hex() if win.token else None,'keys':len(win.keys),'received':list(win.keys),'truth':win.truth,
                           'focus':user.GetFocus(),'edit':win.edit,'top':win.hwnd})
    finally:
        try:layout.restore()
        finally:win.destroy()

def run(dll,directory):
    directory.mkdir();common.set_per_monitor_v2();testid=str(uuid4())
    children=[];host=None;observed=[]
    def launch(name,x):
        pipe,childpipe=multiprocessing.Pipe();child=multiprocessing.Process(target=target,args=(childpipe,name,x));child.start()
        children.append((pipe,child));assert pipe.poll(6);return pipe,pipe.recv()
    def mouse(x,y,hwnd,event=2,child=None):
        target=child or hwnd
        if user.WindowFromPoint(wt.POINT(x,y))!=target:
            r=wt.RECT();assert user.GetWindowRect(target,ct.byref(r))
            pt=next((p for cy in range(r.top+20,r.bottom,18) for cx in range(r.left+20,r.right,18)
                     if user.WindowFromPoint(p:=wt.POINT(cx,cy))==target),None)
            assert pt is not None,'No visible pixel belongs to the expected fixture control'
            x,y=pt.x,pt.y
        assert user.WindowFromPoint(wt.POINT(x,y))==target
        user.SetCursorPos(x,y);time.sleep(.035);user.mouse_event(event,0,0,0,0)
        if event==2:time.sleep(.04);user.mouse_event(4,0,0,0,0)
        time.sleep(.045)
    def key(hwnd,vk,down):
        assert user.GetForegroundWindow()==hwnd,'An unexpected application has focus'
        user.keybd_event(vk,0x1e if vk==0x41 else 0,0 if down else 2,0);time.sleep(.045)
    def snap(pipe):pipe.send('state');assert pipe.poll(2);return pipe.recv()
    try:
        a,ha=launch('ScreenTrigger recording '+testid,160);b,hb=launch('ScreenTrigger observer '+testid,850)
        host=Host(dll,directory);host.session=host.call('start')['session_id'];host.request('hello')
        display=next(d['id'] for d in host.request('get_display_list')['displays'] if d['primary'])
        win=next(w['window_id'] for w in host.request('list_windows')['windows'] if w['title']=='ScreenTrigger recording '+testid)
        region=dict(space='screen_physical',display_id=display,x=170,y=260,width=350,height=150)
        for mode in ('region','display','window'):
            mouse(225,320,ha);wait(lambda:user.GetForegroundWindow()==ha,'focus own target')
            if mode=='window':
                binding=host.request('bind_window',window_id=win)['binding']['binding_id'];context={'binding_id':binding}
            else:context={'display_id':display,**({'region':region} if mode=='region' else {})}
            old=snap(a)['offered'];state=host.request('start_recording',context=context);rid=state['recording_id'];args={'recording_id':rid}
            if mode!='window':
                key(ha,0x75,True);key(ha,0x75,False);assert host.request('get_recording_state',**args)['action_count']==0
            mouse(225,320,ha);wait(lambda:snap(a)['offered'] not in (None,old),'explicit participant nonce')
            assert host.request('get_recording_state',**args)['keyboard_available']
            a.send('reset');a.recv()
            for vk,down in [(0x11,True),(0x76,True),(0x76,False),(0x11,False)]:key(ha,vk,down)
            assert snap(a)['received']==[(0x11,True),(0x76,True),(0x76,False),(0x11,False)]
            point=wt.POINT();assert user.GetCursorPos(ct.byref(point)) and user.WindowFromPoint(point)==ha
            user.mouse_event(0x0800,0,0,120,0);time.sleep(.1)
            if mode!='display':
                mouse(920,330,hb,child=snap(b)['edit']);wait(lambda:user.GetForegroundWindow()==hb,'focus own observer')
                before=host.request('get_recording_state',**args);key(hb,0x75,True);key(hb,0x75,False)
                assert host.request('get_recording_state',**args)['action_count']==before['action_count']
                if mode=='window':assert before['status']=='paused'
            end=host.request('finish_recording',**args);actions=end['actions'];kinds=[v['action']['kind'] for v in actions]
            assert kinds==['click','key_sequence','scroll'],kinds
            keys=next(v['action']['events'] for v in actions if v['action']['kind']=='key_sequence')
            assert [(k['key'],k['down']) for k in keys]==[('ctrl',True),('f7',True),('f7',False),('ctrl',False)]
            assert actions[-1]['action']['delta']==120 and all(v['seq']==i for i,v in enumerate(actions))
            mouse(225,320,ha);key(ha,0x75,True);key(ha,0x75,False)
            assert host.request('get_recording_state',**args)['action_count']==len(actions)
            observed.append(dict(mode=mode,complete=len(actions),counts={kind:kinds.count(kind) for kind in set(kinds)},
                                 pending=len(end['interrupted']),matched_order=True,after_stop_growth=0))
        # First activate the app. An activation click on a WS_POPUP can leave focus on
        # its frame; the recorder must never assume that the child under the pointer has focus.
        child_edit=snap(b)['edit']
        mouse(920,330,hb,child=child_edit);mouse(920,330,hb,child=child_edit)
        before=snap(b);assert before['focus']==before['edit'],before
        state=host.request('start_recording',context={'display_id':display});args={'recording_id':state['recording_id']}
        mouse(920,330,hb,child=child_edit)
        try:
            wait(lambda:host.request('get_recording_state',**args)['keyboard_available'],'ordinary focused edit supports command keys')
        except AssertionError:
            observed.append(dict(mode='ordinary_focus_failure', local_window=snap(b),foreground=user.GetForegroundWindow(),
                                 pointer_window=user.WindowFromPoint(wt.POINT(920,330)),state=host.request('get_recording_state',**args)))
            raise
        source=host.request('get_recording_state',**args)['keyboard_source']
        b.send('english');assert b.poll(2);layout=b.recv()
        assert layout['primary_language']==9 and not (layout['is_ime'] and layout['ime_open']),layout
        b.send('reset');b.recv()
        for vk,down in [(0x11,True),(0x76,True),(0x76,False),(0x11,False)]:key(hb,vk,down)
        captured=snap(b)['truth'];assert captured==[(0x11,True),(0x76,True),(0x76,False),(0x11,False)]
        assert host.request('get_recording_state',**args)['action_count']==2
        key(hb,0x41,True);key(hb,0x41,False)
        assert snap(b)['truth']==captured+[(0x41,True),(0x41,False)],'The edit did not receive the English A'
        assert host.request('get_recording_state',**args)['action_count']==2,'The original desktop captured a printable key'
        b.send('password');b.recv();wait(lambda:not host.request('get_recording_state',**args)['keyboard_available'],'password mask suppresses even function keys')
        key(hb,0x75,True);key(hb,0x75,False)
        end=host.request('finish_recording',**args);assert [v['action']['kind'] for v in end['actions']]==['click','key_sequence']
        edges=end['actions'][1]['action']['events'];assert [(e['key'],e['down']) for e in edges]==[('ctrl',True),('f7',True),('f7',False),('ctrl',False)]
        observed.append(dict(mode='original_desktop_native_edit',real_clicks=1,keyboard_source=source,native_messages=6,matched_command_edges=True,
            layout=layout,min_interval_ms=min(edges[i]['offset_ms']-edges[i-1]['offset_ms'] for i in range(1,4)),
            password_extra_actions=0,plain_printable_extra_actions=0))
    finally:
        (directory/'observations.json').write_text(json.dumps(observed,indent=2),encoding='utf-8')
        if host:host.close()
        for pipe,child in children:
            if child.is_alive():pipe.send('stop')
            child.join(2)
            if child.is_alive():child.terminate();child.join()

if __name__=='__main__':
    opts=argparse.ArgumentParser();opts.add_argument('dll',type=Path);opts.add_argument('directory',type=Path);args=opts.parse_args()
    run(args.dll.resolve(),args.directory.resolve())
