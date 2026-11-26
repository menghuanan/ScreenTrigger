//! Foreground recording session. Semantic actions exist only in the active desktop host.
mod commands;
pub(crate) mod desktop;
mod isolated;
mod isolated_keys;
mod sequence;
mod windows;
use crate::input::{
    anchor::Scope,
    model::{InputContext, MonitorRect, RegionResolutionPort},
    wiring::SystemRegionResolution,
};
use crate::ipc::IpcError;
use sequence::{ScopeKind, Sequence, Status};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Instant;
use uuid::Uuid;

pub const COMMANDS: &[&str] = &[
    "start_recording",
    "get_recording_state",
    "pause_recording",
    "resume_recording",
    "finish_recording",
    "cancel_recording",
];
pub static ADMISSION: Mutex<()> = Mutex::new(());
pub const SELF_INPUT: usize = 0x5354494e;
struct Recordings {
    current: Option<Recording>,
    used_ids: BTreeSet<Uuid>,
    retired: Vec<RetiredWorker>,
}
static RECORDINGS: Mutex<Recordings> = Mutex::new(Recordings {
    current: None,
    used_ids: BTreeSet::new(),
    retired: Vec::new(),
});

struct RetiredWorker {
    worker: windows::Worker,
    desktop: Option<Arc<isolated::IsolatedDesktop>>,
}
impl RetiredWorker {
    fn finished(&self) -> bool {
        self.worker.finished() && self.desktop.as_ref().is_none_or(|d| d.watcher_finished())
    }
}
struct Recording {
    id: Uuid,
    session: String,
    context: InputContext,
    sequence: Arc<Mutex<Sequence>>,
    start: Instant,
    worker: windows::Worker,
    isolated: Option<Arc<isolated::IsolatedDesktop>>,
    isolated_scope: Option<ScopeKind>,
}
fn busy() -> IpcError {
    IpcError::new("BUSY", "录制与活动输入执行互斥", true)
}
pub fn active() -> bool {
    RECORDINGS.lock().unwrap().current.as_ref().is_some_and(|r| {
        matches!(
            r.sequence.lock().unwrap().status,
            Status::Recording | Status::Paused
        )
    })
}
pub fn clear() {
    let (prior, retired) = {
        let mut recordings = RECORDINGS.lock().unwrap();
        recordings.used_ids.clear();
        (recordings.current.take(), std::mem::take(&mut recordings.retired))
    };
    // Shutdown joins every recording worker and desktop watcher before the DLL may unload.
    // No registry or admission lock is held while their thread-owned resources are released.
    drop(prior);
    drop(retired);
}

fn scope(ctx: &InputContext) -> Result<(ScopeKind, MonitorRect, Option<isize>), IpcError> {
    let mode = if ctx.region.is_some() {
        Scope::Region
    } else if ctx.binding_id.is_some() {
        Scope::Window
    } else {
        Scope::Display
    };
    if ctx.background || ctx.allow_exit {
        return Err(IpcError::invalid_params("录制只接受限定范围的前台上下文"));
    }
    if let Some(id) = &ctx.binding_id {
        let service = crate::window::shared()
            .ok_or_else(|| IpcError::window_not_found("窗口服务尚未就绪"))?;
        let entry = service.lock().unwrap().bound(id)?;
        if !entry.live.visible || entry.live.minimized {
            return Err(IpcError::window_unavailable("请恢复绑定窗口"));
        }
        let base = &entry.live.rect;
        let rect = MonitorRect {
            x: base.x,
            y: base.y,
            width: base.width,
            height: base.height,
        };
        let chosen = if let Some(region) = &ctx.region {
            use crate::types::region::RegionSpace;
            if region.space != RegionSpace::WindowClientPhysical
                || region.binding_id.as_ref() != Some(id)
            {
                return Err(IpcError::invalid_params("选区不属于绑定客户区"));
            }
            let selected = MonitorRect {
                x: rect
                    .x
                    .checked_add(region.x)
                    .ok_or_else(|| IpcError::invalid_params("横坐标溢出"))?,
                y: rect
                    .y
                    .checked_add(region.y)
                    .ok_or_else(|| IpcError::invalid_params("纵坐标溢出"))?,
                width: region.width,
                height: region.height,
            };
            if !crate::input::anchor::covered(selected, &[rect]) {
                return Err(IpcError::region_limit_violation("选区超出客户区"));
            }
            selected
        } else {
            rect
        };
        crate::input::anchor::ScopeSnapshot::new(chosen, crate::input::win32::enum_monitors())?;
        return Ok((
            if mode == Scope::Region {
                ScopeKind::Region
            } else {
                ScopeKind::Window
            },
            chosen,
            Some(entry.live.identity.hwnd),
        ));
    }
    let current = SystemRegionResolution::new().anchor_scope(mode, ctx)?;
    Ok((
        if mode == Scope::Region {
            ScopeKind::Region
        } else {
            ScopeKind::Display
        },
        current.rect,
        None,
    ))
}

fn next_id(value: Option<&Value>, used: &BTreeSet<Uuid>) -> Result<Uuid, IpcError> {
    let id = match value {
        None => Uuid::new_v4(),
        Some(v) => v.as_str().and_then(|s| Uuid::try_parse(s).ok())
            .ok_or_else(|| IpcError::invalid_params("recording_id 需要 UUID"))?,
    };
    if used.contains(&id) {
        return Err(IpcError::invalid_params("此会话中的 recording_id 已经使用"));
    }
    Ok(id)
}

pub fn command(session: &str, cmd: &str, p: &Value) -> Result<Value, IpcError> {
    let _admission = ADMISSION.lock().unwrap();
    let mut recordings = RECORDINGS.lock().unwrap();
    if cmd == "start_recording" {
        if recordings.current.as_ref().is_some_and(|r| {
            matches!(
                r.sequence.lock().unwrap().status,
                Status::Recording | Status::Paused
            )
        }) || crate::input::shared_gate().is_some_and(|g| !g.active(None).is_empty())
        {
            return Err(busy());
        }
        if p.as_object()
            .is_none_or(|m| m.keys().any(|k| k != "context" && k != "isolated_app" && k != "recording_id"))
        {
            return Err(IpcError::invalid_params(
                "录制仅接受 context、isolated_app 和 recording_id",
            ));
        }
        let id = next_id(p.get("recording_id"), &recordings.used_ids)?;
        let context = InputContext::parse(p.get("context"))?;
        recordings.retired.retain(|old| !old.finished());
        let claim = desktop::Claim::acquire(desktop::Role::Recording)?;
        let isolated = p
            .get("isolated_app")
            .map(|value| {
                let spec = value.as_object().filter(|m| m.len() == 2).ok_or_else(|| {
                    IpcError::invalid_params("isolated_app 需要 executable 和 scope")
                })?;
                let executable = spec
                    .get("executable")
                    .and_then(Value::as_str)
                    .ok_or_else(|| IpcError::invalid_params("请选择本地 GUI 应用"))?;
                let mode = match spec.get("scope").and_then(Value::as_str) {
                    Some("window") if context.region.is_none() => ScopeKind::Window,
                    Some("region") if context.region.is_some() => ScopeKind::Region,
                    Some("display") if context.region.is_none() => ScopeKind::Display,
                    _ => return Err(IpcError::invalid_params("隔离范围和上下文不一致")),
                };
                if context.binding_id.is_some() || context.allow_exit || context.background {
                    return Err(IpcError::invalid_params("隔离录制使用本次新启动的前台应用"));
                }
                if mode != ScopeKind::Window {
                    scope(&context)?;
                }
                Ok((
                    mode,
                    Arc::new(isolated::IsolatedDesktop::launch(executable)?),
                ))
            })
            .transpose()?;
        let (mode, rect, bound) =
            recording_scope(&context, isolated.as_ref().map(|(m, d)| (*m, d)))?;
        let mut state = Sequence::new(mode, rect, bound, 0);
        state.desktop = Some(claim);
        let sequence = Arc::new(Mutex::new(state));
        let start = Instant::now();
        let isolated = isolated.map(|(_, desktop)| desktop);
        let worker = windows::Worker::start(
            sequence.clone(),
            rect,
            bound,
            start,
            Uuid::new_v4(),
            isolated.clone(),
        )?;
        recordings.used_ids.insert(id);
        let prior = recordings.current.replace(Recording {
            id,
            session: session.into(),
            context,
            sequence,
            start,
            worker,
            isolated_scope: isolated.as_ref().map(|_| mode),
            isolated,
        });
        if let Some(prior) = prior {
            // Keep only the resources still unwinding after the previous terminal
            // recording, and keep their joins off the synchronous start-reply path.
            prior.worker.stop();
            recordings.retired.push(RetiredWorker { worker: prior.worker, desktop: prior.isolated });
        }
    }
    let r = recordings.current
        .as_mut()
        .filter(|r| r.session == session)
        .ok_or_else(|| IpcError::invalid_state("当前会话没有录制"))?;
    if cmd != "start_recording"
        && (p["recording_id"].as_str().and_then(|s| Uuid::try_parse(s).ok()) != Some(r.id)
            || p.as_object().is_none_or(|m| m.len() != 1))
    {
        return Err(IpcError::invalid_params("录制 ID 不属于本会话"));
    }
    let mut s = r.sequence.lock().unwrap();
    match cmd {
        "pause_recording" if s.status == Status::Recording => {
            s.pause(r.start.elapsed().as_millis() as u64, "manual_pause")
        }
        "resume_recording" if s.status == Status::Paused => {
            let (_, rect, bound) =
                recording_scope(&r.context, r.isolated_scope.zip(r.isolated.as_ref()))?;
            if !s.resume_with_scope(r.start.elapsed().as_millis() as u64, rect, bound) {
                return Err(IpcError::invalid_state("范围或身份已变化，请重新录制"));
            }
        }
        "finish_recording" if s.status == Status::Finished || s.finish() => (),
        "cancel_recording" if s.cancel() => (),
        "start_recording" | "get_recording_state" => (),
        _ => return Err(IpcError::invalid_state("录制已终结或当前状态不允许此操作")),
    }
    let mut state = json!({"recording_id":r.id.to_string(),"status":s.status,"action_count":s.completed.len(),"interrupted":s.interrupted,
        "keyboard_source":if r.isolated_scope.is_some() {"isolated_control_keys"} else if r.worker.native_commands.load(std::sync::atomic::Ordering::Acquire){"foreground_command_keys"}else{"focused_window_messages"},
        "keyboard_available":s.status == Status::Recording && r.worker.keyboard_ready.load(std::sync::atomic::Ordering::Acquire), "receiver":format!("0x{:X}",r.worker.receiver)});
    if let Some(desktop) = &r.isolated {
        state["isolated_app"] = json!({"desktop_name":desktop.name(),"pid":desktop.pid()});
    }
    if cmd == "finish_recording" || (cmd == "start_recording" && s.status == Status::Finished) {
        state["actions"] = json!(s.completed);
    }
    if matches!(s.status, Status::Finished | Status::Cancelled) {
        r.worker.stop();
        drop(s);
        if let Some(desktop) = &r.isolated {
            desktop.restore()?;
        }
    }
    Ok(state)
}

fn recording_scope(
    context: &InputContext,
    isolated: Option<(ScopeKind, &Arc<isolated::IsolatedDesktop>)>,
) -> Result<(ScopeKind, MonitorRect, Option<isize>), IpcError> {
    let Some((mode, desktop)) = isolated else {
        return scope(context);
    };
    let selected = if mode == ScopeKind::Window {
        None
    } else {
        Some(scope(context)?)
    };
    let desktop = desktop.clone();
    std::thread::spawn(move || {
        let _thread_desktop = desktop.enter_thread()?;
        // Window discovery and the stop indicator share the five-second IPC deadline.
        let deadline = Instant::now() + std::time::Duration::from_secs(2);
        while desktop.alive() && Instant::now() < deadline {
            if let Some((hwnd, (_, r, _))) = desktop
                .main_window()
                .and_then(|hwnd| crate::window::system::recording_geometry(hwnd).map(|g| (hwnd, g)))
            {
                if let Some(selected) = selected {
                    return Ok(selected);
                }
                let rect = MonitorRect {
                    x: r.x,
                    y: r.y,
                    width: r.width,
                    height: r.height,
                };
                crate::input::anchor::ScopeSnapshot::new(
                    rect,
                    crate::input::win32::enum_monitors(),
                )?;
                return Ok((ScopeKind::Window, rect, Some(hwnd)));
            }
            std::thread::sleep(std::time::Duration::from_millis(30));
        }
        Err(IpcError::window_unavailable(
            "隔离桌面上未出现所选应用的可见窗口",
        ))
    })
    .join()
    .map_err(|_| IpcError::window_unavailable("无法定位隔离窗口"))?
}

#[cfg(test)]
mod id_tests {
    use super::*;

    #[test]
    fn caller_can_query_a_lost_start_and_cannot_reuse_an_earlier_identity() {
        let prior = Uuid::new_v4();
        let used = BTreeSet::from([prior]);
        let chosen = Uuid::new_v4();
        assert_eq!(next_id(Some(&json!(chosen.to_string())), &used).unwrap(), chosen);
        assert_ne!(next_id(None, &used).unwrap(), prior);
        assert!(next_id(Some(&json!(prior.to_string().to_uppercase())), &used).is_err());
        assert!(next_id(Some(&json!("invalid")), &used).is_err());
    }
}
