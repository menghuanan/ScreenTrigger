//! Session-scoped TXT output grants. Only a trusted picker supplies a destination path.
use crate::ipc::{HostHandler, IpcError};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Seek, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration, Instant};
use uuid::Uuid;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{GENERIC_READ, GENERIC_WRITE, HANDLE};
use windows::Win32::Storage::FileSystem::{
    FileDispositionInfo, GetFileInformationByHandle, MoveFileExW, ReplaceFileW,
    SetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, FILE_DISPOSITION_INFO, DELETE,
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_DELETE_ON_CLOSE,
    FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    MOVEFILE_WRITE_THROUGH, REPLACE_FILE_FLAGS,
};

type Result<T> = std::result::Result<T, IpcError>;
fn storage(e: io::Error) -> IpcError {
    IpcError::new("STORAGE_ERROR", &format!("文字导出失败：{e}"), false)
}
fn conflict() -> IpcError {
    IpcError::new(
        "CONFLICT",
        "目标已更改或动作内容冲突，请核对文件后重新选择",
        false,
    )
}
fn denied() -> IpcError {
    IpcError::new(
        "CAPABILITY_DENIED",
        "文字输出授权已失效或目标身份不符",
        false,
    )
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
struct Version {
    file_id: u64,
    volume: u32,
    sha256: String,
}

fn identity(file: &File) -> Result<BY_HANDLE_FILE_INFORMATION> {
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe { GetFileInformationByHandle(HANDLE(file.as_raw_handle()), &mut info) }
        .map_err(|error| storage(io::Error::other(error)))?;
    Ok(info)
}

fn check_path(path: &Path) -> Result<File> {
    if !path.is_absolute()
        || !path
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("txt"))
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
        || !matches!(path.components().next(), Some(Component::Prefix(p))
            if matches!(p.kind(), std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)))
    {
        return Err(denied());
    }
    for c in path.components() {
        if let Component::Normal(name) = c {
            if name.to_string_lossy().contains(':') || name.to_string_lossy().ends_with(['.', ' '])
            {
                return Err(denied());
            }
        }
    }
    let parent = path.parent().ok_or_else(denied)?;
    for part in parent.ancestors() {
        if fs::symlink_metadata(part)
            .map_err(storage)?
            .file_attributes()
            & FILE_ATTRIBUTE_REPARSE_POINT.0
            != 0
        {
            return Err(denied());
        }
    }
    OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ.0 | FILE_SHARE_WRITE.0)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS.0)
        .open(parent)
        .map_err(storage)
}

// Keep a handle denying other writers for the lifetime of the snapshot. Delete sharing
// allows atomic replacement. Identity and digest are checked again at commit.
fn source(path: &Path) -> Result<Option<File>> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(storage(e)),
        Ok(m) if !m.is_file() || m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT.0 != 0 => {
            return Err(denied())
        }
        _ => (),
    }
    let f = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ.0 | FILE_SHARE_DELETE.0)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT.0)
        .open(path)
        .map_err(storage)?;
    let info = identity(&f)?;
    if info.nNumberOfLinks != 1 || info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 != 0 {
        return Err(denied());
    }
    Ok(Some(f))
}

fn version(path: &Path) -> Result<Option<Version>> {
    let Some(mut f) = source(path)? else {
        return Ok(None);
    };
    file_version(&mut f).map(Some)
}

fn file_version(f: &mut File) -> Result<Version> {
    let info = identity(f)?;
    f.rewind().map_err(storage)?;
    let mut hasher = Sha256::new();
    io::copy(f, &mut hasher).map_err(storage)?;
    Ok(Version {
        file_id: ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
        volume: info.dwVolumeSerialNumber,
        sha256: format!("{:x}", hasher.finalize()),
    })
}

#[derive(Deserialize, Serialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Append,
    Overwrite,
}

struct Target {
    path: PathBuf,
    _parent: File,
    expected: Mutex<Option<Version>>,
    key: String,
}
struct Prepared {
    target: Arc<Target>,
    mode: Mode,
    expires: Instant,
}
struct Grant {
    target: Arc<Target>,
    mode: Mode,
    live: AtomicBool,
}
#[derive(Default)]
struct Book {
    prepared: HashMap<String, Prepared>,
    grants: HashMap<String, Arc<Grant>>,
    paths: HashMap<String, Weak<Target>>,
}

pub struct Service {
    session: String,
    receipts: PathBuf,
    book: Mutex<Book>,
    io: Mutex<()>,
}
static ACTIVE: OnceLock<Mutex<Option<Arc<Service>>>> = OnceLock::new();

pub fn begin(session: &str, data: PathBuf) -> Arc<Service> {
    let next = Arc::new(Service {
        session: session.into(),
        receipts: data.join("text-output-receipts"),
        book: Mutex::default(),
        io: Mutex::default(),
    });
    let mut slot = ACTIVE.get_or_init(Mutex::default).lock().unwrap();
    if let Some(old) = slot.replace(next.clone()) {
        old.revoke_all();
    }
    next
}

fn active(session: &str) -> Result<Arc<Service>> {
    ACTIVE
        .get()
        .and_then(|s| s.lock().unwrap().clone())
        .filter(|s| s.session == session)
        .ok_or_else(denied)
}
pub fn prepare(session: &str, path: &str, mode: Mode) -> Result<Value> {
    active(session)?.prepare(Path::new(path), mode)
}
pub fn discard(session: &str, token: &str) -> Result<Value> {
    active(session)?.book.lock().unwrap().prepared.remove(token);
    Ok(json!({"ok":true}))
}
pub fn revoke(session: &str, execution: &str) {
    if let Ok(service) = active(session) {
        service.close(execution);
    }
}
pub fn end() {
    if let Some(store) = ACTIVE.get().and_then(|s| s.lock().unwrap().take()) {
        store.revoke_all();
    }
}

#[derive(Serialize, Deserialize)]
struct Receipt {
    id: String,
    content: String,
    stage: UuidName,
    before: Option<Version>,
    after: Version,
    bytes: u64,
}

// Only parsed UUIDs become filenames, including names read back from an interrupted run.
#[derive(Serialize, Deserialize)]
#[serde(transparent)]
struct UuidName(String);
impl UuidName {
    fn path(&self, target: &Path) -> Result<PathBuf> {
        let id = Uuid::parse_str(&self.0).map_err(|_| conflict())?;
        Ok(target.with_file_name(format!(".ScreenTrigger-{id}.tmp")))
    }
    fn backup(&self, target: &Path) -> Result<PathBuf> {
        Ok(self.path(target)?.with_extension("previous"))
    }
}
struct Scratch(PathBuf);
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn persisted(path: &Path, receipt: &Receipt) -> Result<()> {
    let draft = Scratch(path.with_extension(format!("{}.tmp", Uuid::new_v4())));
    let mut file = File::create_new(&draft.0).map_err(storage)?;
    serde_json::to_writer(&mut file, receipt).map_err(|e| storage(io::Error::other(e)))?;
    file.sync_all().map_err(storage)?;
    drop(file);
    fs::rename(&draft.0, path).map_err(storage)
}

fn read_receipt(path: &Path) -> Result<Option<Receipt>> {
    match File::open(path) {
        Ok(f) => serde_json::from_reader(f).map(Some).map_err(|_| conflict()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(storage(e)),
    }
}

fn settle(path: &Path, journal: &Path) -> Result<()> {
    let pending = journal.join("pending.json");
    let Some(record) = read_receipt(&pending)? else {
        return Ok(());
    };
    if record.id.len() != 64 || !record.id.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(conflict());
    }
    let stage = record.stage.path(path)?;
    let backup = record.stage.backup(path)?;
    let current = version(path)?;
    let displaced = version(&backup)?;
    if displaced.is_some() && displaced != record.before {
        // A different file was renamed over the selected target between the last identity
        // check and ReplaceFileW. Restore the displaced external file if our output is
        // still present; otherwise retain both external versions for manual resolution.
        if current.as_ref() == Some(&record.after) {
            fs::rename(&backup, path).map_err(storage)?;
        }
        fs::rename(&pending, journal.join(format!("{}.unknown", record.id))).map_err(storage)?;
        return Err(conflict());
    }
    if current.as_ref() == Some(&record.after) {
        if displaced.is_some() {
            fs::remove_file(&backup).map_err(storage)?;
        }
        fs::rename(&pending, journal.join(format!("{}.json", record.id))).map_err(storage)?;
    } else if current == record.before {
        fs::remove_file(&pending).map_err(storage)?;
    } else {
        fs::rename(&pending, journal.join(format!("{}.unknown", record.id))).map_err(storage)?;
        return Err(conflict());
    }
    if version(&stage)?.as_ref() == Some(&record.after) {
        fs::remove_file(stage).map_err(storage)?;
    }
    Ok(())
}

fn exclusive(path: &Path, key: &str) -> Result<File> {
    OpenOptions::new().create_new(true).write(true).share_mode(0)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE.0 | FILE_FLAG_OPEN_REPARSE_POINT.0)
        .open(path.with_file_name(format!(".ScreenTrigger-{key}.lock"))).map_err(storage)
}

fn delete_on_close(file: &File, delete: bool) -> Result<()> {
    let disposition = FILE_DISPOSITION_INFO { DeleteFile: delete };
    unsafe {
        SetFileInformationByHandle(HANDLE(file.as_raw_handle()), FileDispositionInfo,
            std::ptr::from_ref(&disposition).cast(), size_of::<FILE_DISPOSITION_INFO>() as u32)
    }.map_err(|e| storage(io::Error::other(e)))
}

fn wide(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain([0]).collect()
}

fn append_previous(source: File, output: &mut File) -> Result<()> {
    let mut reader = BufReader::new(source);
    let mut line = String::new();
    let mut first = true;
    let mut separator = false;
    loop {
        line.clear();
        if reader.read_line(&mut line).map_err(storage)? == 0 {
            break;
        }
        let text = if first {
            line.strip_prefix('\u{feff}').unwrap_or(&line)
        } else {
            &line
        };
        output.write_all(text.as_bytes()).map_err(storage)?;
        separator = !text.is_empty() && !text.ends_with('\n');
        first = false;
    }
    if separator {
        output.write_all(b"\r\n").map_err(storage)?;
    }
    Ok(())
}

impl Service {
    fn prepare(&self, path: &Path, mode: Mode) -> Result<Value> {
        let _io = self.io.lock().unwrap();
        let parent = check_path(path)?;
        let name = path.file_name().ok_or_else(denied)?;
        let info = identity(&parent)?;
        let key = digest(
            format!(
                "{}:{}:{}:{}",
                info.dwVolumeSerialNumber,
                info.nFileIndexHigh,
                info.nFileIndexLow,
                name.to_string_lossy().to_lowercase()
            )
            .as_bytes(),
        );
        let journal = self.receipts.join(&key);
        if journal.join("pending.json").exists() {
            let _lock = exclusive(path, &key)?;
            settle(path, &journal)?;
        }
        let current = version(path)?;
        let mut book = self.book.lock().unwrap();
        book.prepared
            .retain(|_, item| item.expires > Instant::now());
        book.paths.retain(|_, v| v.strong_count() > 0);
        let known = book
            .paths
            .get(&key)
            .and_then(Weak::upgrade)
            .filter(|target| *target.expected.lock().unwrap() == current);
        let target = known.unwrap_or_else(|| {
            Arc::new(Target {
                path: path.into(),
                _parent: parent,
                expected: Mutex::new(current),
                key: key.clone(),
            })
        });
        book.paths.insert(key, Arc::downgrade(&target));
        let token = Uuid::new_v4().to_string();
        book.prepared.insert(
            token.clone(),
            Prepared {
                target,
                mode,
                expires: Instant::now() + Duration::from_secs(300),
            },
        );
        Ok(json!({"output_token":token,"name":name.to_string_lossy()}))
    }
    fn bind(&self, execution: &str, token: &str) -> Result<Value> {
        let mut book = self.book.lock().unwrap();
        if book.grants.contains_key(execution) {
            return Err(conflict());
        }
        let item = book.prepared.remove(token).ok_or_else(denied)?;
        if item.expires <= Instant::now() {
            return Err(denied());
        }
        book.grants.insert(
            execution.into(),
            Arc::new(Grant {
                target: item.target,
                mode: item.mode,
                live: AtomicBool::new(true),
            }),
        );
        Ok(json!({"ok":true}))
    }
    fn close(&self, execution: &str) {
        if let Some(grant) = self.book.lock().unwrap().grants.remove(execution) {
            grant.live.store(false, Ordering::Release);
        }
    }
    fn revoke_all(&self) {
        let mut book = self.book.lock().unwrap();
        book.prepared.clear();
        for (_, grant) in book.grants.drain() {
            grant.live.store(false, Ordering::Release);
        }
    }
    fn commit(&self, execution: &str, action: &str, text: &str) -> Result<Value> {
        let grant = self
            .book
            .lock()
            .unwrap()
            .grants
            .get(execution)
            .cloned()
            .ok_or_else(denied)?;
        let _io = self.io.lock().unwrap();
        if !grant.live.load(Ordering::Acquire) {
            return Err(denied());
        }
        let target = &grant.target;
        let path = &target.path;
        if text.is_empty() {
            return Ok(json!({"bytes_written":0}));
        }
        let _parent = check_path(path)?;
        // A second installed or portable process uses the same lock in the chosen directory.
        let _lock = exclusive(path, &target.key)?;
        let journal = self.receipts.join(&target.key);
        fs::create_dir_all(&journal).map_err(storage)?;
        settle(path, &journal)?;
        let id = digest(format!("{}\0{}\0{}", self.session, execution, action).as_bytes());
        let content = digest(text.as_bytes());
        let saved = journal.join(format!("{id}.json"));
        if journal.join(format!("{id}.unknown")).exists() {
            return Err(conflict());
        }
        let mut expected = target.expected.lock().unwrap();
        if let Some(previous) = read_receipt(&saved)? {
            if previous.content != content {
                return Err(conflict());
            }
            if *expected == previous.before && version(path)?.as_ref() == Some(&previous.after) {
                *expected = Some(previous.after);
            }
            return Ok(json!({"bytes_written":previous.bytes}));
        }
        let previous = source(path)?;
        if *expected != version(path)? {
            return Err(conflict());
        }
        let stage_name = UuidName(Uuid::new_v4().to_string());
        let scratch = Scratch(stage_name.path(path)?);
        let mut stage = OpenOptions::new().create_new(true)
            .access_mode(GENERIC_READ.0 | GENERIC_WRITE.0 | DELETE.0).share_mode(0)
            .open(&scratch.0).map_err(storage)?;
        delete_on_close(&stage, true)?;
        if matches!(grant.mode, Mode::Append) {
            if let Some(previous) = &previous {
                append_previous(previous.try_clone().map_err(storage)?, &mut stage)?;
            }
        }
        stage.write_all(text.as_bytes()).map_err(storage)?;
        stage.sync_all().map_err(storage)?;
        let after = file_version(&mut stage)?;
        let record = Receipt {
            id,
            content,
            stage: stage_name,
            before: expected.clone(),
            after,
            bytes: text.len() as u64,
        };
        let pending = journal.join("pending.json");
        persisted(&pending, &record)?;
        // Before this point a killed writer automatically unlinks the scratch file. From
        // here on the durable journal identifies its file ID and content for recovery.
        delete_on_close(&stage, false)?;
        drop(stage);
        if !grant.live.load(Ordering::Acquire) {
            return Err(denied());
        }
        if version(path)? != *expected {
            return Err(conflict());
        }
        let from = wide(&scratch.0);
        let to = wide(path);
        if expected.is_none() {
            // No REPLACE_EXISTING: a file created externally at the last moment survives.
            unsafe {
                MoveFileExW(
                    PCWSTR(from.as_ptr()),
                    PCWSTR(to.as_ptr()),
                    MOVEFILE_WRITE_THROUGH,
                )
            }
            .map_err(|e| storage(io::Error::other(e)))?;
        } else {
            let backup = wide(&record.stage.backup(path)?);
            unsafe { ReplaceFileW(PCWSTR(to.as_ptr()), PCWSTR(from.as_ptr()), PCWSTR(backup.as_ptr()),
                                  REPLACE_FILE_FLAGS(0), None, None) }.map_err(|e| storage(io::Error::other(e)))?;
        }
        settle(path, &journal)?;
        *expected = Some(record.after.clone());
        Ok(json!({"bytes_written":record.bytes}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        root: PathBuf,
        service: Arc<Service>,
        path: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(Uuid::new_v4().to_string());
            fs::create_dir(&root).unwrap();
            let service = Arc::new(Service {
                session: "s-test".into(),
                receipts: root.join("receipts"),
                book: Mutex::default(),
                io: Mutex::default(),
            });
            let path = root.join("out.txt");
            Self {
                root,
                service,
                path,
            }
        }
        fn grant(&self, id: &str, mode: Mode) -> String {
            let prepared = self.service.prepare(&self.path, mode).unwrap();
            let token = prepared["output_token"].as_str().unwrap();
            self.service.bind(id, token).unwrap();
            token.to_owned()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            self.service.revoke_all();
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    #[test]
    fn append_overwrite_empty_bom_and_duplicates() {
        let f = Fixture::new();
        let s = &f.service;
        let spent = f.grant("a", Mode::Append);
        assert!(!f.path.exists());
        assert_eq!(s.bind("b", &spent).unwrap_err().code, "CAPABILITY_DENIED");
        s.commit("a", "zero", "").unwrap();
        assert!(!f.path.exists());
        for (id, text) in [
            ("1", "A\r\n"),
            ("2", "A\r\n"),
            ("2", "A\r\n"),
            ("3", "B\r\n"),
            ("4", "A\r\n"),
        ] {
            s.commit("a", id, text).unwrap();
        }
        assert_eq!(fs::read(&f.path).unwrap(), b"A\r\nA\r\nB\r\nA\r\n");
        assert_eq!(s.commit("a", "2", "B\r\n").unwrap_err().code, "CONFLICT");
        s.close("a");
        assert_eq!(
            s.commit("a", "5", "X\r\n").unwrap_err().code,
            "CAPABILITY_DENIED"
        );
        fs::write(&f.path, "\u{feff}old").unwrap();
        f.grant("b", Mode::Append);
        s.commit("b", "1", "日本語\r\n").unwrap();
        assert_eq!(fs::read_to_string(&f.path).unwrap(), "old\r\n日本語\r\n");
        f.grant("c", Mode::Overwrite);
        s.commit("c", "0", "").unwrap();
        assert_eq!(fs::read_to_string(&f.path).unwrap(), "old\r\n日本語\r\n");
        s.commit("c", "2", "한글\r\n").unwrap();
        assert_eq!(fs::read_to_string(&f.path).unwrap(), "한글\r\n");
    }

    #[test]
    fn external_changes_locked_files_encoding_and_expiry() {
        let f = Fixture::new();
        let s = &f.service;
        fs::write(&f.path, "original").unwrap();
        let token = s.prepare(&f.path, Mode::Append).unwrap()["output_token"]
            .as_str()
            .unwrap()
            .to_owned();
        s.book
            .lock()
            .unwrap()
            .prepared
            .get_mut(&token)
            .unwrap()
            .expires = Instant::now();
        assert_eq!(
            s.bind("expired", &token).unwrap_err().code,
            "CAPABILITY_DENIED"
        );
        f.grant("first", Mode::Append);
        fs::write(&f.path, "external").unwrap();
        assert_eq!(
            s.commit("first", "x", "text\r\n").unwrap_err().code,
            "CONFLICT"
        );
        assert_eq!(fs::read_to_string(&f.path).unwrap(), "external");
        fs::write(&f.path, [0xff]).unwrap();
        f.grant("second", Mode::Append);
        assert_eq!(
            s.commit("second", "x", "text\r\n").unwrap_err().code,
            "STORAGE_ERROR"
        );
        assert_eq!(fs::read(&f.path).unwrap(), [0xff]);
        f.grant("third", Mode::Overwrite);
        let lock = File::options()
            .read(true)
            .share_mode(0)
            .open(&f.path)
            .unwrap();
        assert_eq!(
            s.commit("third", "1", "text\r\n").unwrap_err().code,
            "STORAGE_ERROR"
        );
        drop(lock);
        s.commit("third", "1", "text\r\n").unwrap();
        assert_eq!(fs::read_to_string(&f.path).unwrap(), "text\r\n");
        assert!(fs::read_dir(&f.root).unwrap().all(|r| !r
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".ScreenTrigger-")));
    }

    #[test]
    fn concurrent_grants_and_recovery_after_replace() {
        let f = Fixture::new();
        f.grant("x", Mode::Append);
        f.grant("y", Mode::Append);
        std::thread::scope(|scope| {
            for id in ["x", "y"] {
                let service = f.service.clone();
                scope.spawn(move || service.commit(id, "1", &format!("{id}\r\n")).unwrap());
            }
        });
        let text = fs::read_to_string(&f.path).unwrap();
        assert!(text == "x\r\ny\r\n" || text == "y\r\nx\r\n");
        f.service.commit("x", "last", "X\r\n").unwrap();
        let before = fs::read(&f.path).unwrap();
        let key = f.service.book.lock().unwrap().grants["x"]
            .target
            .key
            .clone();
        let dir = f.service.receipts.join(key);
        let id = digest(b"s-test\0x\0last");
        // Exact durable state after the OS replaced the target but before the
        // final receipt rename. Re-open a fresh service and reauthorize.
        fs::rename(dir.join(format!("{id}.json")), dir.join("pending.json")).unwrap();
        let reopened = Service {
            session: "s-test".into(),
            receipts: f.service.receipts.clone(),
            book: Mutex::default(),
            io: Mutex::default(),
        };
        let token = reopened.prepare(&f.path, Mode::Append).unwrap();
        reopened
            .bind("x", token["output_token"].as_str().unwrap())
            .unwrap();
        reopened.commit("x", "last", "X\r\n").unwrap();
        assert_eq!(fs::read(&f.path).unwrap(), before);
        assert!(!dir.join("pending.json").exists());
    }
}

pub struct Handler(pub Arc<Service>);
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase", deny_unknown_fields)]
enum Operation {
    Bind {
        execution_id: String,
        output_token: String,
    },
    Commit {
        execution_id: String,
        action_id: String,
        text: String,
    },
    Close {
        execution_id: String,
    },
}
impl HostHandler for Handler {
    fn handle(&mut self, _: &str, params: &Value) -> Result<Value> {
        match serde_json::from_value(params.clone())
            .map_err(|_| IpcError::invalid_params("文字导出请求格式错误"))?
        {
            Operation::Bind {
                execution_id,
                output_token,
            } => self.0.bind(&execution_id, &output_token),
            Operation::Commit {
                execution_id,
                action_id,
                text,
            } => self.0.commit(&execution_id, &action_id, &text),
            Operation::Close { execution_id } => {
                self.0.close(&execution_id);
                Ok(json!({"ok":true}))
            }
        }
    }
}
