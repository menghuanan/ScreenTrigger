use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use crate::input::{
    dispatch::DispatchPipeline,
    model::{
        InputAction, InputContext, InputPort, Key, MouseButton, Point, PointerPort, PortError,
        VirtualScreen, WindowIdentity,
    },
    win32,
    wiring::{SystemClock, SystemRegionResolution},
};
use crate::{
    ipc::IpcError,
    window::{self, Entry, Identity},
};

#[repr(C)]
#[derive(Copy, Clone, Default)]
struct WinPoint {
    x: i32,
    y: i32,
}

#[link(name = "user32")]
extern "system" {
    fn RegisterWindowMessageW(name: *const u16) -> u32;
    fn SendMessageTimeoutW(
        hwnd: isize,
        msg: u32,
        w: usize,
        l: isize,
        flags: u32,
        millis: u32,
        result: *mut usize,
    ) -> isize;
    fn PostMessageW(hwnd: isize, msg: u32, w: usize, l: isize) -> i32;
    fn ScreenToClient(hwnd: isize, p: *mut WinPoint) -> i32;
    fn ChildWindowFromPointEx(hwnd: isize, p: WinPoint, flags: u32) -> isize;
    fn GetWindowThreadProcessId(hwnd: isize, pid: *mut u32) -> u32;
    fn IsChild(parent: isize, child: isize) -> i32;
    fn MapVirtualKeyW(code: u32, kind: u32) -> u32;
}

const CAP_SIGNATURE: usize = 0x5354_0000;
const BUDGET_MS: u32 = 100;
fn send(hwnd: isize, msg: u32, w: usize, l: isize) -> Result<usize, String> {
    let mut result = 0;
    // ABORTIFHUNG | BLOCK | ERRORONEXIT. Never ask Windows to retry a possible side effect.
    if unsafe { SendMessageTimeoutW(hwnd, msg, w, l, 0x23, BUDGET_MS, &mut result) } == 0 {
        return Err(format!(
            "目标没有在 {BUDGET_MS} 毫秒内确认消息，或系统拒绝访问（{}）",
            std::io::Error::last_os_error()
        ));
    }
    Ok(result)
}
pub(super) fn declared(entry: &Entry) -> Result<Option<u32>, String> {
    if !entry.live.visible || entry.live.minimized {
        return Err("窗口已隐藏或最小化".into());
    }
    static QUERY: OnceLock<u32> = OnceLock::new();
    let msg = *QUERY.get_or_init(|| {
        let wide: Vec<_> = "ScreenTrigger.BackgroundInput.Capabilities.v1"
            .encode_utf16()
            .chain(Some(0))
            .collect();
        unsafe { RegisterWindowMessageW(wide.as_ptr()) }
    });
    if msg == 0 {
        return Err("无法注册后台输入能力查询".into());
    }
    let result = send(entry.live.identity.hwnd, msg, 0, 0)?;
    Ok((result & !31 == CAP_SIGNATURE).then_some((result & 31) as u32))
}

#[derive(Copy, Clone, PartialEq)]
struct Receiver {
    hwnd: isize,
    thread: u32,
}

struct State {
    local: Point,
    buttons: Vec<(MouseButton, Receiver)>,
    keys: HashMap<u16, Receiver>,
}
#[derive(Clone)]
struct Target {
    binding: String,
    identity: Identity,
    state: Arc<Mutex<State>>,
}

pub(super) fn pipeline(ctx: &InputContext) -> Result<DispatchPipeline, IpcError> {
    let binding = ctx
        .binding_id
        .as_deref()
        .ok_or_else(|| IpcError::invalid_params("binding_id"))?;
    let entry = bound(binding)?;
    let size = &entry.live.rect;
    let local = ctx.region.as_ref().map_or(
        Point {
            x: size.width / 2,
            y: size.height / 2,
        },
        |roi| Point {
            x: roi.x + roi.width / 2,
            y: roi.y + roi.height / 2,
        },
    );
    let target = Target {
        binding: binding.into(),
        identity: entry.live.identity,
        state: Arc::new(Mutex::new(State {
            local,
            buttons: Vec::new(),
            keys: HashMap::new(),
        })),
    };
    let mut route = DispatchPipeline::new(
        Box::new(target.clone()),
        Box::new(target),
        Box::new(SystemRegionResolution::background()),
        Box::new(SystemClock::new()),
    );
    route.background = true;
    Ok(route)
}

fn bound(binding: &str) -> Result<Entry, IpcError> {
    let windows = window::shared().ok_or_else(|| IpcError::window_not_found("窗口服务不可用"))?;
    let entry = windows
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .bound(binding)?;
    Ok(entry)
}
fn packed(x: i64, y: i64) -> Result<isize, PortError> {
    let x = i16::try_from(x).map_err(|_| PortError::new("横坐标超出窗口消息的 16 位有符号范围"))?;
    let y = i16::try_from(y).map_err(|_| PortError::new("纵坐标超出窗口消息的 16 位有符号范围"))?;
    Ok((u32::from(x as u16) | (u32::from(y as u16) << 16)) as isize)
}
fn client(hwnd: isize, p: Point) -> Result<WinPoint, PortError> {
    let mut local = WinPoint {
        x: i32::try_from(p.x).map_err(|_| PortError::new("坐标溢出"))?,
        y: i32::try_from(p.y).map_err(|_| PortError::new("坐标溢出"))?,
    };
    if unsafe { ScreenToClient(hwnd, &mut local) } == 0 {
        return Err(PortError::new("客户区已失效"));
    }
    Ok(local)
}
fn mouse_mask(state: &State) -> usize {
    let mut mask = 0;
    for (b, _) in &state.buttons {
        mask |= match b {
            MouseButton::Left => 1,
            MouseButton::Right => 2,
            MouseButton::Middle => 16,
        };
    }
    if state.keys.keys().any(|vk| matches!(vk, 0x10 | 0xa0 | 0xa1)) {
        mask |= 4;
    }
    if state.keys.keys().any(|vk| matches!(vk, 0x11 | 0xa2 | 0xa3)) {
        mask |= 8;
    }
    mask
}

impl Target {
    fn entry(&self) -> Result<Entry, IpcError> {
        let entry = bound(&self.binding)?;
        if entry.live.identity != self.identity {
            return Err(IpcError::window_not_found("绑定目标身份变化"));
        }
        Ok(entry)
    }
    fn usable(&self, cleanup: bool) -> Result<Entry, PortError> {
        let entry = self.entry().map_err(|e| PortError::new(e.message))?;
        if !cleanup && (!entry.live.visible || entry.live.minimized) {
            return Err(PortError::new("目标已经隐藏或最小化"));
        }
        Ok(entry)
    }
    fn receiver(&self, hwnd: isize) -> Option<Receiver> {
        let mut pid = 0;
        let thread = unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
        let inside =
            hwnd == self.identity.hwnd || unsafe { IsChild(self.identity.hwnd, hwnd) } != 0;
        (thread != 0 && pid == self.identity.pid && inside).then_some(Receiver { hwnd, thread })
    }
    fn screen(&self, st: &State, e: &Entry) -> Point {
        Point {
            x: e.live.rect.x + st.local.x,
            y: e.live.rect.y + st.local.y,
        }
    }
    fn hit(&self, p: Point) -> Result<Receiver, PortError> {
        let mut hwnd = self.identity.hwnd;
        for _ in 0..64 {
            let local = client(hwnd, p)?;
            let next = unsafe { ChildWindowFromPointEx(hwnd, local, 7) };
            if next == 0 || next == hwnd {
                return self
                    .receiver(hwnd)
                    .ok_or_else(|| PortError::new("目标控件身份失效"));
            }
            if self.receiver(next).is_none() {
                return Err(PortError::new("落点属于不同进程的控件"));
            }
            hwnd = next;
        }
        Err(PortError::new("控件层级过深"))
    }
    fn keyboard(&self, p: Point) -> Result<Receiver, PortError> {
        let top = self
            .receiver(self.identity.hwnd)
            .ok_or_else(|| PortError::new("窗口身份已失效"))?;
        if let Some(focus) = window::system::thread_focus(top.thread).filter(|h| *h != top.hwnd) {
            if let Some(child) = self.receiver(focus) {
                return Ok(child);
            }
        }
        self.hit(p)
    }
    fn message(&self, recv: Receiver, msg: u32, w: usize, l: isize) -> Result<(), PortError> {
        if self.receiver(recv.hwnd) != Some(recv) {
            return Err(PortError::new("原接收控件已经关闭或替换"));
        }
        send(recv.hwnd, msg, w, l)
            .map(|_| ())
            .map_err(PortError::new)
    }
    fn button(&self, btn: MouseButton, down: bool, cleanup: bool) -> Result<(), PortError> {
        let entry = self.usable(cleanup)?;
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let p = self.screen(&st, &entry);
        let recv = if down {
            self.hit(p)?
        } else {
            st.buttons
                .iter()
                .find(|(b, _)| *b == btn)
                .map(|(_, r)| *r)
                .ok_or_else(|| PortError::new("按钮没有绑定接收控件"))?
        };
        let local = client(recv.hwnd, p)?;
        let l = packed(local.x.into(), local.y.into())?;
        let (msg, bit) = match btn {
            MouseButton::Left => (0x201, 1),
            MouseButton::Right => (0x204, 2),
            MouseButton::Middle => (0x207, 16),
        };
        let mask = mouse_mask(&st);
        if down {
            st.buttons.push((btn, recv));
        }
        let result = self.message(
            recv,
            if down { msg } else { msg + 1 },
            if down { mask | bit } else { mask & !bit },
            l,
        );
        if down && result.is_err() && self.receiver(recv.hwnd) == Some(recv) {
            unsafe {
                PostMessageW(recv.hwnd, msg + 1, mask & !bit, l);
            }
        }
        if !down || result.is_err() {
            st.buttons.retain(|(b, _)| *b != btn);
        }
        result
    }
    fn key(&self, key: &Key, down: bool, cleanup: bool) -> Result<(), PortError> {
        let entry = self.usable(cleanup)?;
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let recv = if down {
            self.keyboard(self.screen(&st, &entry))?
        } else {
            *st.keys
                .get(&key.vk)
                .ok_or_else(|| PortError::new("按键没有绑定接收控件"))?
        };
        let scan = key.scan_code.map(u32::from).unwrap_or_else(|| unsafe { MapVirtualKeyW(u32::from(key.vk), 0) } & 255);
        let mut flags = 1 | (scan << 16) | (u32::from(key.extended) << 24);
        if !down {
            flags |= 0xc000_0000;
        }
        if down {
            st.keys.insert(key.vk, recv);
        }
        let result = self.message(
            recv,
            if down { 0x100 } else { 0x101 },
            key.vk.into(),
            flags as isize,
        );
        if down && result.is_err() && self.receiver(recv.hwnd) == Some(recv) {
            unsafe {
                PostMessageW(
                    recv.hwnd,
                    0x101,
                    key.vk.into(),
                    (flags | 0xc000_0000) as isize,
                );
            }
        }
        if !down || result.is_err() {
            st.keys.remove(&key.vk);
        }
        result
    }
}

impl InputPort for Target {
    fn authorize(&self, action: &InputAction) -> Result<(), IpcError> {
        let entry = self.entry()?;
        if entry.live.minimized || !entry.live.visible {
            return Err(IpcError::window_unavailable("请还原绑定窗口后手动继续"));
        }
        let mask = declared(&entry)
            .map_err(|e| IpcError::capability_denied(&e))?
            .unwrap_or(0);
        let allowed = match action {
            InputAction::Move { .. } => mask & 7 != 0,
            InputAction::Click { .. } | InputAction::ClickAnchor { .. } => mask & 1 != 0,
            InputAction::Drag { .. } | InputAction::DragAnchor { .. } => mask & 2 != 0,
            InputAction::Scroll { .. } | InputAction::ScrollAnchor { .. } => mask & 4 != 0,
            InputAction::RecordedKeys { events, .. } => {
                let chord = events.iter().filter(|e| e.down).any(|e| e.key != events[0].key);
                mask & 8 != 0 && (!chord || mask & 16 != 0)
            }
            InputAction::Key { .. } => mask & 8 != 0,
            InputAction::Hotkey { .. } => mask & 16 != 0,
            _ => false,
        };
        if allowed {
            Ok(())
        } else {
            Err(IpcError::capability_denied("目标没有声明所选后台动作能力"))
        }
    }
    fn move_to(&self, x: i64, y: i64) -> Result<(), PortError> {
        let entry = self.usable(false)?;
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let p = Point { x, y };
        let recv = if let Some((_, r)) = state.buttons.first() {
            *r
        } else {
            self.hit(p)?
        };
        let local = client(recv.hwnd, p)?;
        self.message(
            recv,
            0x200,
            mouse_mask(&state),
            packed(local.x.into(), local.y.into())?,
        )?;
        state.local = Point {
            x: x - entry.live.rect.x,
            y: y - entry.live.rect.y,
        };
        Ok(())
    }
    fn wheel(&self, delta: i32) -> Result<(), PortError> {
        let amount = i16::try_from(i64::from(delta) * 120)
            .map_err(|_| PortError::new("滚轮增量超出窗口消息范围"))?;
        self.raw_wheel(amount)
    }
    fn key_target(&self) -> Result<(isize, isize), PortError> {
        let entry = self.usable(false)?;
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        Ok((self.identity.hwnd, self.keyboard(self.screen(&state, &entry))?.hwnd))
    }
    fn raw_wheel(&self, amount: i16) -> Result<(), PortError> {
        let entry = self.usable(false)?;
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let screen = self.screen(&state, &entry);
        let recv = self.hit(screen)?;
        self.message(
            recv,
            0x20a,
            mouse_mask(&state) | (usize::from(amount as u16) << 16),
            packed(screen.x, screen.y)?,
        )
    }
    fn set_button(&self, b: MouseButton, down: bool) -> Result<(), PortError> {
        self.button(b, down, false)
    }
    fn key_event(&self, key: &Key, down: bool) -> Result<(), PortError> {
        self.key(key, down, false)
    }
    fn unicode_text(&self, _: &str) -> Result<(), PortError> {
        Err(PortError::new("目标未声明后台文本输入能力"))
    }
    fn release_keys(&self, _: &str, keys: &[Key], buttons: &[MouseButton]) {
        for b in buttons.iter().rev() {
            let _ = self.button(*b, false, true);
        }
        for key in keys.iter().rev() {
            let _ = self.key(key, false, true);
        }
    }
}

impl PointerPort for Target {
    fn virtual_screen(&self) -> VirtualScreen {
        win32::virtual_screen()
    }
    fn foreground_window(&self) -> WindowIdentity {
        WindowIdentity {
            handle: 0,
            pid: 0,
            process_started_at: String::new(),
        }
    }
    fn cursor_pos(&self) -> Result<Point, IpcError> {
        let entry = self.entry()?;
        Ok(self.screen(
            &self.state.lock().unwrap_or_else(|e| e.into_inner()),
            &entry,
        ))
    }
}
