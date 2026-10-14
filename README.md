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

## 准备：UDE 工程（一次性，必须先做）

**本工具不创建 UDE 工程**——它打开并操控一个已经配置好的 `.wsx` 工程。
第一次使用前，请在 UDE Studio 里手动建好工程：

1. UDE Studio → File → New Workspace，选择目标芯片/封装
2. 按向导选好目标配置（Target Configuration）、调试核（Debug Core）
3. 若目标板需要调试密码，填好并保存（如 TC397 的密码文件）
4. 保存工程，得到一个能一键连上目标板的 `.wsx`

> **为什么需要这步**：目标配置、调试核、连接密码这些选择对话框是 UDE 的
> 工程创建向导的一部分，MCP 没有替代它的 UI。配置好的工程之后可以一直复用；
> `ude_start` 也支持传 `cfg` 参数用现成 `.cfg` 创建新 workspace，但从零
> 建工程请在 UDE Studio 里做一次。

验证工程可用：UDE Studio 里打开它，确认能 Halt/Run 目标。

### 工程怎么告诉 agent

两种方式，按需选一（或混用，工具参数优先）：

**方式一：配置为本地默认工程**（固定一块板的场景，推荐）

在 `windows\config.json` 里加 `default_wsx`：

```json
{
  "default_wsx": "C:\\Users\\Public\\Documents\\pls\\UDE 4.10\\Workspaces\\tc397.wsx"
}
```

之后 agent 直接说"连目标板"即可，`ude_start` 不带参数就会打开它，
无需每次报路径。

**方式二：对话里告诉 agent**

> 「用 `D:\work\myboard.wsx` 连板子」

agent 会把路径传给 `ude_start` 的 `wsx` 参数（WSL 路径如 `/mnt/c/...` 会
自动转换）。适合多块板 / 多工程切换。

优先级：`ude_start(wsx=...)` 参数 > `config.json` 的 `default_wsx`。

## 系统要求

| | |
|---|---|
| 目标调试器 | PLS UDE Studio 4.x（UDE 5 未测） |
| 操作系统 | Windows 10/11（与 UDE 同机）；WSL 拓扑另需 WSL2 |
| Python | 3.10+，依赖 `mcp`、`pywin32` |
| 目标板 | 任意 UDE 支持的芯片（多核 TriCore 有额外适配） |

## 部署 A：纯 Windows（推荐）

agent、MCP、UDE 都在 Windows 上，5 分钟装完。

**1. 安装依赖**

```bat
git clone https://github.com/your-org/autoude-mcp
cd autoude-mcp
pip install -r requirements.txt
```

**2. 配置 UDE 路径和默认工程**

编辑 `windows\config.json`：

```json
{
  "backend": "real",
  "default_wsx": "C:\\Users\\Public\\Documents\\pls\\UDE 4.10\\Workspaces\\tc397-5.wsx",
  "ude_exe": "D:\\Program Files (x86)\\pls\\UDE 4.10\\UDEVisualPlatform.exe"
}
```

`ude_exe` 装在常见默认位置可留空（自动探测）；`default_wsx` 见上文
「工程怎么告诉 agent」，不配也行（每次由 agent 传工程路径）。

**3. 注册到 agent**

本质就是一条启动命令：`python <仓库路径>\ude_mcp.py`。

Claude Code：

```bat
claude mcp add ude -- python C:\path\to\autoude-mcp\ude_mcp.py
```

其他客户端（通用 MCP 配置）：

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

`command` 请指向装了依赖的 Python（虚拟环境亦可）。

**4. 用起来**

对 agent 说"连目标板"即可：

```text
ude_start()                          ← 打开 config.json 里的 default_wsx
ude_start(wsx="D:\work\other.wsx")   ← 或临时指定另一个工程
```

服务端随 agent 会话自动拉起，**不需要手动启动任何进程**。

## 部署 B：WSL + Windows

agent 在 WSL、UDE 在 Windows。两端都要放一份本仓库（协议文件需同版本）。

**Windows 端（1–2 步同部署 A，之后改为常驻服务）**

```bat
pip install pywin32          :: 或在仓库根目录 pip install -r requirements.txt
:: 配置 windows\config.json 同上

cd windows
python ude_bridge_server.py  :: 常驻，关掉窗口服务就停
```

**WSL 端**

```bash
git clone https://github.com/your-org/autoude-mcp
cd autoude-mcp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 还需要 npiperelay（跨 WSL 访问 Windows 命名管道）
# https://github.com/jstarks/npiperelay ，默认找 /mnt/d/npiperelay/npiperelay.exe
```

**注册（WSL 内的 agent）**

```bash
claude mcp add ude -- /path/to/autoude-mcp/.venv/bin/python \
    /path/to/autoude-mcp/ude_mcp.py
```

WSL 默认走 pipe 模式连 Windows 常驻服务；工具参数里的 WSL 路径（`/mnt/c/...`）
会自动转成 Windows 路径。

## 给 agent 的典型指令

配好 `default_wsx` 后：

> 「连上目标板，加载 `project_A.elf`，在 `Com_MainFunctionTx` 打断点，
> 跑起来，命中后把调用栈和 `g_CoreCounter` 给我看」

未配默认工程时，加上工程路径：

> 「用 `D:\work\tc397-5.wsx` 连上目标板，加载 `project_A.elf`，……」

典型流程：`ude_start`（或对已打开的 UDE 用 `ude_attach`）→ `ude_load_program`
→ `ude_breakpoint_add` → `ude_go` → `ude_wait_halt` → `ude_read_variable` /
`ude_get_callstack`。

## 进阶

<details>
<summary>环境变量</summary>

| 变量 | 默认 | 说明 |
|---|---|---|
| `UDE_BRIDGE_MODE` | Windows `stdio` / WSL `pipe` | `stdio` 本地子进程；`pipe` 命名管道（多客户端共享服务端） |
| `UDE_BACKEND` | 由 `windows/config.json` 决定 | 覆盖后端 `mock` / `real` |
| `UDE_BRIDGE_PIPE` | `\\.\pipe\ude_bridge` | 管道名 |
| `UDE_BRIDGE_NPIPERELAY` | `/mnt/d/npiperelay/npiperelay.exe` | 仅 WSL pipe 模式 |

</details>

<details>
<summary>多 agent 共享一个 UDE 会话</summary>

默认 stdio 模式下每个 agent 会话带一个独立服务进程。想共享：

```bat
cd windows
python ude_bridge_server.py
```

所有 agent 设 `UDE_BRIDGE_MODE=pipe` 即可连同一个常驻服务（UDE 会话跨
agent / 重启共享）。

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
