//! Restore a single recording desktop if its owning host terminates.
#![cfg_attr(windows, windows_subsystem = "windows")]
#[cfg(windows)]
#[path = "../recording/return_protocol.rs"]
mod protocol;

#[cfg(windows)]
mod platform {
    use super::protocol::{object_name, Bootstrap};
    use std::{
        io::{self, Read},
        thread,
        time::Duration,
    };
    use windows::Win32::{
        Foundation::{HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::Threading::{
            GetProcessId, SetEvent, WaitForMultipleObjects, WaitForSingleObject, INFINITE,
        },
    };

    const READ_OBJECTS: u32 = 1;
    const SWITCH: u32 = 0x100;
    #[link(name = "user32")]
    extern "system" {
        fn GetProcessWindowStation() -> isize;
        fn OpenDesktopW(name: *const u16, flags: u32, inherit: i32, access: u32) -> isize;
        fn OpenInputDesktop(flags: u32, inherit: i32, access: u32) -> isize;
        fn SwitchDesktop(desktop: isize) -> i32;
        fn CloseDesktop(desktop: isize) -> i32;
    }

    struct Desktop(isize);
    impl Desktop {
        fn checked(raw: isize) -> io::Result<Self> {
            if raw == 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(Self(raw))
            }
        }
        fn input(access: u32) -> io::Result<Self> {
            Self::checked(unsafe { OpenInputDesktop(0, 0, access) })
        }
    }
    impl Drop for Desktop {
        fn drop(&mut self) {
            unsafe {
                CloseDesktop(self.0);
            }
        }
    }

    fn host_exited(host: HANDLE, disarmed: HANDLE) -> io::Result<bool> {
        // Windows returns the lowest signaled index. An already disarmed recording
        // stays disarmed when the host exits at the same time.
        match unsafe { WaitForMultipleObjects(&[disarmed, host], false, INFINITE) }.0 {
            n if n == WAIT_OBJECT_0.0 => Ok(false),
            n if n == WAIT_OBJECT_0.0 + 1 => Ok(true),
            _ => Err(io::Error::last_os_error()),
        }
    }

    pub(super) fn run() -> io::Result<()> {
        let config: Bootstrap = serde_json::from_reader(io::stdin().lock().take(65536))?;
        let host = HANDLE(config.host as _);
        let ready = HANDLE(config.ready as _);
        let disarmed = HANDLE(config.disarmed as _);
        let suffix = config.private.strip_prefix("ScreenTrigger-Recording-");
        if !suffix.is_some_and(|s| uuid::Uuid::try_parse(s).is_ok_and(|id| id.to_string() == s))
            || config.private == config.original
            || config.host_pid == 0
            || unsafe { GetProcessId(host) } != config.host_pid
            || unsafe { WaitForSingleObject(host, 0) } != WAIT_TIMEOUT
            || object_name(unsafe { GetProcessWindowStation() }).as_deref() != Some(&config.station)
        {
            return Err(io::Error::other("invalid recording owner"));
        }
        // Arm on the original input desktop, before the host is permitted to switch.
        // A retained handle for each name prevents either object disappearing and being reused.
        let original = Desktop::input(READ_OBJECTS | SWITCH)?;
        let name: Vec<_> = config.private.encode_utf16().chain([0]).collect();
        let private = Desktop::checked(unsafe { OpenDesktopW(name.as_ptr(), 0, 0, READ_OBJECTS) })?;
        if object_name(original.0).as_deref() != Some(&config.original)
            || object_name(private.0).as_deref() != Some(&config.private)
        {
            return Err(io::Error::other("unrecognized recording desktops"));
        }
        unsafe {
            SetEvent(ready)?;
        }
        if !host_exited(host, disarmed)? {
            return Ok(());
        }
        loop {
            let current = Desktop::input(READ_OBJECTS)
                .ok()
                .and_then(|d| object_name(d.0));
            match current.as_deref() {
                // A different visible desktop belongs to whoever selected it.
                Some(name) if name != config.private => return Ok(()),
                Some(_) => unsafe {
                    SwitchDesktop(original.0);
                },
                // A secure desktop can temporarily deny inspection. Recheck after unlock.
                None => (),
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
        use windows::{core::PCWSTR, Win32::System::Threading::CreateEventW};

        #[test]
        fn a_completed_disarm_wins_even_if_the_host_has_already_exited() {
            let event = |signaled| {
                let handle = unsafe { CreateEventW(None, true, signaled, PCWSTR::null()) }.unwrap();
                unsafe { OwnedHandle::from_raw_handle(handle.0) }
            };
            let owner_event = event(true);
            let release_event = event(false);
            let owner = HANDLE(owner_event.as_raw_handle());
            let release = HANDLE(release_event.as_raw_handle());
            assert!(host_exited(owner, release).unwrap());
            unsafe { SetEvent(release) }.unwrap();
            assert!(!host_exited(owner, release).unwrap());
        }
    }
}

#[cfg(windows)]
fn main() -> std::process::ExitCode {
    if platform::run().is_ok() {
        std::process::ExitCode::SUCCESS
    } else {
        std::process::ExitCode::FAILURE
    }
}

#[cfg(not(windows))]
fn main() -> std::process::ExitCode {
    eprintln!("Recording desktop recovery requires Windows.");
    std::process::ExitCode::FAILURE
}
