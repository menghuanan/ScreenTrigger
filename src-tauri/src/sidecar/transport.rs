//! 命名管道 stdio 传输层（设计修订-01 分支 A；覆盖原文 §2.3 的 stdio 建立方式）。
//!
//! 每次引擎会话建立 3 条 Win32 命名管道，把可继承句柄经 `Stdio::from(File)`
//! 传入子进程；父进程保留对端句柄读写。子进程视角仍是普通 stdout/stdin/stderr，
//! **协议层 03（NDJSON over stdio）零变化，backend/main.py 零改动**。
//!
//! 通道拓扑（探针 P1 T1–T4 定案；见 raw/probe-transport-*.log）：
//!
//! | 通道 | server 端（PIPE_ACCESS_INBOUND，读） | client 端（GENERIC_WRITE，写） | 继承给子进程 |
//! |---|---|---|---|
//! | stdout | 父进程（drain 线程） | 子进程 | client |
//! | stderr | 父进程（drain 线程） | 子进程 | client |
//! | stdin  | 子进程（stdin 读端） | 父进程（Supervisor 写；关闭即 EOF） | **server**（角色反转） |
//!
//! stdin 反转依据：本沙箱过滤层拦截 GENERIC_READ 方向的管道客户端打开
//! （探针 exp 矩阵 4 组合全报 ERROR_PIPE_BUSY），而 GENERIC_WRITE 方向是
//! T1/T3 已验证可行的唯一路径；INBOUND server 作子进程 stdin 读端语义完全
//! 等价（父关 client → 子 EOF），且干净 Windows 上行为一致（双兼容，§7）。
//!
//! 继承控制：可继承端 SECURITY_ATTRIBUTES.bInheritHandle=TRUE；对端显式
//! SetHandleInformation 移除继承标志，防句柄泄漏（修订 §2 要点）。

use std::fs::File;
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::time::{SystemTime, UNIX_EPOCH};

use windows::core::{HRESULT, PCWSTR};
use windows::Win32::Foundation::{
    GENERIC_WRITE, HANDLE, HANDLE_FLAGS, HANDLE_FLAG_INHERIT, SetHandleInformation,
};
use windows::Win32::Security::SECURITY_ATTRIBUTES;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, FILE_FLAG_FIRST_PIPE_INSTANCE, FILE_SHARE_NONE, FILE_FLAGS_AND_ATTRIBUTES,
    OPEN_EXISTING, PIPE_ACCESS_INBOUND, SECURITY_IDENTIFICATION, SECURITY_SQOS_PRESENT,
};
use windows::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_WAIT,
};

/// ERROR_PIPE_CONNECTED（win32 535）：client 在 ConnectNamedPipe 前已连接，
/// 属正常路径。windows Error.code() 返回含设施位的 HRESULT（0x8007xxxx）。
const ERROR_PIPE_CONNECTED: HRESULT = HRESULT(0x8007_0217u32 as i32);
/// 单实例管道：会话独占，管道名唯一化已杜绝跨会话冲突（修订 §2）。
const SINGLE_INSTANCE: u32 = 1;
/// 管道缓冲区字节数（NDJSON 帧通常远小于此；2MiB 上限由协议层分帧处理）。
const PIPE_BUFFER_BYTES: u32 = 8192;

/// 建立失败的可读错误（Supervisor 侧映射为 last_error=`pipe_setup_failed: ...`）。
pub type PipeSetupError = String;

/// 子进程三通道 Stdio 三元组（一次性消费）。
#[derive(Debug)]
pub struct ChildStdio {
    pub stdin: Stdio,
    pub stdout: Stdio,
    pub stderr: Stdio,
}

use std::process::Stdio;

/// 一次引擎会话的三条命名管道句柄对。
///
/// 所有权/生命周期：`child_stdio()` 一次性消费子进程端；父端 server 句柄经
/// `take_*` 移交 drain 线程与 Supervisor；未消费的句柄在 `Drop` 时关闭——
/// 子进程侧随之 EOF/断裂，语义与匿名管道 piped 一致。
pub struct SessionPipes {
    stdout_server: Option<File>,      // 父进程读子进程 stdout
    stderr_server: Option<File>,      // 父进程读子进程 stderr
    stdout_client: Option<Stdio>,     // 子进程 stdout 写端（std spawn 内部副本交付）
    stderr_client: Option<Stdio>,     // 子进程 stderr 写端（同上）
    stdin_child_server: Option<Stdio>, // 子进程 stdin 读端（INBOUND server；反转方案，同机制交付）
    stdin_client: Option<File>,       // 父进程 stdin 写端（关闭即 EOF）
}

impl SessionPipes {
    /// 建立一次会话的 3 条命名管道（stdout/stderr/stdin）。
    ///
    /// 连接语义：client 端在 `CreateFileW` 时即连接到 server 实例，
    /// spawn 后只需对 stdout/stderr server 调 `confirm_connections()` 确认。
    pub fn build(session_id: &str) -> Result<SessionPipes, PipeSetupError> {
        // stdout：子写 → 父读（子进程写端经 std spawn 内部副本交付，见下）
        let (stdout_server, stdout_client) = make_pipe_pair("stdout", session_id)?;
        // stderr：子写 → 父读（同上）
        let (stderr_server, stderr_client) = make_pipe_pair("stderr", session_id)?;
        // stdin（反转）：父写 client → 子读 server（server 作子进程 stdin，同机制交付）
        let (stdin_child_server, stdin_client) = make_pipe_pair("stdin", session_id)?;

        Ok(SessionPipes {
            stdout_server: Some(stdout_server),
            stderr_server: Some(stderr_server),
            stdout_client: Some(Stdio::from(stdout_client)),
            stderr_client: Some(Stdio::from(stderr_client)),
            stdin_child_server: Some(Stdio::from(stdin_child_server)),
            stdin_client: Some(stdin_client),
        })
    }

    /// 一次性取走子进程三通道 Stdio（spawn 前；重复调用返回错误）。
    pub fn take_child_stdio(&mut self) -> Result<ChildStdio, PipeSetupError> {
        let stdin = self
            .stdin_child_server
            .take()
            .ok_or("子进程 stdin 已被取用（child_stdio 不可重复调用）")?;
        let stdout = self
            .stdout_client
            .take()
            .ok_or("子进程 stdout 已被取用（child_stdio 不可重复调用）")?;
        let stderr = self
            .stderr_client
            .take()
            .ok_or("子进程 stderr 已被取用（child_stdio 不可重复调用）")?;
        Ok(ChildStdio { stdin, stdout, stderr })
    }

    /// spawn 后确认 stdout/stderr server 连接状态（client 已连接 → 535 视为成功）。
    pub fn confirm_connections(&mut self) -> Result<(), PipeSetupError> {
        if let Some(server) = self.stdout_server.as_ref() {
            confirm_connected(server, "stdout")?;
        }
        if let Some(server) = self.stderr_server.as_ref() {
            confirm_connected(server, "stderr")?;
        }
        // stdin：server 在子进程侧，父进程无需确认（写失败由 send_envelope 处理）
        Ok(())
    }

    /// 父进程 stdout 读句柄（移交 drain 线程；幂等）。
    pub fn take_stdout_server(&mut self) -> Option<File> {
        self.stdout_server.take()
    }

    /// 父进程 stderr 读句柄（移交 drain 线程；幂等）。
    pub fn take_stderr_server(&mut self) -> Option<File> {
        self.stderr_server.take()
    }

    /// 父进程 stdin 写句柄（Supervisor 持有；单 writer）。
    pub fn take_stdin_client(&mut self) -> Option<File> {
        self.stdin_client.take()
    }
}

// ---------------------------------------------------------------------------
// 内部：管道建立原语
// ---------------------------------------------------------------------------

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 生成会话管道名：`\\.\pipe\ScreenTrigger\<pid>-<session 哈希>-<进程内序号>-<通道>`。
/// pid + session 哈希（SipHash via DefaultHasher）与每次启动新 session_id 共同
/// 保证唯一；进程内序号防同会话内同名（幂等重建不冲突）。
fn pipe_name(session_id: &str, channel: &str) -> String {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    session_id.hash(&mut hasher);
    let session_hash = hasher.finish();
    static BUILD_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let seq = BUILD_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    format!(
        r"\\.\pipe\ScreenTrigger\{}-{session_hash:016x}-{seq}-{channel}-{nanos}",
        std::process::id()
    )
}

/// 建立一对命名管道句柄，**双端原句柄均不可继承**（BUG-5 修复矩阵）。
///
/// BUG-5（QA R3）：旧实现把"交给子进程的一端"显式置可继承；该句柄经
/// `Stdio::from(File)` 传入 Command 后，std spawn 内部 DuplicateHandle
/// (bInheritHandle=TRUE) 会再造一个可继承副本，CreateProcess
/// (bInheritHandles=TRUE) 把两个副本都传给子进程——子进程关闭 fd 对应的
/// 副本后另一无名副本残留，管道写端永不闭合 → 父端无 EOF → 30s 看门狗
/// 误判 startup_timeout。
///
/// 修复：三通道统一为"双端原句柄不可继承"，子进程端句柄完全依赖 std
/// spawn 的内部副本交付（该副本恒为可继承并作为 STARTUPINFO 标准句柄），
/// 保证**每个句柄恰好一个副本到达子进程**——子进程关闭 fd 即闭合写端，
/// EOF 及时可达（探针 T6 实证：os.close(1) 后 EOF 411ms，进程仍存活）。
/// stdin 反转方案（server 作子进程 stdin 读端）沿用同一交付机制。
///
/// 返回（server 端 File，client 端 File）；两个句柄此后由 Rust RAII 管理。
fn make_pipe_pair(channel: &str, session_id: &str) -> Result<(File, File), PipeSetupError> {
    let name = pipe_name(session_id, channel);
    let name_w = wide(&name);

    // server：INBOUND（数据 client→server）；BUG-5：原句柄恒不可继承。
    // SAFETY：SECURITY_ATTRIBUTES 栈上有效；windows-0.62.2 返回裸 HANDLE，需判 invalid。
    let server = unsafe {
        CreateNamedPipeW(
            PCWSTR(name_w.as_ptr()),
            FILE_FLAGS_AND_ATTRIBUTES(PIPE_ACCESS_INBOUND.0 | FILE_FLAG_FIRST_PIPE_INSTANCE.0),
            PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            SINGLE_INSTANCE,
            PIPE_BUFFER_BYTES,
            PIPE_BUFFER_BYTES,
            0,
            Some(&SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: std::ptr::null_mut(),
                bInheritHandle: false.into(),
            }),
        )
    };
    if server.is_invalid() {
        return Err(format!("CreateNamedPipeW({name}) 返回无效句柄"));
    }
    // SAFETY：显式清除 server 继承标志（防句柄泄漏进子进程形成多余副本；
    // 若需交付子进程——如 stdin 反转的读端——由 std spawn 内部副本完成）。
    unsafe { SetHandleInformation(server, HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)) }
        .map_err(|e| format!("SetHandleInformation(server, {name}) 失败: {e}"))?;

    // client：GENERIC_WRITE 打开（连接即刻建立）；BUG-5：原句柄恒不可继承
    // （旧实现此处置可继承导致子进程双副本——见函数头注释）。
    // SAFETY：参数为栈上有效值；成功后句柄移交 File。
    let client = unsafe {
        CreateFileW(
            PCWSTR(name_w.as_ptr()),
            GENERIC_WRITE.0,
            FILE_SHARE_NONE,
            Some(&SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: std::ptr::null_mut(),
                bInheritHandle: false.into(),
            }),
            OPEN_EXISTING,
            FILE_FLAGS_AND_ATTRIBUTES(SECURITY_SQOS_PRESENT.0 | SECURITY_IDENTIFICATION.0),
            None,
        )
    }
    .map_err(|e| format!("CreateFileW(client, {name}) 失败: {e}"))?;
    // SAFETY：显式清除 client 继承标志（BUG-5 核心：父端原句柄不得可继承）。
    unsafe { SetHandleInformation(client, HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)) }
        .map_err(|e| format!("SetHandleInformation(client, {name}) 失败: {e}"))?;

    // SAFETY：原始句柄所有权移交 File（RAII）；此后不再直接使用 HANDLE。
    Ok((
        unsafe { File::from_raw_handle(server.0) },
        unsafe { File::from_raw_handle(client.0) },
    ))
}

/// 确认 server 端连接状态：client 在 CreateFileW 时已连接，
/// ConnectNamedPipe 预期返回 ERROR_PIPE_CONNECTED(535)。
fn confirm_connected(server: &File, channel: &str) -> Result<(), PipeSetupError> {
    // SAFETY：server 句柄由 File 持有；仅借用调用 ConnectNamedPipe。
    match unsafe { ConnectNamedPipe(HANDLE(server.as_raw_handle()), None) } {
        Ok(()) => Ok(()),
        Err(e) if e.code() == ERROR_PIPE_CONNECTED => Ok(()),
        Err(e) => Err(format!("ConnectNamedPipe({channel}) 失败: {e}")),
    }
}

// ---------------------------------------------------------------------------
// 单元测试（替身原则：不在单测中 spawn 真实 Python；跨进程 connect/EOF/洪流
// 行为留探针 T1–T4 与 QA 集成复验——修订 §6）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::Foundation::GetHandleInformation;

    /// 辅助：查询句柄当前继承标志。
    fn inherits(file: &File) -> bool {
        // GetHandleInformation 签名要求 *mut u32（裸 DWORD 出参）。
        let mut flags: u32 = 0;
        // SAFETY：只读查询，句柄由 File 持有。
        unsafe { GetHandleInformation(HANDLE(file.as_raw_handle()), &mut flags) }.is_ok()
            && (flags & HANDLE_FLAG_INHERIT.0) != 0
    }

    #[test]
    fn build_creates_three_channel_session() {
        let mut pipes = SessionPipes::build("session-test-build-1").expect("管道建立应成功");
        // 一次性消费子进程端成功
        assert!(pipes.take_child_stdio().is_ok());
        // 父端句柄可取且不重复
        assert!(pipes.take_stdout_server().is_some());
        assert!(pipes.take_stderr_server().is_some());
        assert!(pipes.take_stdin_client().is_some());
        assert!(pipes.take_stdout_server().is_none(), "stdout server 应幂等取空");
        assert!(pipes.take_stdin_client().is_none(), "stdin client 应幂等取空");
    }

    #[test]
    fn child_stdio_is_one_shot() {
        let mut pipes = SessionPipes::build("session-test-build-2").expect("管道建立应成功");
        assert!(pipes.take_child_stdio().is_ok());
        let err = pipes.take_child_stdio().unwrap_err();
        assert!(err.contains("不可重复调用"), "二次取用应报错: {err}");
    }

    #[test]
    fn handles_not_inheritable_single_copy_matrix() {
        // BUG-5 回归矩阵：三通道双端原句柄一律不可继承——子进程端由 std
        // spawn 内部 DuplicateHandle(bInheritHandle=TRUE) 副本交付，确保
        // 每个句柄恰好一个副本到达子进程（子进程关 fd 即闭合写端 → EOF）。
        // 注：跨进程 stdio 交付与 EOF 行为由探针 T6 验证（替身原则）。
        for (channel, tag) in [("stdout", "mx-a"), ("stderr", "mx-b"), ("stdin", "mx-c")] {
            let (server, client) =
                make_pipe_pair(channel, &format!("session-test-{tag}")).expect("管道建立应成功");
            assert!(!inherits(&server), "{channel} server 原句柄不可继承");
            assert!(!inherits(&client), "{channel} client 原句柄不可继承");
        }
    }

    #[test]
    fn pipe_names_are_unique_across_builds() {
        // 同 session_id 重复建立：名字含 pid+哈希+序号+纳秒，FIRST_PIPE_INSTANCE 不冲突
        let a = pipe_name("session-dup", "stdout");
        let b = pipe_name("session-dup", "stdout");
        assert_ne!(a, b, "两次生成的管道名必须不同（幂等重建不冲突）");
        assert!(a.starts_with(r"\\.\pipe\ScreenTrigger\"));
        // 名字格式 ...-{seq}-{channel}-{nanos}，通道名后仍有纳秒后缀
        assert!(a.contains("-stdout-"), "应含 stdout 通道段: {a}");
        // 不同通道名不同名
        assert_ne!(pipe_name("session-dup", "stdin"), pipe_name("session-dup", "stderr"));
    }

    #[test]
    fn rebuild_after_drop_is_allowed() {
        // Drop 释放句柄后，同名空间可继续使用（RAII 关闭语义）
        {
            let pipes = SessionPipes::build("session-test-build-4").expect("首次建立应成功");
            drop(pipes);
        }
        let pipes = SessionPipes::build("session-test-build-4").expect("Drop 后重建应成功");
        drop(pipes);
    }

    #[test]
    fn confirm_connections_is_ok_on_fresh_pair() {
        // 纯父进程内的连接确认（client 已在 CreateFileW 时连接 → 535）
        let (server, _client) =
            make_pipe_pair("confirm", "session-test-build-5").expect("管道建立应成功");
        assert!(confirm_connected(&server, "stdout").is_ok());
    }
}
