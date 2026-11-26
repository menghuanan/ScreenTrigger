"""Compare a consenting stock Win32 edit control's own message queue with recorder actions."""
from ctypes import wintypes as wt
import ctypes as ct
import json
import multiprocessing as mp
import os
from pathlib import Path
import struct
import sys
import time
from uuid import UUID, uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/acceptance'))
from recording import Packet, common, user, Host, wait

class ParticipatingEditor(common.MiniWindow):
    def __init__(self, title, trusted_pid):
        self.trusted_pid = trusted_pid
        self.receiver = self.token = None
        super().__init__(title,180,250,445,250,popup=True)
        user.SetPropW.argtypes=[wt.HWND,wt.LPCWSTR,ct.c_void_p]
        assert user.SetPropW(self.hwnd,'ScreenTrigger.RecordingInput.v1',1)

    def trusted(self, hwnd, token):
        if not hwnd or not token or not user.IsWindowVisible(hwnd):
            return False
        pid=wt.DWORD()
        if not user.GetWindowThreadProcessId(hwnd,ct.byref(pid)) or pid.value != self.trusted_pid:
            return False
        cls=ct.create_unicode_buffer(100)
        return bool(user.GetClassNameW(hwnd,cls,len(cls)) and cls.value=='ScreenTrigger.Recording.'+str(UUID(bytes=token)))

    def _wndproc(self, hwnd, msg, w, l):
        if msg==0x4a and l:
            offer=Packet.from_address(l)
            if offer.kind==0x53545231 and offer.count==16 and offer.data:
                token=ct.string_at(offer.data,16)
                if self.trusted(w,token):
                    self.receiver,self.token=w,token
                    return 1
            return 0
        return super()._wndproc(hwnd,msg,w,l)

def editor(channel,title,trusted_pid):
    common.set_per_monitor_v2()
    user.GetWindowThreadProcessId.argtypes=[wt.HWND,ct.POINTER(wt.DWORD)]
    user.GetClassNameW.argtypes=[wt.HWND,wt.LPWSTR,ct.c_int]
    user.IsWindowVisible.argtypes=[wt.HWND]
    user.SendMessageW.argtypes=[wt.HWND,wt.UINT,ct.c_size_t,ct.c_ssize_t]
    user.SendMessageTimeoutW.argtypes=[wt.HWND,wt.UINT,ct.c_size_t,ct.c_ssize_t,wt.UINT,wt.UINT,ct.POINTER(ct.c_size_t)]
    w=ParticipatingEditor(title,trusted_pid)
    edit=common.create_edit_child(w.hwnd,'',20,20,360,150)
    user.SetWindowPos(w.hwnd,-1,180,250,445,250,0x10)
    user.GetFocus.restype=wt.HWND
    imm=ct.WinDLL('imm32')
    imm.ImmGetContext.argtypes=[wt.HWND];imm.ImmGetContext.restype=ct.c_void_p
    imm.ImmSetOpenStatus.argtypes=[ct.c_void_p,wt.BOOL];imm.ImmGetOpenStatus.argtypes=[ct.c_void_p]
    imm.ImmReleaseContext.argtypes=[wt.HWND,ct.c_void_p]
    imm.ImmGetConversionStatus.argtypes=[ct.c_void_p,ct.POINTER(wt.DWORD),ct.POINTER(wt.DWORD)]
    imm.ImmSetConversionStatus.argtypes=[ct.c_void_p,wt.DWORD,wt.DWORD]
    user.GetKeyboardLayout.argtypes=[wt.DWORD];user.GetKeyboardLayout.restype=ct.c_void_p
    original=None
    truth=[]
    channel.send((w.hwnd,edit))
    try:
        msg=common.MSG()
        while True:
            while user.PeekMessageW(ct.byref(msg),None,0,0,1):
                if msg.hwnd==edit and msg.message in (0x100,0x101,0x104,0x105):
                    down=msg.message in (0x100,0x104);vk=msg.wParam; scan=(msg.lParam>>16)&255
                    if user.GetFocus()==edit and user.GetForegroundWindow()==w.hwnd and w.trusted(w.receiver,w.token):
                        if user.SendMessageW(edit,0xd2,0,0)==0:
                            truth.append((vk,scan,down))
                            buf=ct.create_string_buffer(w.token+struct.pack('<HHBBBB',vk,scan,(msg.lParam>>24)&1,int(down),0,1))
                            packet=Packet(0x53545232,24,ct.addressof(buf))
                            reply=ct.c_size_t()
                            user.SendMessageTimeoutW(w.receiver,0x4a,edit,ct.addressof(packet),2,50,ct.byref(reply))
                user.TranslateMessage(ct.byref(msg));user.DispatchMessageW(ct.byref(msg))
            if channel.poll():
                cmd=channel.recv()
                if cmd=='quit':break
                if cmd=='clear':truth.clear()
                if cmd=='password':user.SendMessageW(edit,0xcc,ord('*'),0)
                if cmd=='remove-mask':user.SendMessageW(edit,0xcc,0,0)
                if cmd in ('ime','direct'):
                    ctx=imm.ImmGetContext(edit)
                    native,sentence=wt.DWORD(),wt.DWORD()
                    readable=bool(ctx and imm.ImmGetConversionStatus(ctx,ct.byref(native),ct.byref(sentence)))
                    if ctx:
                        if original is None:original=(bool(imm.ImmGetOpenStatus(ctx)),native.value,sentence.value,readable)
                        imm.ImmSetOpenStatus(ctx,cmd=='ime')
                        if cmd=='ime' and readable and not native.value&1:
                            imm.ImmSetConversionStatus(ctx,native.value|1,sentence.value)
                    opened=bool(ctx and imm.ImmGetOpenStatus(ctx))
                    if ctx:imm.ImmGetConversionStatus(ctx,ct.byref(native),ct.byref(sentence))
                    if ctx:imm.ImmReleaseContext(edit,ctx)
                    channel.send(dict(opened=opened,conversion=native.value,sentence=sentence.value,layout=hex(user.GetKeyboardLayout(0))));continue
                channel.send(dict(focus=user.GetFocus(),offered=w.token.hex() if w.token else None,truth=truth))
            time.sleep(.001)
    finally:
        if original:
            ctx=imm.ImmGetContext(edit)
            if ctx:
                if original[3]:imm.ImmSetConversionStatus(ctx,original[1],original[2])
                imm.ImmSetOpenStatus(ctx,original[0]);imm.ImmReleaseContext(edit,ctx)
        w.destroy()

def run(dll,folder,scope='window'):
    folder.mkdir();common.set_per_monitor_v2();host=None
    title='ScreenTrigger participating editor '+str(uuid4())
    parent,child=mp.Pipe();proc=mp.Process(target=editor,args=(child,title,os.getpid()));proc.start()
    out={}
    def ask(cmd):
        parent.send(cmd);assert parent.poll(4),cmd;return parent.recv()
    try:
        assert parent.poll(8);top,edit=parent.recv()
        def click():
            # A system IME candidate or the visible stop indicator can cover part of
            # the child. Choose only a point the real Windows hit test assigns to the edit.
            pt=next((p for y in (395,365,330,305,285) for x in (280,235,330,380,445,495)
                     if user.WindowFromPoint(p:=wt.POINT(x,y))==edit),None)
            assert pt is not None,'No visible verified pixel belongs to the stock edit'
            user.SetCursorPos(pt.x,pt.y);user.mouse_event(2,0,0,0,0);time.sleep(.035);user.mouse_event(4,0,0,0,0);time.sleep(.055)
        def key(vk,scan,down):
            assert user.GetForegroundWindow()==top and ask('state')['focus']==edit
            user.keybd_event(vk,scan,0 if down else 2,0);time.sleep(.045)
        host=Host(dll,folder);host.session=host.call('start')['session_id'];host.request('hello')
        dsp=next(d['id'] for d in host.request('get_display_list')['displays'] if d['primary'])
        win=next(w['window_id'] for w in host.request('list_windows')['windows'] if w['title']==title)
        binding=host.request('bind_window',window_id=win)['binding']['binding_id']
        context={'window':{'binding_id':binding},'display':{'display_id':dsp},'region':{'display_id':dsp,
                 'region':dict(space='screen_physical',display_id=dsp,x=185,y=255,width=415,height=205)}}[scope]
        mouse_actions=int(scope!='window')
        def start():
            click();click();assert ask('state')['focus']==edit
            old=ask('state')['offered']
            rid=host.request('start_recording',context=context)['recording_id'];args={'recording_id':rid}
            if mouse_actions:
                key(0x75,0x40,True);key(0x75,0x40,False)
                assert host.request('get_recording_state',**args)['action_count']==0
                click()
            wait(lambda:ask('state')['offered'] not in (None,old),'app consents to this bounded session');ask('clear')
            return args
        args=start()
        fixed=[(0x41,0x1e,'a'),(0x32,0x03,'2'),(0xba,0x27,0x27)]
        expected_actions=[];truth=[]
        for mode in ('ime','direct'):
            control=ask(mode);opened=control['opened'];assert opened==(mode=='ime')
            time.sleep(.18)
            ask('clear')
            for vk,scan,_ in fixed:
                for down in (True,False):key(vk,scan,down)
            local=ask('state')['truth']
            assert len(local)==6,local
            expected_scans=[(scan,down) for _,scan,_ in fixed for down in (True,False)]
            assert [(s,d) for _,s,d in local]==expected_scans,local
            for i,(vk,scan,name) in enumerate(fixed):
                first=local[i*2][0]
                assert first in ((vk,0xe5) if opened else (vk,)),local
                expected_actions.append(scan if first==0xe5 else name)
            truth.extend(local)
            out[mode]={**control,'physical_edges':len(local),'process_key_edges':sum(vk==0xe5 for vk,_,_ in local)}
        assert out['ime']['process_key_edges']>0,out
        n=len(expected_actions)+mouse_actions
        args_state=host.request('get_recording_state',**args)
        out['before_password']=args_state['action_count']
        assert args_state['action_count']==n,args_state
        ask('password');time.sleep(.05);key(0x46,0x21,True);key(0x46,0x21,False)
        assert host.request('get_recording_state',**args)['action_count']==n
        ask('remove-mask');time.sleep(.05);key(0x47,0x22,True)
        try:
            end=host.request('finish_recording',**args)
        finally:key(0x47,0x22,False)
        assert len(end['actions'])==n and end['interrupted'],end
        for i,row in enumerate(end['actions'][mouse_actions:]):
            events=row['action']['events'];assert [e['down'] for e in events]==[True,False]
            assert [e.get('key',e.get('scan_code')) for e in events]==[expected_actions[i]]*2
        key(0x48,0x23,True);key(0x48,0x23,False)
        assert host.request('get_recording_state',**args)['action_count']==n
        out.update(scope=scope,control='unmodified Win32 EDIT',source='application own foreground queue with per-session consent',
                   exact_letter_digit_scan_pairs=6,matching_native_edges=len(truth),password_new_actions=0,
                   interruption=end['interrupted'],post_stop_new_actions=0)
        args=start();ask('direct');key(0x41,0x1e,True);key(0x41,0x1e,False)
        assert host.request('get_recording_state',**args)['action_count']==1+mouse_actions
        cancelled=host.request('cancel_recording',**args)
        assert cancelled['status']=='cancelled' and cancelled['action_count']==0
        key(0x42,0x30,True);key(0x42,0x30,False)
        assert host.request('get_recording_state',**args)['action_count']==0
        out.update(cancelled_draft_actions=0,post_cancel_new_actions=0)
    finally:
        (folder/'observed.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
        if host:host.close()
        if proc.is_alive():parent.send('quit')
        proc.join(3)
        if proc.is_alive():proc.terminate();proc.join()
if __name__=='__main__':run(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve(),sys.argv[3])
