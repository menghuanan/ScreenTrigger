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
