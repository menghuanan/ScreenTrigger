//! Foreground command-key sampling for standard controls. Printable typing is never sampled.
use super::sequence::{Key, Sequence};
use crate::input::win32::key_is_down;

const KEYS: &[(u16, &str)] = &[
    (0x11, "ctrl"),
    (0x10, "shift"),
    (0x12, "alt"),
    (0x1b, "esc"),
    (0x09, "tab"),
    (0x0d, "enter"),
    (0x21, "pageup"),
    (0x22, "pagedown"),
    (0x23, "end"),
    (0x24, "home"),
    (0x25, "left"),
    (0x26, "up"),
    (0x27, "right"),
    (0x28, "down"),
    (0x2d, "insert"),
    (0x2e, "delete"),
    (0x08, "backspace"),
    (0x70, "f1"),
    (0x71, "f2"),
    (0x72, "f3"),
    (0x73, "f4"),
    (0x74, "f5"),
    (0x75, "f6"),
    (0x76, "f7"),
    (0x77, "f8"),
    (0x78, "f9"),
    (0x79, "f10"),
    (0x7a, "f11"),
    (0x7b, "f12"),
    (0x15, "kana"),
    (0x1c, "convert"),
    (0x1d, "nonconvert"),
];

#[derive(Default)]
pub struct CommandKeys {
    focus: Option<isize>,
    down: u32,
    previous: u64,
    suppressed: bool,
}

impl CommandKeys {
    pub fn reset(&mut self, state: &mut Sequence) {
        if self.focus.is_some() && self.down != 0 {
            state.abort_chord("command_focus_changed");
        }
        *self = Self::default();
    }

    /// `control` verifies the actual foreground, focused control and password state twice.
    pub fn sample(
        &mut self,
        state: &mut Sequence,
        now: u64,
        top: isize,
        control: impl Fn() -> Option<isize>,
    ) {
        if !state.accepts_key(Some(top)) {
            self.reset(state);
            return;
        }
        let Some(hwnd) = control() else {
            self.reset(state);
            return;
        };
        let mut mask = 0u32;
        for (index, (vk, _)) in KEYS.iter().enumerate() {
            if key_is_down(*vk) {
                mask |= 1u32 << index;
            }
        }
        if control() != Some(hwnd) {
            self.reset(state);
            return;
        }
        if self.focus != Some(hwnd) {
            self.reset(state);
            self.focus = Some(hwnd);
            self.down = mask;
            self.previous = now;
            self.suppressed = mask != 0;
            return;
        }
        let delta = mask ^ self.down;
        let gap = now.saturating_sub(self.previous);
        self.previous = now;
        self.down = mask;
        if self.suppressed {
            self.suppressed = mask != 0;
            return;
        }
        if delta == 0 {
            return;
        }
        if delta.count_ones() != 1 || gap > 40 {
            state.abort_chord("ambiguous_command_edges");
            self.suppressed = mask != 0;
            return;
        }
        let index = delta.trailing_zeros() as usize;
        state.key(
            now,
            Some(top),
            Key::Named { key: KEYS[index].1 },
            mask & delta != 0,
        );
    }
}
