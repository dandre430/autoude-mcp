# autoude-mcp

**让 AI agent 直接调试嵌入式目标的 MCP 服务器** —— 驱动 Windows 上的 PLS UDE
Studio（COM 自动化），把固件加载、断点、运行控制、变量/内存/寄存器读写变成
模型可调用的工具。

任何支持 [MCP](https://modelcontextprotocol.io) 的 agent 都能用：Claude Code、
Claude Desktop、Cursor、Windsurf、Cline、Zed ……

## 功能

| 类别 | 工具 |
|---|---|
| 会话 | `ude_start` · `ude_attach` · `ude_status` · `ude_close` · `ude_load_program` · `ude_get_callstack` · `ude_get_message_log` |
| 运行 | `ude_go` · `ude_break` · `ude_step_in` · `ude_step_over` · `ude_step_out` · `ude_reset` · `ude_wait_halt` · `ude_target_state` |
| 数据 | `ude_read_variable` · `ude_write_variable` · `ude_read_memory` · `ude_write_memory` · `ude_read_register` · `ude_write_register` |
| 断点 | `ude_breakpoint_add` · `ude_breakpoint_remove` · `ude_breakpoint_clear_all` · `ude_breakpoint_list` |

- 加载 / 烧写 ELF，断点命中后读调用栈、变量、寄存器、内存
- 多核目标（如 TC397）自动处理 co-stop 运行控制
- 无硬件时可切 mock 后端开发调试

## 快速开始

**前置**：Windows 上装好 PLS UDE Studio（4.x）；Python 3.10+。

### 1. 安装

```bat
git clone https://github.com/your-org/autoude-mcp
cd autoude-mcp
pip install -r requirements.txt
```

### 2. 配置

编辑 `windows/config.json`，填 UDE 安装路径：

```json
{
  "backend": "real",
  "ude_exe": "D:\\Program Files (x86)\\pls\\UDE 4.10\\UDEVisualPlatform.exe"
}
```

装在常见默认位置的话 `ude_exe` 可以留空（自动探测）。

### 3. 注册到你的 agent

本质就是一条启动命令：`python <仓库路径>\ude_mcp.py`。

**Claude Code**

```bat
claude mcp add ude -- python C:\path\to\autoude-mcp\ude_mcp.py
```

**其他客户端**（通用 MCP 配置）

```json
{
  "mcpServers": {
    "ude": {
      "command": "python",
      "args": ["C:\\path\\to\\autoude-mcp\\ude_mcp.py"]
    }
  }
}
```

| Agent | 配置文件位置 |
|---|---|
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Cline / Roo Code | VS Code 扩展 MCP 设置 |
| Zed | `settings.json` → `context_servers` |

> `command` 请指向装了依赖的 Python（虚拟环境亦可）。

### 4. 用起来

注册后直接对 agent 说话：

> 「在 `Com_MainFunctionTx` 打个断点，跑起来，命中后把调用栈和 `g_CoreCounter` 给我看」

典型流程：`ude_start`（或 `ude_attach`）→ `ude_load_program` →
`ude_breakpoint_add` → `ude_go` → `ude_wait_halt` → `ude_read_variable` /
`ude_get_callstack`。

## 进阶

<details>
<summary>常驻服务模式（多个 agent 共享一个 UDE 会话）</summary>

默认下每个 agent 会话自带一个本地服务进程。想让 UDE 会话跨 agent / 重启共享：

```bat
cd windows
python ude_bridge_server.py
```

然后给 MCP 加环境变量 `UDE_BRIDGE_MODE=pipe`。

</details>

<details>
<summary>WSL + Windows 拓扑</summary>

agent 跑在 WSL、UDE 装在 Windows 时：Windows 侧按上面起 `ude_bridge_server.py`
常驻服务，WSL 侧安装后注册：

```bash
pip install -r requirements.txt
claude mcp add ude -- .venv/bin/python /path/to/autoude-mcp/ude_mcp.py
```

WSL 需要 [npiperelay](https://github.com/jstarks/npiperelay)（默认路径
`/mnt/d/npiperelay/npiperelay.exe`，可用 `UDE_BRIDGE_NPIPERELAY` 改）。
工具参数里的 WSL 路径（`/mnt/c/...`）会自动转成 Windows 路径。

</details>

<details>
<summary>无硬件自测（mock 后端）</summary>

```bash
UDE_BRIDGE_MODE=stdio UDE_BACKEND=mock python -c "
from bridge_client import BridgeClient
c = BridgeClient()
print(c.ping(), c.request('read_variable', {'expr': 'g_counter'}))"
```

</details>

<details>
<summary>环境变量</summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `UDE_BRIDGE_MODE` | Windows `stdio` / WSL `pipe` | `stdio` 本地子进程；`pipe` 命名管道 |
| `UDE_BACKEND` | 由 `windows/config.json` 决定 | 覆盖后端 `mock` / `real` |
| `UDE_BRIDGE_PIPE` | `\\.\pipe\ude_bridge` | 管道名 |
| `UDE_BRIDGE_NPIPERELAY` | `/mnt/d/npiperelay/npiperelay.exe` | 仅 WSL pipe 模式 |

</details>

## 系统要求

| | |
|---|---|
| 目标调试器 | PLS UDE Studio 4.x（UDE 5 未测） |
| 操作系统 | Windows 10/11（与 UDE 同机）；WSL 拓扑另需 WSL2 |
| Python | 3.10+，依赖 `mcp`、`pywin32` |
| 目标板 | 任意 UDE 支持的芯片（多核 TriCore 有额外适配） |
