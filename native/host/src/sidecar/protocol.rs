//! NDJSON 协议最小解析（TASK-004 骨架子集，严格对齐 docs/03-IPC协议.md）。
//!
//! 范围（设计 §3.3 protocol.rs）：
//! - UTF-8、一行一个 JSON、LF 分帧（`extract_complete_lines`，供流式缓冲）；
//! - `ProtocolEnvelope` 最小解析（type/session_id/protocol_version 校验；
//!   跨 session 拒绝由 Supervisor 依据 session_id 相等性执行——03：旧会话消息全部拒绝）；
//! - system_ready / hello response 数据提取（versions/capabilities）。
//!
//! 不实现（TASK-005）：完整路由、pending map、去重、背压、seq 缺口恢复、
//! 连续畸形帧关会话。不发明字段：信封字段名与 03 逐字对齐。

use serde::Serialize;
use serde_json::Value;

use super::{CapabilityView, VersionsView};

/// 当前支持协议 major（03：协议 major 不一致拒绝业务请求）。
pub const SUPPORTED_PROTOCOL_VERSION: u32 = 1;

/// session_id 约束（设计 §4.2）：非空、≤128 字符、仅 `[A-Za-z0-9-]`、
/// 以 "session-" 前缀开头（Rust 侧生成 `"session-" + uuid.v4()`）。
pub const SESSION_ID_MAX_LENGTH: usize = 128;
const SESSION_ID_PREFIX: &str = "session-";

/// host_request 命令白名单（docs/03 宿主命令目录逐字；§3.1 hostbridge.rs：
/// 白名单内骨架期回 CAPABILITY_DENIED 的 host_response，白名单外回
/// UNKNOWN_COMMAND——host_request 不进入 UI 命令路由）。
pub const HOST_REQUEST_COMMANDS: [&str; 6] = [
    "input_action",
    "window_snapshot",
    "query_pointer",
    "acquire_input_lease",
    "release_input_lease",
    "text_output",
];

/// 校验 host_request 的 command 是否在白名单内。
pub fn is_host_request_command(command: &str) -> bool {
    HOST_REQUEST_COMMANDS.contains(&command)
}

/// 解析错误（本骨架仅计数与日志，不构造 PROTOCOL_ERROR 信封——TASK-005 范围）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolParseError {
    EmptyLine,
    MalformedJson(String),
    MissingField(&'static str),
    UnsupportedProtocolVersion(i64),
    InvalidSessionId(String),
    UnknownMessageType(String),
    InvalidFieldType(&'static str),
}

impl std::fmt::Display for ProtocolParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyLine => write!(f, "空行"),
            Self::MalformedJson(e) => write!(f, "JSON 解析失败: {e}"),
            Self::MissingField(k) => write!(f, "缺少必填字段: {k}"),
            Self::UnsupportedProtocolVersion(v) => {
                write!(f, "协议版本不支持: {v}（期望 {SUPPORTED_PROTOCOL_VERSION}）")
            }
            Self::InvalidSessionId(s) => write!(f, "session_id 非法: {s}"),
            Self::UnknownMessageType(t) => write!(f, "未知消息类型: {t}"),
            Self::InvalidFieldType(k) => write!(f, "字段类型错误: {k}"),
        }
    }
}

impl std::error::Error for ProtocolParseError {}

/// 统一信封（03 消息封装）。字段名与 03 逐字对齐，禁止同义字段；
/// `type` 在 Rust 侧命名为 `msg_type`，序列化时 rename 回 `"type"`。
#[derive(Debug, Clone, Serialize)]
pub struct ProtocolEnvelope {
    pub protocol_version: u32,
    pub session_id: String,
    #[serde(rename = "type")]
    pub msg_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// request/host_request 的命令名（03 信封顶层字段；TASK-005 扩展提取）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    /// request/host_request 的参数对象（03 信封顶层字段；TASK-005 扩展提取）。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub success: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<Value>,
}

/// session_id 合法性校验（与 Python 端 main.py 同一规则）。
pub fn is_valid_session_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= SESSION_ID_MAX_LENGTH
        && s.starts_with(SESSION_ID_PREFIX)
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-')
}

/// 从流式缓冲中提取全部完整行（LF 分帧），残余不完整行保留在 buffer。
/// CR（CRLF 场景）由调用方按行 trim；此处只负责 LF 切分。
/// 骨架期 drain 线程使用 BufReader::lines；本函数为 TASK-005 完整协议
/// 流式分帧预留并经单测覆盖，故豁免 unused 告警。
#[cfg_attr(not(test), allow(dead_code))]
pub fn extract_complete_lines(buffer: &mut String) -> Vec<String> {
    let mut lines = Vec::new();
    while let Some(pos) = buffer.find('\n') {
        // drain 到 '\n'（含），行内容去掉 LF；\r 交由行级 trim 处理
        let line: String = buffer.drain(..=pos).collect();
        lines.push(line.trim_end_matches(['\n', '\r']).to_string());
    }
    lines
}

/// 解析一行 NDJSON 为信封。畸形行返回 Err（Supervisor 计数 + stderr 日志，
/// 不关会话——连续畸形关会话属 TASK-005 完整协议）。
pub fn parse_line(line: &str) -> Result<ProtocolEnvelope, ProtocolParseError> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Err(ProtocolParseError::EmptyLine);
    }
    let value = serde_json::from_str::<crate::ipc::strict_json::StrictValue>(trimmed)
        .map_err(|e| ProtocolParseError::MalformedJson(e.to_string()))?.0;

    let protocol_version = value
        .get("protocol_version")
        .ok_or(ProtocolParseError::MissingField("protocol_version"))?
        .as_i64()
        .ok_or(ProtocolParseError::InvalidFieldType("protocol_version"))?;
    if protocol_version != SUPPORTED_PROTOCOL_VERSION as i64 {
        return Err(ProtocolParseError::UnsupportedProtocolVersion(protocol_version));
    }

    let msg_type = value
        .get("type")
        .ok_or(ProtocolParseError::MissingField("type"))?
        .as_str()
        .ok_or(ProtocolParseError::InvalidFieldType("type"))?
        .to_string();
    match msg_type.as_str() {
        "request" | "response" | "event" | "host_request" | "host_response" => {}
        other => return Err(ProtocolParseError::UnknownMessageType(other.to_string())),
    }

    // 帧必填集（用户契约③/设计 §5.4 逐字；缺失=畸形帧，计数；连续达阈值
    // → 关闸门关会话，§6.5）。
    let id = match value.get("id") {
        Some(v) => Some(
            v.as_str()
                .ok_or(ProtocolParseError::InvalidFieldType("id"))?
                .to_string(),
        ),
        None => None,
    };
    let seq = value.get("seq").and_then(Value::as_u64);
    let command = value
        .get("command")
        .and_then(Value::as_str)
        .map(str::to_string);
    let params = value.get("params").cloned();
    match msg_type.as_str() {
        "request" => {
            if id.is_none() {
                return Err(ProtocolParseError::MissingField("id"));
            }
            if !value.get("command").map(Value::is_string).unwrap_or(false) {
                return Err(ProtocolParseError::MissingField("command"));
            }
        }
        "response" | "host_response" => {
            if id.is_none() {
                return Err(ProtocolParseError::MissingField("id"));
            }
            if value.get("success").and_then(Value::as_bool).is_none() {
                return Err(ProtocolParseError::MissingField("success"));
            }
            // data/error 互斥必有一（§5.4）
            enforce_data_error_exclusive(&value)?;
        }
        "event" => {
            if seq.is_none() {
                return Err(ProtocolParseError::MissingField("seq"));
            }
            if !value.get("name").map(Value::is_string).unwrap_or(false) {
                return Err(ProtocolParseError::MissingField("name"));
            }
            // 03 事件示例均含 timestamp 且 02 规定 UTC RFC3339（§9-3 冻结为必填）
            if !value.get("timestamp").map(Value::is_string).unwrap_or(false) {
                return Err(ProtocolParseError::MissingField("timestamp"));
            }
        }
        "host_request" => {
            if id.is_none() {
                return Err(ProtocolParseError::MissingField("id"));
            }
            if !value.get("command").map(Value::is_string).unwrap_or(false) {
                return Err(ProtocolParseError::MissingField("command"));
            }
            if !value.get("params").map(Value::is_object).unwrap_or(false) {
                return Err(ProtocolParseError::MissingField("params"));
            }
        }
        _ => {}
    }

    // session_id 校验后置（对齐 Python parse_frame 检查顺序：session_id 不在
    // 解析必填集，缺失/非法由调用方按用户契约③丢弃不计数——schema 用例
    // drop-missing-session-id 双端 counted=false；放在类型必填集之后判定，
    // 保证"缺失 session_id 且缺失其他必填字段"时的错误码与 Python 一致）。
    let session_id = value
        .get("session_id")
        .ok_or(ProtocolParseError::MissingField("session_id"))?
        .as_str()
        .ok_or(ProtocolParseError::InvalidFieldType("session_id"))?
        .to_string();
    if !is_valid_session_id(&session_id) {
        return Err(ProtocolParseError::InvalidSessionId(session_id));
    }

    Ok(ProtocolEnvelope {
        protocol_version: protocol_version as u32,
        session_id,
        msg_type,
        id,
        command,
        params,
        seq,
        name: value
            .get("name")
            .and_then(Value::as_str)
            .map(str::to_string),
        timestamp: value
            .get("timestamp")
            .and_then(Value::as_str)
            .map(str::to_string),
        payload: value.get("payload").cloned(),
        success: value.get("success").and_then(Value::as_bool),
        data: value.get("data").cloned(),
        error: value.get("error").cloned(),
    })
}

/// data/error 互斥必有一（§5.4：response/host_response）。两者皆缺或皆备
/// 均为畸形帧。
fn enforce_data_error_exclusive(value: &Value) -> Result<(), ProtocolParseError> {
    let has_data = value.get("data").is_some();
    let has_error = value.get("error").is_some();
    if has_data == has_error {
        return Err(ProtocolParseError::MissingField(if has_data {
            "data/error 互斥（两者不得同时出现）"
        } else {
            "data/error 必有一"
        }));
    }
    if value.get("success").and_then(Value::as_bool) != Some(has_data) {
        return Err(ProtocolParseError::MissingField("success 与 data/error 不符"));
    }
    Ok(())
}

/// hello 响应数据 → VersionsView（03：hello 返回 protocol_version/engine_version/
/// lua_version/schema_version/capabilities/model_manifests；字段缺失即非法）。
pub fn parse_versions(data: &Value) -> Option<VersionsView> {
    Some(VersionsView {
        protocol_version: u32::try_from(data.get("protocol_version")?.as_u64()?).ok()?,
        engine_version: data.get("engine_version")?.as_str()?.to_string(),
        lua_version: data
            .get("lua_version")
            .and_then(Value::as_str)
            .map(str::to_string),
        schema_version: u32::try_from(data.get("schema_version")?.as_u64()?).ok()?,
    })
}

/// hello / system_ready 数据 → CapabilityView 列表（最小子集 {id,state,reason}，
/// 对齐 capability-manifest.template.json；非法元素跳过）。
pub fn parse_capabilities(data: &Value) -> Vec<CapabilityView> {
    data.get("capabilities")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|c| {
                    Some(CapabilityView {
                        id: c.get("id")?.as_str()?.to_string(),
                        state: c.get("state")?.as_str()?.to_string(),
                        reason: c
                            .get("reason")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// 单元测试（设计 §6.3：行拆分与畸形处理；Job Object 真实 OS 行为不在单测覆盖）
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn extract_lines_splits_and_keeps_partial_tail() {
        let mut buf = String::from("{\"a\":1}\n{\"b\":2}\n{\"part");
        let lines = extract_complete_lines(&mut buf);
        assert_eq!(lines, vec!["{\"a\":1}".to_string(), "{\"b\":2}".to_string()]);
        assert_eq!(buf, "{\"part");
    }

    #[test]
    fn extract_lines_handles_crlf_and_empty_lines() {
        let mut buf = String::from("x\r\n\r\ny\n");
        let lines = extract_complete_lines(&mut buf);
        assert_eq!(lines, vec!["x".to_string(), String::new(), "y".to_string()]);
        assert!(buf.is_empty());
    }

    #[test]
    fn parse_valid_event_envelope() {
        let line = r#"{"protocol_version":1,"session_id":"session-abc","type":"event","seq":1,"name":"system_ready","timestamp":"2026-10-09T12:00:00.000Z","payload":{"k":1}}"#;
        let env = parse_line(line).expect("合法 event 应解析成功");
        assert_eq!(env.protocol_version, 1);
        assert_eq!(env.msg_type, "event");
        assert_eq!(env.name.as_deref(), Some("system_ready"));
        assert_eq!(env.seq, Some(1));
        assert!(env.id.is_none());
    }

    #[test]
    fn parse_valid_response_envelope() {
        let line = r#"{"protocol_version":1,"session_id":"session-abc","type":"response","id":"req-1","success":true,"data":{}}"#;
        let env = parse_line(line).expect("合法 response 应解析成功");
        assert_eq!(env.msg_type, "response");
        assert_eq!(env.id.as_deref(), Some("req-1"));
        assert_eq!(env.success, Some(true));
    }

    #[test]
    fn malformed_lines_are_rejected() {
        // 非 JSON
        assert!(matches!(
            parse_line("this is not json at all"),
            Err(ProtocolParseError::MalformedJson(_))
        ));
        // 截断 JSON（stdout_garbage 样本）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"#),
            Err(ProtocolParseError::MalformedJson(_))
        ));
        // 缺 session_id（B5：session_id 校验后置于类型必填集之后，对齐 Python
        // parse_frame 检查顺序；其余必填齐全时才判 session_id 缺失）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"type":"event","seq":1,"name":"x","timestamp":"t"}"#),
            Err(ProtocolParseError::MissingField("session_id"))
        ));
        // 缺 session_id 且缺类型必填 timestamp：先报类型必填（与 Python 一致；
        // seq 在 timestamp 之前校验，故补 seq 使 timestamp 为首个缺失字段）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"type":"event","seq":1,"name":"x"}"#),
            Err(ProtocolParseError::MissingField("timestamp"))
        ));
        // 协议版本不符
        assert!(matches!(
            parse_line(r#"{"protocol_version":2,"session_id":"session-a","type":"event","name":"x"}"#),
            Err(ProtocolParseError::UnsupportedProtocolVersion(2))
        ));
        // response 缺 id（03：request/response id 必填）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"response","success":true}"#),
            Err(ProtocolParseError::MissingField("id"))
        ));
        // response 缺 success（§5.4 必填集）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"response","id":"r1","data":{}}"#),
            Err(ProtocolParseError::MissingField("success"))
        ));
        // response data/error 皆缺（§5.4 互斥必有一）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"response","id":"r1","success":true}"#),
            Err(ProtocolParseError::MissingField("data/error 必有一"))
        ));
        // response data/error 皆备（互斥违反）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"response","id":"r1","success":true,"data":{},"error":{}}"#),
            Err(ProtocolParseError::MissingField("data/error 互斥（两者不得同时出现）"))
        ));
        // event 缺 name（必填集校验次序：seq → name → timestamp）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"event","seq":1,"timestamp":"t"}"#),
            Err(ProtocolParseError::MissingField("name"))
        ));
        // event 缺 timestamp（§9-3 冻结必填）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"event","seq":1,"name":"x"}"#),
            Err(ProtocolParseError::MissingField("timestamp"))
        ));
        // request 缺 command（§5.4）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"request","id":"q1"}"#),
            Err(ProtocolParseError::MissingField("command"))
        ));
        // 未知消息类型
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-a","type":"weird"}"#),
            Err(ProtocolParseError::UnknownMessageType(t)) if t == "weird"
        ));
        // 空行
        assert!(matches!(parse_line("   "), Err(ProtocolParseError::EmptyLine)));
    }

    #[test]
    fn host_request_and_host_response_frames_parse() {
        // host_request 必填集：id/command/params（§5.4）
        let line = r#"{"protocol_version":1,"session_id":"session-abc","type":"host_request","id":"py-1","command":"input_action","params":{"kind":"click"}}"#;
        let env = parse_line(line).expect("合法 host_request 应解析成功");
        assert_eq!(env.msg_type, "host_request");
        assert_eq!(env.id.as_deref(), Some("py-1"));
        assert_eq!(env.payload, None);
        // params 必须是 object
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-abc","type":"host_request","id":"py-1","command":"input_action","params":"oops"}"#),
            Err(ProtocolParseError::MissingField("params"))
        ));
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-abc","type":"host_request","id":"py-1","command":"input_action"}"#),
            Err(ProtocolParseError::MissingField("params"))
        ));
        // host_response 必填集：id/success + data/error 互斥必有一
        let line = r#"{"protocol_version":1,"session_id":"session-abc","type":"host_response","id":"py-1","success":false,"error":{"code":"CAPABILITY_DENIED","message":"m","details":{},"retryable":false}}"#;
        let env = parse_line(line).expect("合法 host_response 应解析成功");
        assert_eq!(env.msg_type, "host_response");
        assert_eq!(env.success, Some(false));
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"session-abc","type":"host_response","id":"py-1"}"#),
            Err(ProtocolParseError::MissingField("success"))
        ));
        // 白名单逐字（03 内部宿主命令目录）
        for cmd in HOST_REQUEST_COMMANDS {
            assert!(is_host_request_command(cmd));
        }
        assert!(!is_host_request_command("execute_script"));
        assert!(!is_host_request_command("make_coffee"));
    }

    #[test]
    fn cross_session_and_bad_session_ids_rejected() {
        // session_id 前缀/字符集/长度校验（03：旧会话消息全部拒绝——由
        // Supervisor 相等性判断执行，此处校验格式层）
        assert!(is_valid_session_id("session-1"));
        assert!(is_valid_session_id("session-550e8400-e29b-41d4-a716-446655440000"));
        assert!(!is_valid_session_id(""));
        assert!(!is_valid_session_id("550e8400-e29b-41d4-a716-446655440000")); // 缺前缀
        assert!(!is_valid_session_id("session-路径")); // 非法字符
        assert!(!is_valid_session_id("session-a b")); // 空格
        assert!(!is_valid_session_id(&format!("session-{}", "a".repeat(200)))); // 超长
        // 解析层同样拒绝非法 session_id 的行（其余必填齐全时才轮到格式校验）
        assert!(matches!(
            parse_line(r#"{"protocol_version":1,"session_id":"bogus","type":"event","seq":1,"name":"x","timestamp":"t"}"#),
            Err(ProtocolParseError::InvalidSessionId(_))
        ));
    }

    #[test]
    fn response_success_matches_payload_in_both_directions() {
        for kind in ["response", "host_response"] {
            for (success, field) in [(true, "error"), (false, "data")] {
                let mut value = serde_json::json!({"protocol_version":1,"session_id":"s","type":kind,"id":"py-1","success":success});
                value[field] = serde_json::json!({});
                assert!(parse_line(&value.to_string()).is_err());
            }
        }
    }

    #[test]
    fn hello_and_system_ready_data_extraction() {
        let caps = json!([
            {"id": "ocr", "state": "unavailable", "reason": "骨架未接入识别，模型未由引擎加载"},
            {"id": "lua_script", "state": "unavailable", "reason": "Lua 运行时未由骨架接入"}
        ]);
        let versions_json = json!({
            "protocol_version": 1,
            "engine_version": "0.1.0-skeleton",
            "lua_version": "Lua 5.4",
            "schema_version": 2
        });
        let versions = parse_versions(&versions_json).expect("versions 解析成功");
        assert_eq!(versions.engine_version, "0.1.0-skeleton");
        assert_eq!(versions.lua_version.as_deref(), Some("Lua 5.4"));
        assert_eq!(versions.schema_version, 2);
        let parsed_caps = parse_capabilities(&json!({ "capabilities": caps }));
        assert_eq!(parsed_caps.len(), 2);
        assert_eq!(parsed_caps[0].id, "ocr");
        assert_eq!(parsed_caps[0].state, "unavailable");
        // lua_version 缺失 → None 兜底（§9-1：运行时探测失败 → null 语义）
        let mut bare = versions_json.clone();
        bare.as_object_mut().unwrap().remove("lua_version");
        assert_eq!(parse_versions(&bare).unwrap().lua_version, None);
        // 空能力数组
        assert!(parse_capabilities(&json!([])).is_empty());
    }
}
