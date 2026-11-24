# -*- coding: utf-8 -*-
"""backend.ipc —— Python 侧 IPC 协议层（TASK-005 批 B1）。

模块组成（设计 §3.2）：
- envelope:   信封编解码（自 main.py 平移，行为不变）+ §5.4 多类型必填集 + host_request 构造；
- dispatcher: 命令路由（框架命令表 + 业务空注册表）与 stdin 读循环托管、畸形计数对称语义；
- jobs:       JobManager（job-<uuid>、pending/running/completed/cancelled/error、NOT_FOUND）；
- hostclient: HostClient（py-<uuid> host_request、条件变量等待 host_response，不占 reader）。

纯标准库，零第三方依赖（设计 §10）。
"""
