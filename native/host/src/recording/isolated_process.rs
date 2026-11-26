//! Launch exactly the selected GUI image, with no inherited handles, under the current token.
use crate::ipc::IpcError;
use std::{
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom},
    mem::size_of,
    os::windows::{
        ffi::OsStrExt,
        fs::OpenOptionsExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::{Component, Path, Prefix},
};
use windows::{
    core::{PCWSTR, PWSTR},
    Win32::{
        Foundation::{HANDLE, WAIT_TIMEOUT},
        System::{
            JobObjects::{
                CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{
                CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
                UpdateProcThreadAttribute, WaitForSingleObject, EXTENDED_STARTUPINFO_PRESENT,
                LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                STARTUPINFOEXW,
            },
        },
    },
};

fn invalid_image() -> IpcError {
    IpcError::invalid_params("请选择本机已安装的普通 Windows 图形应用 .exe")
}

fn local_exe(path: &Path) -> bool {
    path.is_absolute()
        && matches!(path.components().next(), Some(Component::Prefix(p))
        if matches!(p.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
        && path
            .extension()
            .is_some_and(|v| v.eq_ignore_ascii_case("exe"))
}

fn gui_image(f: &mut std::fs::File) -> std::io::Result<bool> {
    // Subsystem has the same offset in the PE32 and PE32+ optional headers.
    let mut dos = [0; 64];
    f.read_exact(&mut dos)?;
    if &dos[..2] != b"MZ" {
        return Ok(false);
    }
    let pe = u32::from_le_bytes(dos[60..64].try_into().unwrap()) as u64;
    f.seek(SeekFrom::Start(pe))?;
    let mut header = [0; 24 + 70];
    f.read_exact(&mut header)?;
    let word = |at| u16::from_le_bytes([header[at], header[at + 1]]);
    Ok(pe >= 64
        && &header[..4] == b"PE\0\0"
        && word(20) >= 70
        && word(22) & 0x2002 == 2
        && matches!(word(24), 0x10b | 0x20b)
        && word(24 + 68) == 2)
}

fn launch_error(error: windows::core::Error) -> IpcError {
    IpcError::new(
        "CAPABILITY_DENIED",
        &format!("隔离应用启动失败：{error}"),
        false,
    )
}

struct Attributes {
    _storage: Vec<usize>,
    list: LPPROC_THREAD_ATTRIBUTE_LIST,
}
impl Attributes {
    fn new(job: &HANDLE) -> Result<Self, IpcError> {
        unsafe {
            let mut bytes = 0;
            let _ = InitializeProcThreadAttributeList(None, 1, None, &mut bytes);
            if bytes == 0 {
                return Err(launch_error(windows::core::Error::from_thread()));
            }
            let mut storage = vec![0usize; bytes.div_ceil(size_of::<usize>())];
            let list = LPPROC_THREAD_ATTRIBUTE_LIST(storage.as_mut_ptr().cast());
            InitializeProcThreadAttributeList(Some(list), 1, None, &mut bytes)
                .map_err(launch_error)?;
            let attributes = Self {
                _storage: storage,
                list,
            };
            // Assign at creation, so a host exit can never leave an unassigned suspended child.
            UpdateProcThreadAttribute(
                list,
                0,
                PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
                Some((job as *const HANDLE).cast()),
                size_of::<HANDLE>(),
                None,
                None,
            )
            .map_err(launch_error)?;
            Ok(attributes)
        }
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        unsafe {
            DeleteProcThreadAttributeList(self.list);
        }
    }
}

pub(super) struct Target {
    pub pid: u32,
    pub initial_thread: u32,
    process: OwnedHandle,
}
impl Target {
    pub fn alive(&self) -> bool {
        unsafe { WaitForSingleObject(HANDLE(self.process.as_raw_handle()), 0) == WAIT_TIMEOUT }
    }
    pub fn launch(executable: &str, desktop: &str) -> Result<(Self, OwnedHandle), IpcError> {
        let chosen = Path::new(executable);
        if executable.contains(['\0', '"']) || !local_exe(chosen) {
            return Err(invalid_image());
        }
        let path = chosen.canonicalize().map_err(|_| invalid_image())?;
        if !local_exe(&path) {
            return Err(invalid_image());
        }
        let mut image = OpenOptions::new()
            .read(true)
            .share_mode(1)
            .open(&path)
            .map_err(|_| invalid_image())?;
        if !gui_image(&mut image).unwrap_or(false) {
            return Err(invalid_image());
        }
        let exe: Vec<_> = path.as_os_str().encode_wide().chain([0]).collect();
        let cwd: Vec<_> = path
            .parent()
            .ok_or_else(invalid_image)?
            .as_os_str()
            .encode_wide()
            .chain([0])
            .collect();
        let mut argv: Vec<_> = [34u16]
            .into_iter()
            .chain(path.as_os_str().encode_wide())
            .chain([34, 0])
            .collect();
        let mut desktop: Vec<_> = desktop.encode_utf16().chain([0]).collect();
        unsafe {
            let raw_job = CreateJobObjectW(None, None).map_err(launch_error)?;
            let job = OwnedHandle::from_raw_handle(raw_job.0);
            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                raw_job,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            )
            .map_err(launch_error)?;
            let attributes = Attributes::new(&raw_job)?;
            let mut startup = STARTUPINFOEXW::default();
            startup.StartupInfo.cb = size_of_val(&startup) as u32;
            startup.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
            startup.lpAttributeList = attributes.list;
            let mut info = PROCESS_INFORMATION::default();
            CreateProcessW(
                PCWSTR(exe.as_ptr()),
                Some(PWSTR(argv.as_mut_ptr())),
                None,
                None,
                false,
                EXTENDED_STARTUPINFO_PRESENT,
                None,
                PCWSTR(cwd.as_ptr()),
                &startup.StartupInfo,
                &mut info,
            )
            .map_err(launch_error)?;
            drop(OwnedHandle::from_raw_handle(info.hThread.0));
            Ok((
                Self {
                    pid: info.dwProcessId,
                    initial_thread: info.dwThreadId,
                    process: OwnedHandle::from_raw_handle(info.hProcess.0),
                },
                job,
            ))
        }
    }
}
