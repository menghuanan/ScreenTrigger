//! Thread-local Win32 message queue shared by registered hotkeys and the stop window.
#[repr(C)]
#[derive(Default)]
pub struct Message {
    window: isize,
    id: u32,
    pub wparam: usize,
    lparam: isize,
    time: u32,
    x: i32,
    y: i32,
    private: u32,
}
#[link(name = "user32")]
extern "system" {
    fn PeekMessageW(message: *mut Message, hwnd: isize, min: u32, max: u32, remove: u32) -> i32;
    fn DispatchMessageW(message: *const Message) -> isize;
    fn MsgWaitForMultipleObjectsEx(count: u32, handles: *const isize, timeout: u32, mask: u32, flags: u32) -> u32;
}

/// On a dedicated GUI thread, let USER finish synchronous notifications during teardown.
/// Do not dispatch posted keyboard, mouse, command or timer messages after capture ends.
pub fn join_servicing_sent_messages(join: std::thread::JoinHandle<()>) {
    use std::os::windows::io::AsRawHandle;
    const QS_SENDMESSAGE: u32 = 0x40;
    const MWMO_INPUTAVAILABLE: u32 = 4;
    let handle = join.as_raw_handle() as isize;
    while unsafe { MsgWaitForMultipleObjectsEx(1, &handle, u32::MAX, QS_SENDMESSAGE, MWMO_INPUTAVAILABLE) } == 1 {
        let mut message = Message::default();
        unsafe { PeekMessageW(&mut message, 0, 0, 0, QS_SENDMESSAGE << 16); }
    }
    let _ = join.join();
}
pub fn pop(min: u32, max: u32) -> Option<Message> {
    let mut message = Message::default();
    (unsafe { PeekMessageW(&mut message, 0, min, max, 1) } != 0).then_some(message)
}
impl Message {
    pub fn dispatch(&self) {
        unsafe {
            DispatchMessageW(self);
        }
    }
}
