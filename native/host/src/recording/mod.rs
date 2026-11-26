//! Foreground recording session. Semantic actions exist only in the active desktop host.
mod commands;
pub(crate) mod desktop;
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
static CURRENT: Mutex<Option<Recording>> = Mutex::new(None);

struct Recording {
    id: String,
    session: String,
    context: InputContext,
    sequence: Arc<Mutex<Sequence>>,
    start: Instant,
    worker: windows::Worker,
}
fn busy() -> IpcError {
    IpcError::new("BUSY", "录制与活动输入执行互斥", true)
}
pub fn active() -> bool {
    CURRENT.lock().unwrap().as_ref().is_some_and(|r| {
        matches!(
            r.sequence.lock().unwrap().status,
            Status::Recording | Status::Paused
        )
    })
}
pub fn clear() {
    let prior = CURRENT.lock().unwrap().take();
    drop(prior);
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

pub fn command(session: &str, cmd: &str, p: &Value) -> Result<Value, IpcError> {
    let _admission = ADMISSION.lock().unwrap();
    let mut slot = CURRENT.lock().unwrap();
    if cmd == "start_recording" {
        if slot.as_ref().is_some_and(|r| {
            matches!(
                r.sequence.lock().unwrap().status,
                Status::Recording | Status::Paused
            )
        }) || crate::input::shared_gate().is_some_and(|g| !g.active(None).is_empty())
        {
            return Err(busy());
        }
        if p.as_object()
            .is_none_or(|m| m.keys().any(|k| k != "context"))
        {
            return Err(IpcError::invalid_params("录制仅接受 context"));
        }
        let context = InputContext::parse(p.get("context"))?;
        let (mode, rect, bound) = scope(&context)?;
        let mut state=Sequence::new(mode, rect, bound, 0);
        state.desktop=Some(desktop::Claim::acquire(desktop::Role::Recording)?);
        let sequence = Arc::new(Mutex::new(state));
        let start = Instant::now();
        let id = Uuid::new_v4().to_string();
        let worker = windows::Worker::start(sequence.clone(), rect, bound, start, Uuid::new_v4())?;
        *slot = Some(Recording {
            id,
            session: session.into(),
            context,
            sequence,
            start,
            worker,
        });
    }
    let r = slot
        .as_mut()
        .filter(|r| r.session == session)
        .ok_or_else(|| IpcError::invalid_state("当前会话没有录制"))?;
    if cmd != "start_recording"
        && (p["recording_id"].as_str() != Some(&r.id) || p.as_object().is_none_or(|m| m.len() != 1))
    {
        return Err(IpcError::invalid_params("录制 ID 不属于本会话"));
    }
    let mut s = r.sequence.lock().unwrap();
    match cmd {
        "pause_recording" if s.status == Status::Recording => {
            s.pause(r.start.elapsed().as_millis() as u64, "manual_pause")
        }
        "resume_recording" if s.status == Status::Paused => {
            let (_, rect, bound) = scope(&r.context)?;
            if !s.resume_with_scope(r.start.elapsed().as_millis() as u64, rect, bound) {
                return Err(IpcError::invalid_state("范围或身份已变化，请重新录制"));
            }
        }
        "finish_recording" if s.status == Status::Finished || s.finish() => (),
        "cancel_recording" if s.cancel() => (),
        "start_recording" | "get_recording_state" => (),
        _ => return Err(IpcError::invalid_state("录制已终结或当前状态不允许此操作")),
    }
    let mut state = json!({"recording_id":r.id,"status":s.status,"action_count":s.completed.len(),"interrupted":s.interrupted,
        "keyboard_source":if r.worker.native_commands.load(std::sync::atomic::Ordering::Acquire){"foreground_command_keys"}else{"focused_window_messages"},
        "keyboard_available":r.worker.keyboard_ready.load(std::sync::atomic::Ordering::Acquire), "receiver":format!("0x{:X}",r.worker.receiver)});
    if cmd == "finish_recording" {
        state["actions"] = json!(s.completed);
    }
    if matches!(s.status, Status::Finished | Status::Cancelled) {
        r.worker.stop();
    }
    Ok(state)
}
