//! One foreground recording excludes other recordings and input leases on the same Win32 desktop.
//! Other input leases may coexist. The last handle releases a role, including on process death.
use crate::ipc::IpcError;

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Role {
    Input,
    Recording,
}

#[cfg(windows)]
mod platform {
    use super::{IpcError, Role};
    use sha2::{Digest, Sha256};
    use std::ffi::c_void;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use std::ptr;

    type Handle = *mut c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThreadId() -> u32;
        fn GetLastError() -> u32;
        fn CreateMutexW(attributes: Handle, owner: i32, name: *const u16) -> Handle;
        fn WaitForSingleObject(handle: Handle, millis: u32) -> u32;
        fn ReleaseMutex(handle: Handle) -> i32;
        fn CreateEventW(attributes: Handle, manual: i32, signaled: i32, name: *const u16)
            -> Handle;
        fn OpenEventW(access: u32, inherit: i32, name: *const u16) -> Handle;
    }
    #[link(name = "user32")]
    extern "system" {
        fn GetProcessWindowStation() -> Handle;
        fn GetThreadDesktop(thread: u32) -> Handle;
        fn GetUserObjectInformationW(
            object: Handle,
            index: i32,
            buffer: Handle,
            bytes: u32,
            needed: *mut u32,
        ) -> i32;
    }
    fn unavailable() -> IpcError {
        IpcError::new(
            "CAPABILITY_DENIED",
            "无法核对当前交互桌面的录制和输入占用",
            false,
        )
    }
    fn busy() -> IpcError {
        IpcError::new("BUSY", "此桌面已有冲突的录制或输入执行", true)
    }
    fn owned(handle: Handle) -> Result<OwnedHandle, IpcError> {
        if handle.is_null() {
            Err(unavailable())
        } else {
            Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
        }
    }
    fn object_name(object: Handle) -> Result<Vec<u16>, IpcError> {
        if object.is_null() {
            return Err(unavailable());
        }
        let mut bytes = 0;
        unsafe {
            GetUserObjectInformationW(object, 2, ptr::null_mut(), 0, &mut bytes);
        }
        if !(2..=65536).contains(&bytes) || bytes % 2 != 0 {
            return Err(unavailable());
        }
        let mut name = vec![0u16; bytes as usize / 2];
        if unsafe {
            GetUserObjectInformationW(object, 2, name.as_mut_ptr().cast(), bytes, &mut bytes)
        } == 0
        {
            return Err(unavailable());
        }
        Ok(name)
    }
    fn desktop_namespace() -> Result<String, IpcError> {
        let station = object_name(unsafe { GetProcessWindowStation() })?;
        let desktop = object_name(unsafe { GetThreadDesktop(GetCurrentThreadId()) })?;
        let mut hash = Sha256::new();
        // Win32 supplies a terminating NUL for each component.
        for unit in station.into_iter().chain(desktop) {
            hash.update(unit.to_le_bytes());
        }
        Ok(format!(
            "Local\\ScreenTrigger.Automation.Desktop.{:x}",
            hash.finalize()
        ))
    }
    fn name(namespace: &str, part: &str) -> Vec<u16> {
        format!("{namespace}.{part}")
            .encode_utf16()
            .chain([0])
            .collect()
    }
    struct Admission(OwnedHandle);
    impl Admission {
        fn acquire(namespace: &str) -> Result<Self, IpcError> {
            let mutex = owned(unsafe {
                CreateMutexW(ptr::null_mut(), 0, name(namespace, "admission").as_ptr())
            })?;
            match unsafe { WaitForSingleObject(mutex.as_raw_handle(), 250) } {
                0 | 0x80 => Ok(Self(mutex)), // acquired, or recovered after its owner died
                258 => Err(busy()),
                _ => Err(unavailable()),
            }
        }
    }
    impl Drop for Admission {
        fn drop(&mut self) {
            unsafe {
                ReleaseMutex(self.0.as_raw_handle());
            }
        }
    }
    fn present(name: &[u16]) -> Result<bool, IpcError> {
        let handle = unsafe { OpenEventW(0x0010_0000, 0, name.as_ptr()) };
        if handle.is_null() {
            return if unsafe { GetLastError() } == 2 {
                Ok(false)
            } else {
                Err(unavailable())
            };
        }
        drop(owned(handle)?);
        Ok(true)
    }
    pub(crate) struct Claim {
        _marker: OwnedHandle,
    }
    impl Claim {
        pub(crate) fn acquire(role: Role) -> Result<Self, IpcError> {
            let namespace = desktop_namespace()?;
            let _guard = Admission::acquire(&namespace)?;
            let record = name(&namespace, "recording");
            let input = name(&namespace, "input");
            if present(&record)? || (role == Role::Recording && present(&input)?) {
                return Err(busy());
            }
            let marker = if role == Role::Recording {
                &record
            } else {
                &input
            };
            let handle = owned(unsafe { CreateEventW(ptr::null_mut(), 1, 0, marker.as_ptr()) })?;
            Ok(Self { _marker: handle })
        }
    }
}
#[cfg(not(windows))]
mod platform {
    use super::{IpcError, Role};
    pub(crate) struct Claim;
    impl Claim {
        pub(crate) fn acquire(_: Role) -> Result<Self, IpcError> {
            Ok(Self)
        }
    }
}
pub(crate) use platform::Claim;
