# UDE Studio 控制 MCP

给 Claude Code 用的 MCP 服务器：驱动 Windows 上的 PLS UDE Studio（COM 自动化
接口），实现固件加载、运行控制、变量/内存/寄存器读写、断点管理等调试操作。

支持两种宿主环境：

- **WSL + Windows**（原始拓扑）：MCP 跑在 WSL，经 npiperelay 跨过命名管道
  调 Windows 侧服务端。
- **纯 Windows**（推荐）：MCP 直接跑在 Windows，本地 stdio 子进程起服务端，
  不需要 WSL/npiperelay。

## 架构

```
WSL 拓扑：
Claude Code ──stdio──> ude_mcp.py (WSL)
                          │ bridge_client.py（持久子进程）
                          ▼
               npiperelay.exe ⇄ \\.\pipe\ude_bridge
                          ▼
               ude_bridge_server.py (Windows, pywin32)
                          ▼ win32com
               UDE COM (UDELauncher / UDERunningInstanceManager) ──> UDE Studio

纯 Windows 拓扑（默认 stdio）：
Claude Code ──stdio──> ude_mcp.py (Windows)
                          │ bridge_client.py（本地子进程）
                          ▼
               ude_bridge_server.py --stdio (同机, pywin32)
                          ▼ win32com
               UDE COM ──> UDE Studio (UDEVisualPlatform.exe)
```

## 目录

| 文件 | 说明 |
|---|---|
| `protocol.py` | 线协议：每行一个 JSON 请求/响应 |
| `bridge_client.py` | 持久连接客户端（stdio 子进程 / 命名管道直连 / npiperelay） |
| `ude_mcp.py` | MCP 服务器，暴露全部 ude_* 工具 |
| `windows/ude_bridge_server.py` | 管道/stdio 服务端 |
| `windows/ude_backend.py` | `RealUdeBackend`（win32com 调 UDE）+ `MockUdeBackend`（假目标） |
| `windows/config.json` | 服务端配置（backend / pipe_name / ude_exe） |

## 安装：纯 Windows（推荐）

1. 安装 Python 3.10+（带 pip），装依赖：

   ```bat
   pip install mcp pywin32
   ```

2. 把本项目整个目录拷到 Windows 本地，例如 `D:\ude-agent\`
   （`protocol.py` 与 `windows\` 必须保持同级目录）。

3. 配置 `windows\config.json`：

   ```json
   {
     "backend": "real",
     "pipe_name": "\\\\.\\pipe\\ude_bridge",
     "ude_exe": "D:\\Program Files (x86)\\pls\\UDE 4.10\\UDEVisualPlatform.exe",
     "dialog_sentinel": true,
     "dialog_action": "auto_fill",
     "source_roots": ["D:\\WinGitWorkspace\\APA_TC397_APP"]
   }
   ```

   `backend: "real"` 对接真实 UDE，`"mock"` 为无硬件自测；
   `ude_exe` 留空则自动探测常见 UDE 安装位置。

4. 注册到 Claude Code（cmd）：

   ```bat
   claude mcp add ude -- C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\python.exe D:\ude-agent\ude_mcp.py
   ```

   或 PowerShell 下用完整 python.exe 路径。默认 `UDE_BRIDGE_MODE=stdio`：
   ude_mcp.py 会自动拉起 `windows\ude_bridge_server.py --stdio` 子进程，
   **不需要手动启动服务端，也不需要 npiperelay**。

5. （可选）长驻管道模式：想让服务端常驻（多个 MCP 客户端共享、重启 Claude 不
   断 UDE 会话），先手动起服务端：

   ```bat
   cd /d D:\ude-agent\windows
   python ude_bridge_server.py
   ```

   再给 MCP 设 `UDE_BRIDGE_MODE=pipe`——纯 Windows 下 bridge_client 用
   win32file 直连 `\\.\pipe\ude_bridge`。

## 安装：WSL + Windows

### WSL 侧

```bash
cd /home/zhuangzhuangduan/projects/autoude
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Windows 侧

1. 安装 Python 3 并 `pip install pywin32`
2. 确认 npiperelay.exe 存在（默认 `/mnt/d/npiperelay/npiperelay.exe`，
   可用环境变量 `UDE_BRIDGE_NPIPERELAY` 改）
3. 启动 bridge server（真实 UDE 模式）：

```bat
cd C:\path\to\autoude\windows
python ude_bridge_server.py
```

## 注册到 Claude Code（WSL）

```bash
claude mcp add ude -- /home/zhuangzhuangduan/projects/autoude/.venv/bin/python \
    /home/zhuangzhuangduan/projects/autoude/ude_mcp.py
```

环境变量（可选，`claude mcp add ude -e KEY=VAL ...` 传入）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `UDE_BRIDGE_MODE` | Windows=`stdio`，WSL=`pipe` | `stdio`=本地子进程；`pipe`=命名管道（WSL 经 npiperelay，Windows 直连） |
| `UDE_BACKEND` | 不设（Windows 下由 `windows/config.json` 决定；WSL stdio 回退 `mock`） | 覆盖服务端后端 `mock`/`real` |
| `UDE_BRIDGE_PIPE` | `\\.\pipe\ude_bridge` | 管道名 |
| `UDE_BRIDGE_NPIPERELAY` | `/mnt/d/npiperelay/npiperelay.exe` | npiperelay 路径（仅 WSL pipe 模式） |

## 无硬件自测（mock 后端）

`UDE_BACKEND=mock` 强制走假目标（不碰 UDE/COM），WSL 和 Windows 都能跑：

```bash
UDE_BRIDGE_MODE=stdio UDE_BACKEND=mock .venv/bin/python - <<'EOF'
from bridge_client import BridgeClient
c = BridgeClient()
print(c.ping())
print(c.request("start_session", {"wsx": "a.wsx", "cfg": "a.cfg"}))
print(c.request("read_variable", {"expr": "g_counter"}))
EOF
```

## 工具一览（25 个）

- 会话：`ude_start` / `ude_attach` / `ude_status` / `ude_close` / `ude_load_program`
  / `ude_get_callstack` / `ude_get_message_log`
- 运行：`ude_go` / `ude_break` / `ude_step_in` / `ude_step_over` / `ude_step_out`
  / `ude_reset` / `ude_wait_halt` / `ude_target_state`
- 数据：`ude_read_variable` / `ude_write_variable` / `ude_read_memory` / `ude_write_memory`
  / `ude_read_register` / `ude_write_register`
- 断点：`ude_breakpoint_add` / `ude_breakpoint_remove` / `ude_breakpoint_clear_all`
  / `ude_breakpoint_list`

WSL 路径参数（如 `/mnt/c/...`）会自动转换为 Windows 路径。
典型流程：`ude_start`（或 `ude_attach`）→ `ude_load_program` → `ude_breakpoint_add`
→ `ude_go` → `ude_wait_halt` → `ude_read_variable` / `ude_get_callstack`。

## 线协议

每行一个 UTF-8 JSON，`\n` 定界：

```json
→ {"id": 1, "action": "read_variable", "params": {"expr": "g_counter"}}
← {"id": 1, "status": "ok", "result": {"expr": "g_counter", "value": 42, "type": "int"}}
← {"id": 1, "status": "error", "code": "not_connected", "message": "..."}
```

错误码：`no_session` / `not_connected` / `bad_request` / `unknown_action` /
`backend_error` / `timeout` / `comm_error`。
# autoude-mcp
