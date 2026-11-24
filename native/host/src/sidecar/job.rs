//! Windows Job Object 封装（设计说明 §2.2/§2.5；docs/07 §4 整树清理要求）。
//!
//! **本文件是 crate 内唯一允许出现 unsafe 代码的位置**；每个 unsafe 块均注明
//! 所调用 Win32 API 的语义与安全前提。
//!
//! 机制：spawn 成功后立即 `AssignProcessToJobObject`，此后 Python 将来派生的
//! 任何 worker 都落入同一 Job；应用无论正常退出还是被杀，`CloseHandle` 触发
//! `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 兜底终止整树，不留残留进程。

use std::mem::size_of;
use std::os::windows::io::AsRawHandle;
use std::process::Child;

use windows::Win32::Foundation::CloseHandle;
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, QueryInformationJobObject,
    SetInformationJobObject, JobObjectBasicAndIoAccountingInformation,
    JobObjectExtendedLimitInformation, JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows::Win32::System::Threading::TerminateProcess;
use windows::Win32::Foundation::HANDLE;

/// Job Object 句柄的所有权封装。
///
/// Send/Sync 说明：HANDLE 是 OS 原生句柄，Win32 层面跨线程使用合法；
/// 实际访问由 Supervisor 的 Mutex 串行化（唯一所有者），此处仅满足
/// `State<SupervisorHandle>` 对托管类型 Send+Sync 的编译期要求。
pub struct JobObject {
    handle: HANDLE,
}

// SAFETY：见类型注释；句柄不含线程亲和状态，所有权唯一归属 Supervisor。
unsafe impl Send for JobObject {}
unsafe impl Sync for JobObject {}

impl JobObject {
    /// 创建匿名 Job Object 并启用 `KILL_ON_JOB_CLOSE`（设计 §2.2 步骤 4）。
    pub fn new() -> Result<Self, String> {
        unsafe {
            // CreateJobObjectW(None, None)：创建无名 Job，句柄仅本进程可用。
            let handle: HANDLE = CreateJobObjectW(None, None)
                .map_err(|e| format!("CreateJobObjectW 失败: {e}"))?;

            // SetInformationJobObject(JobObjectExtendedLimitInformation)：
            // 设置 LimitFlags=KILL_ON_JOB_CLOSE —— 最后一个 Job 句柄关闭时，
            // 内核终止该 Job 关联的全部进程（整树兜底，不依赖 taskkill 轮询）。
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
            .map_err(|e| {
                let _ = CloseHandle(handle); // 失败路径先释放句柄再返回
                format!("SetInformationJobObject(KILL_ON_JOB_CLOSE) 失败: {e}")
            })?;

            Ok(Self { handle })
        }
    }

    /// 将子进程（及其未来派生的整棵进程树）关联到本 Job（设计 §2.2 步骤 6）。
    pub fn assign(&self, child: &Child) -> Result<(), String> {
        unsafe {
            // AssignProcessToJobObject(hjob, hprocess)：第一参为 Job 句柄、
            // 第二参为子进程句柄（windows-0.62.2 签名核对；两参同型 HANDLE，
            // 顺序颠倒编译器无法察觉——QA BUG-1）。把已有进程加入 Job 后，
            // 其子进程自动继承 Job 关联。
            AssignProcessToJobObject(self.handle, HANDLE(child.as_raw_handle()))
                .map_err(|e| format!("AssignProcessToJobObject 失败: {e}"))
        }
    }

    /// 关闭 Job 句柄（消费 self，保证只关一次）。
    ///
    /// 语义：CloseHandle 后本进程不再持有 Job 引用；若这是最后一个句柄，
    /// KILL_ON_JOB_CLOSE 立即终止 Job 内全部进程（含残留 worker）。
    pub fn close_kill_tree(self) {
        unsafe {
            // CloseHandle：引用计数减一；配合 KILL_ON_JOB_CLOSE 即整树终止兜底。
            let _ = CloseHandle(self.handle);
        }
    }

    /// Job 内是否仍有活跃进程（诊断视图 `job_object_active`，§4.1）。
    pub fn is_active(&self) -> bool {
        unsafe {
            // QueryInformationJobObject(BasicAndIoAccounting)：读取 ActiveProcesses
            // 计数；查询失败按 false 处理（诊断字段，不影响清理路径）。
            let mut info = JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION::default();
            match QueryInformationJobObject(
                Some(self.handle),
                JobObjectBasicAndIoAccountingInformation,
                &mut info as *mut _ as *mut core::ffi::c_void,
                size_of::<JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION>() as u32,
                None,
            ) {
                Ok(()) => info.BasicInfo.ActiveProcesses > 0,
                Err(_) => false,
            }
        }
    }
}

/// 非优雅兜底：TerminateProcess 强制终止 sidecar 进程（§2.5 步骤 7；
/// 2s 优雅期内未退出时调用，exit code 非 0）。
///
/// 安全前提：child 进程句柄仍有效（未 reap）；终止是异步请求，随后由
/// 调用方 `child.wait()` 回收。Job 内其余进程由 close_kill_tree 清理。
pub fn terminate_process(child: &Child, exit_code: u32) -> Result<(), String> {
    unsafe {
        // TerminateProcess：内核级强制终止，无清理回调；仅作优雅期超时后的兜底。
        TerminateProcess(HANDLE(child.as_raw_handle()), exit_code)
            .map_err(|e| format!("TerminateProcess 失败: {e}"))
    }
}
