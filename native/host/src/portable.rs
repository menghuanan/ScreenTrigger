//! Release data-root selection. A portable manifest explicitly opts the executable into
//! the self-contained layout; installed executables retain their installation binding.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

pub const MARKER: &str = "ScreenTrigger.portable.json";
const LAYOUT: &[u8] = b"{\"format\":1,\"data\":\"data\"}";

#[derive(Deserialize)]
struct Marker { format: u32, version: String, mode: String, data: String, core_sha256: String }

#[derive(Deserialize)]
struct Entry { path: String, bytes: u64, sha256: String }

fn regular(path: &Path) -> Result<fs::Metadata, String> {
    let m = fs::symlink_metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if m.file_attributes() & 0x400 != 0 { return Err(format!("不接受重解析路径 {}",path.display())); }
    }
    if m.file_type().is_symlink() { return Err(format!("不接受链接 {}",path.display())); }
    Ok(m)
}

fn plain_parents(path: &Path, checked: &mut HashSet<PathBuf>) -> Result<(), String> {
    if checked.contains(path) { return Ok(()); }
    if let Some(parent) = path.parent() { plain_parents(parent,checked)?; }
    if !regular(path)?.is_dir() { return Err(format!("不是目录 {}",path.display())); }
    checked.insert(path.to_owned());
    Ok(())
}

fn check_files(root: &Path, entries: &[Entry]) -> Result<(), String> {
    let mut checked = HashSet::new();
    let mut buffer = vec![0; 256 * 1024];
    for entry in entries {
        let relative = Path::new(&entry.path);
        if relative.components().any(|p| !matches!(p, Component::Normal(_))) || entry.path.contains(':')
            || entry.path.contains('\\') || entry.path.is_empty() {
            return Err(format!("清单相对路径无效 {}",entry.path));
        }
        let path = root.join(relative);
        plain_parents(path.parent().unwrap(), &mut checked)?;
        let info = regular(&path)?;
        if !info.is_file() || info.len() != entry.bytes { return Err(format!("核心文件长度不符 {}",entry.path)); }
        let mut file = File::open(&path).map_err(|e| format!("{}: {e}",entry.path))?;
        let mut hash = Sha256::new();
        loop {
            let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
            if count == 0 { break; }
            hash.update(&buffer[..count]);
        }
        if format!("{:x}", hash.finalize()) != entry.sha256 { return Err(format!("核心文件哈希不符 {}",entry.path)); }
    }
    Ok(())
}

fn verify(root: &Path, marker: &Marker) -> Result<(), String> {
    let inventory = root.join("portable-core.json");
    regular(&inventory)?;
    let raw = fs::read(inventory).map_err(|e| e.to_string())?;
    if format!("{:x}",Sha256::digest(&raw)) != marker.core_sha256 { return Err("便携核心文件清单哈希不符".into()); }
    let entries: Vec<Entry> = serde_json::from_slice(&raw).map_err(|e| e.to_string())?;
    for required in ["ScreenTrigger.exe","screen_trigger_lib.dll","resources/python/python.exe","resources/backend/main.py"] {
        if !entries.iter().any(|e|e.path == required) { return Err(format!("核心清单缺少 {required}")); }
    }
    std::thread::scope(|scope| {
        let size = entries.len().div_ceil(4);
        let checks: Vec<_> = entries.chunks(size).map(|group| scope.spawn(move || check_files(root,group))).collect();
        for job in checks { job.join().map_err(|_| "核心文件校验线程中止".to_string())??; }
        Ok(())
    })
}

fn writable(dir: &Path) -> Result<(), String> {
    let test = dir.join(format!(".write-{}.tmp",uuid::Uuid::new_v4()));
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(0x04000000); // FILE_FLAG_DELETE_ON_CLOSE
    }
    let file = options.open(&test).map_err(|e| format!("{} 不可写。请将整个便携目录复制到可写的本地目录后重试。{e}",dir.display()))?;
    drop(file);
    #[cfg(not(windows))]
    fs::remove_file(test).map_err(|e|e.to_string())?;
    Ok(())
}

#[cfg(windows)]
fn local_volume(path: &Path) -> Result<(), String> {
    use std::ffi::{OsStr, OsString};
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use std::path::Prefix;
    #[link(name="kernel32")]
    extern "system" {
        fn GetVolumePathNameW(name:*const u16,root:*mut u16,capacity:u32) -> i32;
        fn GetDriveTypeW(root:*const u16) -> u32;
        fn QueryDosDeviceW(name:*const u16,target:*mut u16,capacity:u32) -> u32;
    }
    fn wide(name:&OsStr) -> Vec<u16> { name.encode_wide().chain(Some(0)).collect() }
    fn local(path:&Path) -> bool {
        let mut volume=vec![0u16;32768];
        unsafe { GetVolumePathNameW(wide(path.as_os_str()).as_ptr(),volume.as_mut_ptr(),volume.len() as u32)!=0
            && matches!(GetDriveTypeW(volume.as_ptr()),2|3|6) }
    }
    if local(path) { return Ok(()); }
    let refused="请将整个便携目录复制到本地可写磁盘或移动磁盘。";
    // Windows can reject GetVolumePathNameW on the root of a SUBST drive.
    // Accept only an actual drive-root alias pointing to a plain local directory.
    let Some(Component::Prefix(prefix))=path.components().next() else { return Err(refused.into()); };
    let letter=match prefix.kind() { Prefix::Disk(c)|Prefix::VerbatimDisk(c) if path.parent().is_none()=>c,
        _=>return Err(refused.into()) };
    let device=[letter as u16, b':' as u16, 0];
    let mut targets=vec![0u16;32768];
    if unsafe{QueryDosDeviceW(device.as_ptr(),targets.as_mut_ptr(),targets.len() as u32)}==0 { return Err(refused.into()); }
    let first=&targets[..targets.iter().position(|c|*c==0).unwrap_or(targets.len())];
    let Some(target)=first.strip_prefix(&[92,63,63,92]) else { return Err(refused.into()); };
    let target=PathBuf::from(OsString::from_wide(target));
    if !target.is_absolute() || !matches!(target.components().next(),Some(Component::Prefix(p)) if matches!(p.kind(),Prefix::Disk(_))) {
        return Err(refused.into());
    }
    plain_parents(&target,&mut HashSet::new())?;
    let physical=fs::canonicalize(&target).map_err(|e|e.to_string())?;
    if !local(&physical) { return Err(refused.into()); }
    Ok(())
}

pub fn data_root(exe_root: &Path) -> Result<(PathBuf,bool), String> {
    let marker_file = exe_root.join(MARKER);
    if !marker_file.try_exists().map_err(|e|e.to_string())? {
        let installed = fs::read_to_string(exe_root.join("data-root.txt"))
            .map_err(|_| "缺少便携清单或安装数据绑定，请重新完整解压或修复安装。".to_string())?;
        let path=PathBuf::from(installed.trim());
        if !path.is_absolute() { return Err("安装数据绑定须为绝对路径".into()); }
        return Ok((path,false));
    }
    #[cfg(windows)]
    local_volume(exe_root)?;
    plain_parents(exe_root, &mut HashSet::new())?;
    regular(&marker_file)?;
    let marker: Marker = serde_json::from_slice(&fs::read(marker_file).map_err(|e|e.to_string())?)
        .map_err(|e|format!("便携清单损坏：{e}"))?;
    if marker.format != 1 || marker.version != "automation-v2" || marker.data != "data" || marker.mode != "portable" {
        return Err("便携清单版本或路径不匹配".into());
    }
    verify(exe_root,&marker)?;
    writable(exe_root)?;
    let data = exe_root.join("data");
    fs::create_dir_all(&data).map_err(|e|format!("无法建立便携数据目录，请将整个目录复制到可写位置。{e}"))?;
    regular(&data)?;
    writable(&data)?;
    let lock_path=data.join(".portable-layout.lock");
    if lock_path.try_exists().map_err(|e|e.to_string())? { regular(&lock_path)?; }
    let mut options=OpenOptions::new();
    options.read(true).write(true).create(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0);
    }
    let _lock=options.open(lock_path).map_err(|e|format!("便携目录正被初始化或不可写：{e}"))?;
    let layout=data.join("portable-layout.json");
    if layout.try_exists().map_err(|e|e.to_string())? {
        regular(&layout)?;
        if fs::read(&layout).map_err(|e|e.to_string())? != LAYOUT { return Err("便携数据布局损坏或不兼容，请恢复备份".into()); }
    } else {
        let tmp=data.join(format!(".layout-{}.tmp",uuid::Uuid::new_v4()));
        let mut out=File::create_new(&tmp).map_err(|e|e.to_string())?;
        out.write_all(LAYOUT).and_then(|_|out.sync_all()).map_err(|e|e.to_string())?;
        drop(out);
        fs::rename(&tmp,&layout).map_err(|e|e.to_string())?;
    }
    for part in ["cache", "cache/temp", "cache/paddlex", "cache/profile", "models", "backups", "logs"] {
        let path=data.join(part);
        fs::create_dir_all(&path).map_err(|e|format!("{}: {e}",path.display()))?;
        plain_parents(&path,&mut HashSet::new())?;
        writable(&path)?;
    }
    Ok((data,true))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn marker_requires_verified_core_and_data_follows_the_executable() {
        let root=std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        fs::create_dir(&root).unwrap();
        let installed=root.join("external");
        assert!(data_root(&root).is_err());
        fs::write(root.join("data-root.txt"),installed.to_str().unwrap()).unwrap();
        assert_eq!(data_root(&root).unwrap(),(installed,false));
        let mut entries=vec![];
        for file in ["ScreenTrigger.exe","screen_trigger_lib.dll","resources/python/python.exe","resources/backend/main.py"] {
            let target=root.join(file);
            fs::create_dir_all(target.parent().unwrap()).unwrap();
            fs::write(target,b"abc").unwrap();
            entries.push(json!({"path":file,"bytes":3,"sha256":format!("{:x}",Sha256::digest(b"abc"))}));
        }
        let manifest=serde_json::to_vec(&entries).unwrap();
        fs::write(root.join("portable-core.json"),&manifest).unwrap();
        fs::write(root.join(MARKER),json!({"format":1,"mode":"portable","version":"automation-v2","data":"data",
            "core_sha256":format!("{:x}",Sha256::digest(manifest))}).to_string()).unwrap();
        assert_eq!(data_root(&root).unwrap(),(root.join("data"),true));
        fs::write(root.join("resources/backend/main.py"),b"def").unwrap();
        assert!(data_root(&root).unwrap_err().contains("哈希"));
        fs::remove_dir_all(root).unwrap();
    }
}
