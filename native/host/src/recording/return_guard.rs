//! Arm the companion next to this DLL before the first private desktop switch.
use super::protocol::Bootstrap;
use std::{
    ffi::OsString,
    io,
    os::windows::{
        ffi::OsStringExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
};
use windows::{
    core::PCWSTR,
    Win32::{
        Foundation::{DuplicateHandle, HANDLE, HMODULE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::{
            LibraryLoader::{
                GetModuleFileNameW, GetModuleHandleExW, GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            },
            Threading::{
                CreateEventW, GetCurrentProcess, SetEvent, WaitForMultipleObjects,
                WaitForSingleObject, EVENT_MODIFY_STATE, PROCESS_QUERY_LIMITED_INFORMATION,
            },
        },
    },
};

const SYNCHRONIZE: u32 = 0x0010_0000;

fn companion() -> io::Result<PathBuf> {
    let mut module = HMODULE::default();
    let mut path = vec![0; 32768];
    unsafe {
        GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            PCWSTR(companion as *const u16),
            &mut module,
        )?;
    }
    let n = unsafe { GetModuleFileNameW(Some(module), &mut path) } as usize;
    if n == 0 || n == path.len() {
        return Err(io::Error::last_os_error());
    }
    Ok(PathBuf::from(OsString::from_wide(&path[..n]))
        .with_file_name("recording-desktop-return.exe"))
}

fn event() -> io::Result<OwnedHandle> {
    let raw = unsafe { CreateEventW(None, true, false, PCWSTR::null()) }?;
    Ok(unsafe { OwnedHandle::from_raw_handle(raw.0) })
}

fn copy_into(child: HANDLE, source: HANDLE, access: u32) -> io::Result<usize> {
    let mut remote = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            source,
            child,
            &mut remote,
            access,
            false,
            Default::default(),
        )?
    };
    Ok(remote.0 as usize)
}

pub(super) struct ReturnGuard {
    process: Child,
    disarmed: OwnedHandle,
}
impl ReturnGuard {
    pub(super) fn arm(station: &str, original: &str, private: &str) -> io::Result<Self> {
        let ready = event()?;
        let disarmed = event()?;
        let mut process = Command::new(companion()?)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let child = HANDLE(process.as_raw_handle());
        let setup = (|| -> io::Result<()> {
            let config = Bootstrap {
                station: station.into(),
                original: original.into(),
                private: private.into(),
                host_pid: std::process::id(),
                host: copy_into(
                    child,
                    unsafe { GetCurrentProcess() },
                    SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION.0,
                )?,
                ready: copy_into(child, HANDLE(ready.as_raw_handle()), EVENT_MODIFY_STATE.0)?,
                disarmed: copy_into(child, HANDLE(disarmed.as_raw_handle()), SYNCHRONIZE)?,
            };
            let input = process
                .stdin
                .take()
                .ok_or_else(|| io::Error::other("缺少监护握手管道"))?;
            serde_json::to_writer(input, &config)?; // EOF completes this one-time message.
            let result = unsafe {
                WaitForMultipleObjects(&[child, HANDLE(ready.as_raw_handle())], false, 2000)
            };
            if result.0 != WAIT_OBJECT_0.0 + 1 {
                return Err(io::Error::other(format!(
                    "监护进程未就绪 (wait={:#x})",
                    result.0
                )));
            }
            Ok(())
        })();
        if let Err(error) = setup {
            // Our desktop has never been displayed; this newly created child owns no recording.
            let _ = process.kill();
            let _ = process.wait();
            return Err(error);
        }
        Ok(Self { process, disarmed })
    }
    pub(super) fn alive(&self) -> bool {
        unsafe { WaitForSingleObject(HANDLE(self.process.as_raw_handle()), 0) == WAIT_TIMEOUT }
    }
    pub(super) fn disarm(&self) -> io::Result<()> {
        unsafe {
            SetEvent(HANDLE(self.disarmed.as_raw_handle()))?;
        }
        Ok(())
    }
}
impl Drop for ReturnGuard {
    fn drop(&mut self) {
        // Session retains us throughout recovery. Also covers errors before presentation.
        let _ = self.disarm();
    }
}
