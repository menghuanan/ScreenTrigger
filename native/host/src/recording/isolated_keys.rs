//! Read the launched GUI thread's queue on its private recording desktop.
//! The reader has no windows. The stop window keeps its own independent queue.
use super::{
    isolated::IsolatedDesktop,
    sequence::{Key, Sequence},
    windows::named,
};
use std::{
    sync::{mpsc, Arc},
    thread,
    time::Duration,
};

#[link(name = "user32")]
extern "system" {
    fn GetWindowThreadProcessId(hwnd: isize, pid: *mut u32) -> u32;
    fn AttachThreadInput(from: u32, to: u32, attach: i32) -> i32;
    fn GetKeyboardState(state: *mut u8) -> i32;
    fn GetFocus() -> isize;
}
#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentThreadId() -> u32;
}

struct AttachedQueue {
    reader: u32,
    target: u32,
}
impl Drop for AttachedQueue {
    fn drop(&mut self) {
        super::isolated::trace_lifecycle("reader.detach.begin");
        unsafe {
            AttachThreadInput(self.reader, self.target, 0);
        }
        super::isolated::trace_lifecycle("reader.detach.end");
    }
}

type Sample = Vec<(u16, &'static str)>;
type Request = (Option<isize>, mpsc::Sender<Option<Sample>>);
pub(super) struct ControlKeys {
    request: Option<mpsc::SyncSender<Request>>,
    join: Option<thread::JoinHandle<()>>,
    previous: Sample,
    control: Option<isize>,
    timestamp: u64,
    suppressed: bool,
}
impl ControlKeys {
    pub fn new(desktop: Arc<IsolatedDesktop>) -> Self {
        let (request, requests) = mpsc::sync_channel::<Request>(1);
        let join = thread::spawn(move || {
            super::isolated::prepare_non_text_thread();
            super::isolated::trace_lifecycle("reader.begin");
            if desktop.enter_dedicated_thread().is_err() {
                return;
            }
            // USER creates the reader's queue on first use.
            crate::winmessages::pop(0, 0);
            let reader = unsafe { GetCurrentThreadId() };
            let mut attached: Option<AttachedQueue> = None;
            let names: Vec<_> = (1..=0x7b)
                .filter_map(|vk| named(vk).map(|name| (vk, name)))
                .collect();
            for (control, response) in requests {
                let snapshot = control.and_then(|hwnd| {
                    if !desktop.is_input_desktop() || !desktop.owns_window(hwnd) {
                        return None;
                    }
                    let target = unsafe { GetWindowThreadProcessId(hwnd, std::ptr::null_mut()) };
                    if target == 0 {
                        return None;
                    }
                    if attached.as_ref().is_none_or(|q| q.target != target) {
                        drop(attached.take());
                        if unsafe { AttachThreadInput(reader, target, 1) } == 0 {
                            return None;
                        }
                        attached = Some(AttachedQueue { reader, target });
                    }
                    let mut keys = [0u8; 256];
                    if unsafe {
                        GetFocus() != hwnd
                            || GetKeyboardState(keys.as_mut_ptr()) == 0
                            || GetFocus() != hwnd
                    } || !desktop.is_input_desktop()
                    {
                        return None;
                    }
                    Some(
                        names
                            .iter()
                            .copied()
                            .filter(|(vk, _)| keys[*vk as usize] & 0x80 != 0)
                            .collect(),
                    )
                });
                if snapshot.is_none() {
                    drop(attached.take());
                }
                let _ = response.send(snapshot);
            }
            super::isolated::trace_lifecycle("reader.channel.closed");
        });
        Self {
            request: Some(request),
            join: Some(join),
            previous: vec![],
            control: None,
            timestamp: 0,
            suppressed: false,
        }
    }

    fn read(&self, control: Option<isize>) -> Option<Sample> {
        let (respond, result) = mpsc::channel();
        self.request.as_ref()?.try_send((control, respond)).ok()?;
        result.recv_timeout(Duration::from_millis(20)).ok()?
    }

    pub fn stop(&mut self) {
        drop(self.request.take());
    }

    pub fn reset(&mut self, sequence: &std::sync::Mutex<Sequence>) {
        if self.control.take().is_some() {
            self.read(None);
            if !self.previous.is_empty() && !self.suppressed {
                sequence.lock().unwrap().abort_chord("key_control_changed");
            }
        }
        self.previous.clear();
        self.suppressed = false;
    }

    pub fn sample(
        &mut self,
        sequence: &std::sync::Mutex<Sequence>,
        now: u64,
        top: isize,
        control: impl Fn() -> Option<isize>,
    ) -> bool {
        let Some(hwnd) = control() else {
            self.reset(sequence);
            return false;
        };
        let Some(keys) = self.read(Some(hwnd)) else {
            self.reset(sequence);
            return false;
        };
        if control() != Some(hwnd) {
            self.reset(sequence);
            return false;
        }
        let mut state = sequence.lock().unwrap();
        if !state.accepts_key(Some(top)) {
            drop(state);
            self.reset(sequence);
            return false;
        }
        if self.control != Some(hwnd) {
            if self.control.is_some() && !self.previous.is_empty() && !self.suppressed {
                state.abort_chord("key_control_changed");
            }
            self.control = Some(hwnd);
            self.suppressed = !keys.is_empty();
        } else if self.suppressed {
            self.suppressed = !keys.is_empty();
        } else if self.previous != keys {
            let mut edges = self
                .previous
                .iter()
                .filter(|key| !keys.contains(key))
                .map(|key| (key.1, false))
                .chain(
                    keys.iter()
                        .filter(|key| !self.previous.contains(key))
                        .map(|key| (key.1, true)),
                );
            if let Some((name, down)) = edges.next() {
                if edges.next().is_some() || now.saturating_sub(self.timestamp) > 40 {
                    state.abort_chord("ambiguous_control_edges");
                    self.suppressed = !keys.is_empty();
                } else {
                    state.message_key(now, top, hwnd, None, Key::Named { key: name }, down);
                }
            }
        }
        self.timestamp = now;
        self.previous = keys;
        true
    }
}
impl Drop for ControlKeys {
    fn drop(&mut self) {
        super::isolated::trace_lifecycle("worker.join-reader.begin");
        self.stop();
        if let Some(join) = self.join.take() {
            crate::winmessages::join_servicing_sent_messages(join);
        }
        super::isolated::trace_lifecycle("worker.join-reader.end");
    }
}
