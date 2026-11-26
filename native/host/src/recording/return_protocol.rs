//! Bootstrap sent over the helper's private stdin before the input desktop can change.
use serde::{Deserialize, Serialize};
use std::{ffi::c_void, ptr};

#[derive(Serialize, Deserialize)]
pub(crate) struct Bootstrap {
    pub(crate) station: String,
    pub(crate) original: String,
    pub(crate) private: String,
    pub(crate) host_pid: u32,
    // These are restricted, non-inheritable handles in the child process's handle table.
    pub(crate) host: usize,
    pub(crate) ready: usize,
    pub(crate) disarmed: usize,
}

#[link(name = "user32")]
extern "system" {
    fn GetUserObjectInformationW(
        object: *mut c_void,
        index: i32,
        buffer: *mut c_void,
        bytes: u32,
        needed: *mut u32,
    ) -> i32;
}

pub(crate) fn object_name(handle: isize) -> Option<String> {
    if handle == 0 {
        return None;
    }
    let mut bytes = 0;
    unsafe {
        GetUserObjectInformationW(handle as *mut c_void, 2, ptr::null_mut(), 0, &mut bytes);
    }
    if !(2..=65536).contains(&bytes) || bytes % 2 != 0 {
        return None;
    }
    let mut name = vec![0u16; bytes as usize / 2];
    if unsafe {
        GetUserObjectInformationW(
            handle as *mut c_void,
            2,
            name.as_mut_ptr().cast(),
            bytes,
            &mut bytes,
        )
    } == 0
    {
        return None;
    }
    String::from_utf16(name.strip_suffix(&[0])?).ok()
}
