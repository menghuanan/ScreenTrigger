//! In-memory semantic events for a single explicitly started recording.
use crate::input::model::MonitorRect;
use serde::Serialize;

const MAX_EVENTS: usize = 8192;
const MAX_ACTIVE_MS: u64 = 30 * 60 * 1000;

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ScopeKind {
    Window,
    Region,
    Display,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct Anchor {
    version: u8,
    scope: ScopeKind,
    strategy: &'static str,
    reference_width: i64,
    reference_height: i64,
    x: i64,
    y: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Action {
    Click {
        button: &'static str,
        point: Anchor,
    },
    Drag {
        from: Anchor,
        path: Vec<Anchor>,
        to: Anchor,
    },
    Scroll {
        point: Anchor,
        delta: i32,
    },
    KeySequence {
        events: Vec<KeyEdge>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Key {
    Named { key: &'static str },
    Physical { scan_code: u16, extended: bool },
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct KeyEdge {
    offset_ms: u64,
    down: bool,
    #[serde(flatten)]
    key: Key,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Recorded {
    pub seq: usize,
    pub start_ms: u64,
    pub end_ms: u64,
    pub action: Action,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Recording,
    Paused,
    Finished,
    Cancelled,
}

#[derive(Serialize)]
pub struct Interruption {
    pub after: usize,
    pub reason: &'static str,
}

struct Button {
    start_ms: u64,
    name: &'static str,
    from: Anchor,
    path: Vec<Anchor>,
}
struct Chord {
    start_ms: u64,
    events: Vec<KeyEdge>,
    down: Vec<Key>,
    message_control: Option<isize>,
    physical: Vec<((u16, bool), Key)>,
}

pub struct Sequence {
    pub(super) desktop: Option<super::desktop::Claim>,
    pub status: Status,
    pub completed: Vec<Recorded>,
    pub interrupted: Vec<Interruption>,
    mode: ScopeKind,
    rect: MonitorRect,
    bound: Option<isize>,
    owner: Option<isize>,
    awaiting_bound: bool,
    pending_owner: Option<(isize, u64)>,
    button: Option<Button>,
    chord: Option<Chord>,
    points: usize,
    paused: u64,
    since: u64,
}

impl Sequence {
    pub fn new(mode: ScopeKind, rect: MonitorRect, bound: Option<isize>, now: u64) -> Self {
        Self {
            desktop: None,
            status: Status::Recording,
            completed: Vec::new(),
            interrupted: Vec::new(),
            mode,
            rect,
            bound,
            owner: bound,
            awaiting_bound: false,
            pending_owner: None,
            button: None,
            chord: None,
            points: 0,
            paused: 0,
            since: now,
        }
    }
    fn clock(&self, now: u64) -> u64 {
        self.paused + now.saturating_sub(self.since)
    }
    fn anchor(&self, x: i64, y: i64) -> Option<Anchor> {
        let (dx, dy) = (x.checked_sub(self.rect.x)?, y.checked_sub(self.rect.y)?);
        if dx < 0 || dy < 0 || dx >= self.rect.width || dy >= self.rect.height {
            return None;
        }
        Some(Anchor {
            version: 1,
            scope: self.mode,
            strategy: "relative",
            reference_width: self.rect.width,
            reference_height: self.rect.height,
            x: dx,
            y: dy,
        })
    }
    fn report(&mut self, reason: &'static str) {
        if self.interrupted.len() < MAX_EVENTS {
            self.interrupted.push(Interruption {
                after: self.completed.len(),
                reason,
            });
        }
    }
    pub fn unknown_key(&mut self) {
        self.chord = None;
        self.report("unknown_physical_key");
    }
    pub fn abort_chord(&mut self, reason: &'static str) {
        self.chord = None;
        self.report(reason);
    }
    pub fn source_failed(&mut self, reason: &'static str) {
        self.interrupt_pending(reason);
        self.report(reason);
        self.status = Status::Finished;
        self.desktop = None;
    }
    fn interrupt_pending(&mut self, reason: &'static str) {
        if self.button.take().is_some() {
            self.report(reason);
        }
        if self.chord.take().is_some() {
            self.report(reason);
        }
    }
    fn room(&mut self, now: u64) -> bool {
        if self.status != Status::Recording {
            return false;
        }
        if self.points >= MAX_EVENTS
            || self.clock(now) >= MAX_ACTIVE_MS
            || self.interrupted.len() >= MAX_EVENTS
        {
            self.interrupt_pending("recording_limit");
            self.report("recording_limit");
            self.status = Status::Finished;
            self.desktop = None;
            return false;
        }
        self.points += 1;
        true
    }
    pub fn pause(&mut self, now: u64, reason: &'static str) {
        if self.status == Status::Recording {
            self.interrupt_pending(reason);
            self.owner = None;
            self.pending_owner = None;
            self.paused = self.clock(now);
            self.status = Status::Paused;
        }
    }
    pub fn resume(&mut self, now: u64, focus: Option<isize>) -> bool {
        if self.status != Status::Paused || (self.bound.is_some() && self.bound != focus) {
            return false;
        }
        self.owner = self.bound;
        self.since = now;
        self.status = Status::Recording;
        true
    }
    pub fn await_bound(&mut self) {
        if self.bound.is_some() {
            self.awaiting_bound = true;
            self.owner = None;
        }
    }
    pub fn resume_with_scope(&mut self, now: u64, rect: MonitorRect, bound: Option<isize>) -> bool {
        if rect != self.rect || bound != self.bound || !self.resume(now, bound) {
            return false;
        }
        self.await_bound();
        true
    }
    pub fn finish(&mut self) -> bool {
        if !matches!(self.status, Status::Recording | Status::Paused) {
            return false;
        }
        self.interrupt_pending("unpaired_on_finish");
        self.owner = None;
        self.status = Status::Finished;
        self.desktop = None;
        true
    }
    pub fn cancel(&mut self) -> bool {
        if !matches!(self.status, Status::Recording | Status::Paused) {
            return false;
        }
        self.button = None;
        self.chord = None;
        self.owner = None;
        self.completed.clear();
        self.interrupted.clear();
        self.status = Status::Cancelled;
        self.desktop = None;
        true
    }
    pub fn focus(&mut self, now: u64, foreground: Option<isize>, point_in_scope: bool) {
        if self.status != Status::Recording {
            return;
        }
        if self.awaiting_bound {
            if self.bound == foreground {
                self.awaiting_bound = false;
                self.owner = self.bound;
            } else {
                return;
            }
        }
        if self.bound.is_some() && self.bound != foreground {
            self.pause(now, "bound_focus_lost");
            return;
        }
        if let Some((candidate, expires)) = self.pending_owner.take() {
            if point_in_scope && now <= expires {
                if foreground == Some(candidate) {
                    self.owner = foreground;
                } else {
                    self.pending_owner = Some((candidate, expires));
                }
            }
        }
        if self.owner != foreground || (self.bound.is_none() && !point_in_scope) {
            self.owner = None;
            if self.chord.take().is_some() {
                self.report("key_focus_lost");
            }
        }
    }
    pub fn accepts_key(&self, foreground: Option<isize>) -> bool {
        self.status == Status::Recording
            && !self.awaiting_bound
            && self.owner.is_some()
            && self.owner == foreground
    }
    fn add(&mut self, start_ms: u64, end_ms: u64, action: Action) {
        let seq = self.completed.len();
        self.completed.push(Recorded {
            seq,
            start_ms,
            end_ms,
            action,
        });
    }
    pub fn message_control(&self) -> Option<isize> {
        self.chord.as_ref().and_then(|chord| chord.message_control)
    }
    pub fn observe_message_control(&mut self, focus: Option<isize>) {
        if self
            .message_control()
            .is_some_and(|owner| Some(owner) != focus)
        {
            self.abort_chord("key_control_changed");
        }
    }
    /// Some IMEs deliver VK_PROCESSKEY on press and the normal VK on release.
    /// Pair edges using the scan code only while the original focused control owns the chord.
    pub fn message_key(
        &mut self,
        now: u64,
        top: isize,
        control: isize,
        physical: Option<(u16, bool)>,
        reported: Key,
        down: bool,
    ) {
        if !self.accepts_key(Some(top)) {
            return;
        }
        self.observe_message_control(Some(control));
        let key = physical
            .and_then(|scan| {
                self.chord
                    .as_ref()?
                    .physical
                    .iter()
                    .find(|(s, _)| *s == scan)
                    .map(|(_, key)| *key)
            })
            .unwrap_or(reported);
        self.key(now, Some(top), key, down);
        if let Some(chord) = self.chord.as_mut() {
            chord.message_control = Some(control);
            if let Some(scan) = physical {
                chord.physical.retain(|(s, _)| *s != scan);
                if down {
                    chord.physical.push((scan, key));
                }
            }
        }
    }
    pub fn key(&mut self, now: u64, foreground: Option<isize>, key: Key, down: bool) {
        if !self.accepts_key(foreground) || !self.room(now) {
            return;
        }
        if self.button.is_some() {
            self.interrupt_pending("overlapping_keyboard_pointer");
            return;
        }
        let at = self.clock(now);
        if self.chord.is_none() && !down {
            return;
        }
        let chord = self.chord.get_or_insert_with(|| Chord {
            start_ms: at,
            events: vec![],
            down: vec![],
            message_control: None,
            physical: vec![],
        });
        if down {
            if !chord.down.contains(&key) {
                chord.down.push(key);
            }
        } else if let Some(n) = chord.down.iter().position(|k| k == &key) {
            chord.down.remove(n);
        } else {
            return;
        }
        chord.events.push(KeyEdge {
            offset_ms: at - chord.start_ms,
            down,
            key,
        });
        if chord.down.is_empty() {
            let done = self.chord.take().unwrap();
            self.add(
                done.start_ms,
                at,
                Action::KeySequence {
                    events: done.events,
                },
            );
        }
    }
    pub fn pointer_focus(&mut self, now: u64, hit: Option<isize>, foreground: Option<isize>) {
        if self.bound.is_some() && (!self.awaiting_bound || (self.bound == hit && self.bound == foreground)) {
            self.focus(now, foreground, true);
        }
    }
    pub fn pointer(&mut self, now: u64, x: i64, y: i64, hit: Option<isize>, event: Pointer) {
        if self.status != Status::Recording {
            return;
        }
        if self.awaiting_bound
            && (hit != self.bound || !matches!(event, Pointer::Down(_) | Pointer::Move))
        {
            // A click can activate this window. Buffer its in-window down/moves,
            // but never complete it unless the same bound target gained foreground.
            self.button = None;
            return;
        }
        let anchor = self
            .anchor(x, y)
            .filter(|_| hit.is_some() && (self.bound.is_none() || self.bound == hit));
        let Some(point) = anchor else {
            if self.bound.is_none() {
                self.owner = None;
                self.pending_owner = None;
                if self.chord.take().is_some() {
                    self.report("pointer_left_scope");
                }
            }
            if self.button.take().is_some() {
                self.report("drag_left_scope");
            }
            return;
        };
        if matches!(event, Pointer::Move) && self.button.is_none() {
            return;
        }
        if !self.room(now) {
            return;
        }
        let at = self.clock(now);
        match event {
            Pointer::Down(name) => {
                if self.button.is_some() || self.chord.is_some() {
                    self.interrupt_pending("overlap");
                    return;
                }
                if self.bound.is_none() {
                    self.owner = hit;
                    self.pending_owner = hit.map(|h| (h, now.saturating_add(250)));
                }
                self.button = Some(Button {
                    start_ms: at,
                    name,
                    from: point,
                    path: vec![],
                });
            }
            Pointer::Move => {
                if let Some(b) = &mut self.button {
                    if b.path.last().unwrap_or(&b.from) != &point {
                        b.path.push(point);
                    }
                }
            }
            Pointer::Up(name) => {
                let Some(button) = self.button.take() else {
                    return;
                };
                if name != button.name {
                    self.report("unpaired_button");
                    return;
                }
                let action = if button.path.is_empty() && button.from == point {
                    Action::Click {
                        button: name,
                        point,
                    }
                } else if name == "left" {
                    Action::Drag {
                        from: button.from,
                        path: button.path,
                        to: point,
                    }
                } else {
                    self.report("unsupported_drag_button");
                    return;
                };
                self.add(button.start_ms, at, action);
            }
            Pointer::Wheel(delta) => {
                if self.button.is_some() || self.chord.is_some() {
                    self.interrupt_pending("overlap");
                    return;
                }
                self.add(at, at, Action::Scroll { point, delta });
            }
            Pointer::Unsupported => {
                self.interrupt_pending("unsupported_button");
                self.report("unsupported_button");
            }
        }
    }
}

pub enum Pointer {
    Move,
    Down(&'static str),
    Up(&'static str),
    Wheel(i32),
    Unsupported,
}

#[cfg(test)]
mod tests {
    use super::*;
    const A: Key = Key::Named { key: "a" };
    const CTRL: Key = Key::Named { key: "ctrl" };
    fn seq(mode: ScopeKind) -> Sequence {
        Sequence::new(
            mode,
            MonitorRect {
                x: -100,
                y: 60,
                width: 101,
                height: 51,
            },
            if mode == ScopeKind::Window {
                Some(1)
            } else {
                None
            },
            1000,
        )
    }
    fn click(s: &mut Sequence, t: u64, owner: isize) {
        s.pointer(t, -50, 85, Some(owner), Pointer::Down("left"));
        s.pointer(t + 1, -50, 85, Some(owner), Pointer::Up("left"));
    }
    #[test]
    fn first_click_requires_the_bound_recipient_and_verified_foreground() {
        for (down_hit, down_focus, up_hit, up_focus, expected) in [
            (1, 1, 1, 1, 1), // both packets precede the polling loop
            (1, 2, 1, 1, 1), // down activates the chosen target
            (1, 2, 1, 2, 0), // activation never happened
            (1, 1, 1, 2, 0), // the original recipient lost focus before release
            (2, 2, 1, 1, 0), // foreign down
            (1, 2, 2, 1, 0), // foreign release
        ] {
            let mut s = seq(ScopeKind::Window);
            s.await_bound();
            s.pointer_focus(1001, Some(down_hit), Some(down_focus));
            s.pointer(1001,-50,85,Some(down_hit),Pointer::Down("left"));
            s.pointer_focus(1002, Some(up_hit), Some(up_focus));
            s.pointer(1002,-50,85,Some(up_hit),Pointer::Up("left"));
            assert_eq!(s.completed.len(), expected);
        }
    }
    #[test]
    fn ime_and_named_release_share_the_focused_physical_key() {
        let mut s = seq(ScopeKind::Window);
        let scan = Some((0x1e, false));
        let physical = Key::Physical {
            scan_code: 0x1e,
            extended: false,
        };
        s.message_key(1001, 1, 10, scan, physical, true);
        s.message_key(1002, 1, 10, scan, A, false);
        let Action::KeySequence { events } = &s.completed[0].action else {
            panic!()
        };
        assert_eq!(
            events
                .iter()
                .map(|edge| (edge.key, edge.down))
                .collect::<Vec<_>>(),
            [(physical, true), (physical, false)]
        );
        s.message_key(1003, 1, 10, scan, physical, true);
        s.observe_message_control(Some(11));
        s.message_key(1004, 1, 11, scan, A, false);
        assert_eq!(s.completed.len(), 1);
        assert!(s
            .interrupted
            .iter()
            .any(|i| i.reason == "key_control_changed"));
    }
    #[test]
    fn all_three_scopes_and_focus_ownership() {
        for mode in [ScopeKind::Display, ScopeKind::Region, ScopeKind::Window] {
            let mut s = seq(mode);
            assert_eq!(s.accepts_key(Some(1)), mode == ScopeKind::Window);
            s.key(1000, Some(2), A, true);
            assert!(s.completed.is_empty());
            click(&mut s, 1010, 1);
            assert!(s.accepts_key(Some(1)));
            for (ms, key, down) in [
                (1020, CTRL, true),
                (1020, A, true),
                (1022, A, false),
                (1030, CTRL, false),
            ] {
                s.key(ms, Some(1), key, down);
            }
            let Action::KeySequence { events } = &s.completed[1].action else {
                panic!()
            };
            assert_eq!(
                events
                    .iter()
                    .map(|k| (k.key, k.down, k.offset_ms))
                    .collect::<Vec<_>>(),
                [
                    (CTRL, true, 0),
                    (A, true, 0),
                    (A, false, 2),
                    (CTRL, false, 10)
                ]
            );
            s.focus(1040, Some(2), true);
            assert!(!s.accepts_key(Some(2)));
            assert!(!s.accepts_key(Some(1)));
            if mode == ScopeKind::Window {
                assert_eq!(s.status, Status::Paused);
                assert!(!s.resume(1090, Some(2)));
                assert!(s.resume(1100, Some(1)));
            } else {
                click(&mut s, 1100, 2);
                assert!(s.accepts_key(Some(2)));
            }
            assert!(s.finish());
            assert!(!s.cancel());
            let n = s.completed.len();
            click(&mut s, 1200, 1);
            s.key(1200, Some(1), A, true);
            s.key(1201, Some(1), A, false);
            assert_eq!(s.completed.len(), n);
        }
    }
    #[test]
    fn cross_boundary_drag_and_unpaired_keys_are_not_exported() {
        let mut s = seq(ScopeKind::Window);
        click(&mut s, 1001, 1);
        s.pointer(1010, -90, 70, Some(1), Pointer::Down("left"));
        s.pointer(1011, -70, 70, Some(1), Pointer::Move);
        s.pointer(1012, 1, 70, Some(1), Pointer::Move);
        s.pointer(1013, -90, 70, Some(1), Pointer::Up("left"));
        s.key(1015, Some(1), CTRL, true);
        assert!(s.finish());
        assert_eq!(s.completed.len(), 1);
        assert_eq!(
            s.interrupted
                .iter()
                .map(|e| (e.after, e.reason))
                .collect::<Vec<_>>(),
            [(1, "drag_left_scope"), (1, "unpaired_on_finish")]
        );
    }
    #[test]
    fn cancel_releases_and_manual_pause_excludes_elapsed_time() {
        let mut s = seq(ScopeKind::Display);
        click(&mut s, 1010, 1);
        s.pause(1020, "manual");
        s.resume(2020, Some(1));
        assert!(!s.accepts_key(Some(1)));
        click(&mut s, 2030, 1);
        assert_eq!(s.completed[1].start_ms, 30);
        assert!(s.cancel());
        assert!(s.completed.is_empty());
        assert!(!s.finish());
    }
}
