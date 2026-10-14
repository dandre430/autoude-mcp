# UDE Studio 控制 MCP（autoude-mcp）

给支持 MCP 的 AI agent 用的调试服务器：驱动 Windows 上的 PLS UDE Studio
（COM 自动化接口），实现固件加载、运行控制、变量/内存/寄存器读写、断点管理
等调试操作。标准 MCP stdio 服务器，Claude Code / Cursor / Windsurf / Cline /
Claude Desktop 等任意 MCP 客户端都能接。

支持两种宿主环境：

- **WSL + Windows**（原始拓扑）：MCP 跑在 WSL，经 npiperelay 跨过命名管道
  调 Windows 侧服务端。
- **纯 Windows**（推荐）：MCP 直接跑在 Windows，本地 stdio 子进程起服务端，
  不需要 WSL/npiperelay。

## 架构

```
WSL 拓扑：
MCP 客户端 ──stdio──> ude_mcp.py (WSL)
                        │ bridge_client.py（持久子进程）
                        ▼
             npiperelay.exe ⇄ \\.\pipe\ude_bridge
                        ▼
             ude_bridge_server.py (Windows, pywin32)
                        ▼ win32com
             UDE COM (UDELauncher / UDERunningInstanceManager) ──> UDE Studio

纯 Windows 拓扑（默认 stdio）：
MCP 客户端 ──stdio──> ude_mcp.py (Windows)
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
   pip install -r requirements.txt
   ```

   （即 `mcp` + `pywin32`，后者由 `sys_platform == 'win32'` 标记控制。）

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

4. 按下文「注册到 MCP 客户端」把 `ude_mcp.py` 挂进你用的 agent。
   默认 `UDE_BRIDGE_MODE=stdio`：ude_mcp.py 会自动拉起
   `windows\ude_bridge_server.py --stdio` 子进程，**不需要手动启动服务端，
   也不需要 npiperelay**。

5. （可选）长驻管道模式：想让服务端常驻（多个 agent 共享、重启客户端不断 UDE
   会话），先手动起服务端：

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

## 注册到 MCP 客户端（通用）

MCP 服务器就是一个 stdio 子进程，注册的本质是给客户端一段启动命令：

```
command = <装了依赖的 Python> <路径>/ude_mcp.py
```

通用配置格式（绝大多数客户端认这个结构）：

```json
{
  "mcpServers": {
    "ude": {
      "command": "C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
      "args": ["D:\\ude-agent\\ude_mcp.py"],
      "env": { "UDE_BRIDGE_MODE": "pipe" }
    }
  }
}
```

`env` 可选，不填就用下文「环境变量」的默认值。Linux/WSL 侧把 command 换成
`.venv/bin/python` 即可。

各家客户端只是配置文件位置不同：

| 客户端 | 配置位置 | 备注 |
|---|---|---|
| Claude Code | `claude mcp add ude -- <python> <...>/ude_mcp.py` | 也可直接写项目 `.mcp.json` |
| Claude Desktop | `claude_desktop_config.json` | Windows 在 `%APPDATA%\Claude\` |
| Cursor | `.cursor/mcp.json`（项目）或全局 `mcp.json` | |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | |
| Cline / Roo Code | VS Code 扩展的 MCP Servers 设置 | |
| Zed | `~/.config/zed/settings.json` 的 `context_servers` | |
| 其他 | 任何支持 MCP stdio 的客户端 | 用上面的通用 JSON |

Claude Code 的具体命令：

```bash
# 纯 Windows（cmd / PowerShell）
claude mcp add ude -- C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\python.exe D:\ude-agent\ude_mcp.py

# WSL
claude mcp add ude -- /home/zhuangzhuangduan/projects/autoude/.venv/bin/python \
    /home/zhuangzhuangduan/projects/autoude/ude_mcp.py
```

注意：

- **command 必须指向装了 `mcp`（以及 Windows 上 `pywin32`）的 Python**，
  系统裸 python 常常没装。
- 跑在 Windows 的客户端配纯 Windows 形态（`D:\ude-agent\ude_mcp.py`）；
  跑在 WSL/Linux 的客户端才用 WSL 形态。
- 多个客户端可同时注册同一个 server 文件。会话起 → `ude_mcp.py` 子进程起；
  会话结束 → 进程退出。stdio 模式各自拉各自的 bridge server；
  `UDE_BRIDGE_MODE=pipe` 时可共享同一个常驻 server。

### 环境变量

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
