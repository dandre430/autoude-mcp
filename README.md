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

### 工程 / 程序怎么告诉 agent

两种方式，按需选一（或混用，工具参数优先）：

**方式一：配置为本地默认路径**（固定一块板、固定烧一个产物，推荐）

在 `windows\config.json` 里配好工程和程序文件：

```json
{
  "default_wsx": "C:\\Users\\Public\\Documents\\pls\\UDE 4.10\\Workspaces\\tc397.wsx",
  "default_elf": "D:\\work\\proj\\_bin\\swb\\project_A.elf"
}
```

之后 agent 直接说"连目标板、烧写"即可，`ude_start` / `ude_load_program`
不带参数就用它们，无需每次报路径。

**方式二：对话里告诉 agent**

> 「用 `D:\work\myboard.wsx` 连板子，烧 `D:\work\proj\_bin\swb\project_B.elf`」

agent 会把路径传给 `ude_start` 的 `wsx` 参数和 `ude_load_program` 的 `elf`
参数（WSL 路径如 `/mnt/c/...` 会自动转换）。适合多块板 / A/B 多镜像切换。

优先级：工具参数 > `config.json` 的 `default_wsx` / `default_elf`。
注意 `ude_start` 的 elf 参数只在显式传了才烧写——打开工程 ≠ 烧写；
烧写统一走 `ude_load_program`（可不带参数）。

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

**2. 配置 UDE 路径、默认工程和源码根**

编辑 `windows\config.json`：

```json
{
  "backend": "real",
  "default_wsx": "C:\\Users\\Public\\Documents\\pls\\UDE 4.10\\Workspaces\\tc397-5.wsx",
  "default_elf": "D:\\work\\proj\\_bin\\swb\\project_A.elf",
  "ude_exe": "D:\\Program Files (x86)\\pls\\UDE 4.10\\UDEVisualPlatform.exe",
  "source_roots": ["D:\\work\\proj"]
}
```

| 字段 | 必要性 | 说明 |
|---|---|---|
| `ude_exe` | 可选 | 装在常见默认位置可留空（自动探测） |
| `default_wsx` | 推荐 | 默认 UDE 工程，见「工程 / 程序怎么告诉 agent」；不配则每次由 agent 传 |
| `default_elf` | 推荐 | 默认程序文件，`ude_load_program()` 不带参数就烧它 |
| `source_roots` | **必配** | 工程源码根目录，弹框哨兵靠它自动填源文件定位框，见「弹窗怎么办」 |
| `flash_skip_modules` | 可选 | 默认 `["OTP","UCB","BMHD"]`，见「烧写：会写哪些地址」 |

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

## 烧写：会写哪些地址？

**烧哪些地址不由工具参数指定**，由文件和 UDE 工程的 Flash 配置共同决定：

| 谁决定 | 内容 |
|---|---|
| 文件本身（elf/hex/s19） | 有哪些数据段、各自的地址 |
| UDE 工程的 Flash 配置 | 有哪些 Flash 模块、地址窗多大、使能与否 |
| `skip_modules`（本工具） | 哪些模块**不**烧——默认跳过一次性区 |

`ude_load_program` 把文件里的数据段填进 Memtool，**每个段落在哪个 Flash 模块的
地址窗内，就由哪个模块烧**；文件里没有的地址不写，没使能的模块不写。

**一次性区保护（默认开）**：OTP、UCB、BMHD 这类一次性可编程区烧错无法挽回，
默认按模块名/描述子串跳过（`flash_skip_modules`，默认 `["OTP","UCB","BMHD"]`）。
确要烧时二选一：显式 `ude_load_program(program_all=True)`，或传
`skip_modules="UCB"` 自定义跳过列表。

烧前用 `ude_flash_info` 预览：每个模块的名称/地址窗/数据段数量（有数据 = 会
被烧）；烧写结果里的 `skipped_safety` 列出本次被跳过的模块和原因。

> `strategy=load_and_flash` / `multicore` 是黑盒一把梭，文件里有什么烧什么，
> **不做模块过滤**（结果里会注明）。要保护生效用默认的 `strategy=auto`（走
> Memtool 配方，逐模块 `ProgramSections`）。

## 弹窗（模态框）怎么办？

UDE 的模态框会把 COM 调用整条卡死，无人值守必须处理。两类：

**1. 源文件定位框**（调试时弹）

原因：ELF DWARF 里记录的编译路径在本机不存在，UDE 打开源码窗口时弹框找
`.c` 文件。解法：bridge server 自带**弹框哨兵**（`dialog_sentinel`），轮询
发现这类框后在 `source_roots` 里搜同名文件自动填入确认：

```json
"dialog_sentinel": true,
"dialog_action": "auto_fill",
"source_roots": ["D:\\WinGitWorkspace\\APA_TC397_APP"]
```

**`source_roots` 是开箱必配项**——填你的工程源码根目录（可多个，递归索引
`.c/.h/.cpp...`）。加载程序时 ELF 所在目录会自动登记为搜索根。找不到时哨兵
直接关框：不显示源码，但断点/变量/调用栈等调试不受影响。不想自动填可改
`"dialog_action": "cancel"`。

**2. 烧写类弹框**（下载时弹）

典型：Memtool 下载后的确认框、OTP Memory Programming tool（曾把自动化卡死）。
三层防护：

1. **预防**：Memtool 配方里 `ScriptIsRunning=True` 关下载后弹窗、擦除模式预设
   免选项框；`strategy=auto` 不走 `DoDownload`（OTP 工具的来源）。
2. **兜底**：哨兵按 `dialog_blockers` 文本特征（默认 `"OTP Memory Programming"`、
   `"Memory Programming"`）识别并**直接关掉**——宁可中止操作，也不误确认烧写。
   遇到新的弹框文本可往这个列表里加。
3. **超时**：万一还是卡住，工具调用以 timeout 报错并提示检查 UDE 窗口，不会永久挂起。

## 给 agent 的典型指令

配好 `default_wsx` + `default_elf` 后：

> 「连上目标板烧写，在 `Com_MainFunctionTx` 打断点，跑起来，命中后把调用栈
> 和 `g_CoreCounter` 给我看」

（`ude_start()`、`ude_load_program()` 都无需报路径）

临时换工程/产物时带上路径：

> 「用 `D:\work\tc397-5.wsx` 连上目标板，烧 `D:\work\proj\_bin\swb\project_B.elf`，……」

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
