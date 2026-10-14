r"""UDE Studio 控制 MCP 服务器（标准 stdio MCP，任意 MCP 客户端可接）。

启动方式（客户端按 MCP 配置拉起本脚本，WSL 或纯 Windows 皆可）：
  python ude_mcp.py
  # 例：Claude Code —— claude mcp add ude -- <python> <...>/ude_mcp.py
  # 例：Cursor/Windsurf 等 —— mcpServers 配 {"command": python, "args": [ude_mcp.py]}

环境变量：
  UDE_BRIDGE_MODE        stdio（纯 Windows 默认：本地子进程）
                         | pipe（WSL 默认：npiperelay 连 Windows 命名管道；
                                纯 Windows 下 pipe=win32file 直连）
  UDE_BACKEND            覆盖服务端后端 mock|real（不设则由 windows/config.json 决定）
  UDE_BRIDGE_PIPE        管道名（默认 \\\\.\\pipe\\ude_bridge）
  UDE_BRIDGE_NPIPERELAY  npiperelay.exe 路径（仅 WSL pipe 模式用）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

import protocol as proto
from bridge_client import BridgeClient, BridgeError, to_windows_path

mcp = MCPServer("ude")

_client = None


def client() -> BridgeClient:
    global _client
    if _client is None:
        _client = BridgeClient()
    return _client


def _call(action: str, params: dict = None, timeout: float = None):
    """调用 bridge，结果/错误统一转成 JSON 文本返回给 Claude。"""
    try:
        result = client().request(action, params, timeout=timeout)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except BridgeError as e:
        return f"ERROR [{e.code}]: {e.message}"


def _wp(path: str) -> str:
    return to_windows_path(path)


# ---------------- 会话管理 ----------------

@mcp.tool()
def ude_start(wsx: str, cfg: str = "", elf: str = "", exe: str = "") -> str:
    """启动 UDE 实例（不需要人工打开 UI）。

    wsx: workspace 工程文件路径（.wsx）。
    cfg: 目标配置文件（.cfg）——传了就新建 workspace；留空则直接打开已有 wsx。
    可选 elf: 启动后立即加载烧写；exe: UDE 可执行文件路径（默认自动探测）。
    WSL 路径会自动转成 Windows 路径。"""
    params = {"wsx": _wp(wsx)}
    if cfg:
        params["cfg"] = _wp(cfg)
    if elf:
        params["elf"] = _wp(elf)
    if exe:
        params["exe"] = _wp(exe)
    return _call(proto.START_SESSION, params, timeout=180.0)


@mcp.tool()
def ude_attach() -> str:
    """附加到已在运行的 UDE 实例（用户自己启动的那个）。"""
    return _call(proto.ATTACH_SESSION)


@mcp.tool()
def ude_status() -> str:
    """查询 UDE 会话状态：连接情况、目标运行状态、已加载程序、workspace 路径等。"""
    return _call(proto.STATUS)


@mcp.tool()
def ude_close() -> str:
    """关闭 UDE 会话（由本 bridge 启动的实例会被关闭；附加的实例只断开引用）。"""
    return _call(proto.CLOSE_SESSION)


@mcp.tool()
def ude_load_program(elf: str, flash: bool = True, verify_only: bool = False,
                     strategy: str = "auto", erase_mode: str = "AutoErase",
                     options: str = "") -> str:
    """加载/烧写程序到目标板。elf 为程序文件路径（.hex/.s19 更稳，.elf 亦可）；
    flash=True 烧写 Flash，verify_only=True 仅校验，flash=False 只加载不烧写。
    strategy: auto（默认，Memtool 配方：LoadFile+ProgramSections，失败退回
    LoadAndFlash）| memtool | load_and_flash | multicore（MultiCoreLoader
    DoDownload，可能弹模态框）。
    erase_mode: AutoErase（默认）| AutoChipErase | SimulateRAM。
    options: 透传给 LoadAndFlash 的选项字符串。"""
    params = {"path": _wp(elf), "flash": flash, "verify_only": verify_only,
              "strategy": strategy}
    if erase_mode:
        params["erase_mode"] = erase_mode
    if options:
        params["options"] = options
    return _call(proto.LOAD_PROGRAM, params, timeout=300.0)


@mcp.tool()
def ude_flash_info() -> str:
    """诊断：列出 Memtool 的 Flash 模块（名称/描述/使能/扇区/段数量）。
    烧写前可先用它确认 OTP 等特殊 Flash 模块的状态。"""
    return _call(proto.FLASH_INFO)


@mcp.tool()
def ude_loader_info() -> str:
    """诊断：列出 MultiCoreLoader 的核名与下载文件清单（含每核 load_bin/load_sym 勾选）。"""
    return _call(proto.LOADER_INFO)


@mcp.tool()
def ude_get_callstack(core: int = -1) -> str:
    """读取当前调用栈（目标需处于停住状态）。
    core: 核序号（0-based）；-1=自动挑命中/活动核。多核停住时只有命中核栈完整，
    其余核是残栈，工具会自动避开；cores 概况里可看各核 Location。"""
    params = {} if core < 0 else {"core": core}
    return _call(proto.CALLSTACK, params)


@mcp.tool()
def ude_get_message_log(limit: int = 50) -> str:
    """读取消息日志（最近 limit 条）。"""
    return _call(proto.MESSAGE_LOG, {"limit": limit})


# ---------------- 运行控制 ----------------

@mcp.tool()
def ude_go(reset_first: bool = False) -> str:
    """从当前 PC 开始/继续运行目标程序。
    reset_first: 断点命中/清除后直接 go 可能跑不起来（或短暂跑动又立刻 break），
    此时传 True 先复位目标再 go（会重启目标程序）。"""
    params = {"reset_first": True} if reset_first else {}
    return _call(proto.GO, params)


@mcp.tool()
def ude_break() -> str:
    """停止目标运行（异步）。"""
    return _call(proto.BREAK)


@mcp.tool()
def ude_step_in() -> str:
    """单步进入（step into）。目标需停住。"""
    return _call(proto.STEP_IN)


@mcp.tool()
def ude_step_over() -> str:
    """单步跳过（step over）。目标需停住。"""
    return _call(proto.STEP_OVER)


@mcp.tool()
def ude_step_out() -> str:
    """单步跳出（step out）。目标需停住。"""
    return _call(proto.STEP_OUT)


@mcp.tool()
def ude_reset() -> str:
    """复位目标板。"""
    return _call(proto.RESET)


@mcp.tool()
def ude_wait_halt(timeout_ms: int = 5000) -> str:
    """等待目标停住（断点命中等），最多 timeout_ms 毫秒。"""
    return _call(proto.WAIT_HALT, {"timeout_ms": timeout_ms}, timeout=timeout_ms / 1000.0 + 10.0)


@mcp.tool()
def ude_target_state() -> str:
    """查询目标运行状态：not_connected / halted / running / inactive。"""
    return _call(proto.TARGET_STATE)


@mcp.tool()
def ude_connect(timeout_ms: int = 10000) -> str:
    """连接目标板（发起调试连接并等待就绪，最多 timeout_ms 毫秒）。"""
    return _call(proto.CONNECT_TARGET, {"timeout_ms": timeout_ms},
                 timeout=timeout_ms / 1000.0 + 10.0)


@mcp.tool()
def ude_disconnect() -> str:
    """断开目标板连接。"""
    return _call(proto.DISCONNECT_TARGET)


# ---------------- 变量 / 寄存器 / 内存 ----------------

@mcp.tool()
def ude_read_variable(expr: str) -> str:
    """读取目标变量/表达式的值。expr 支持符号名与结构体成员，如 g_counter、g_SharedData.Seconds。"""
    return _call(proto.READ_VARIABLE, {"expr": expr})


@mcp.tool()
def ude_write_variable(expr: str, value: float | int | str | bool) -> str:
    """写入目标变量/表达式。value 可为整数、浮点、字符串或布尔。"""
    return _call(proto.WRITE_VARIABLE, {"expr": expr, "value": value})


@mcp.tool()
def ude_read_register(name: str) -> str:
    """读取寄存器值，如 PC、SP、PSW。"""
    return _call(proto.READ_REGISTER, {"name": name})


@mcp.tool()
def ude_write_register(name: str, value: str | int) -> str:
    """写寄存器。value 可为整数或十六进制字符串（如 0x80000000）。"""
    return _call(proto.WRITE_REGISTER, {"name": name, "value": value})


@mcp.tool()
def ude_read_memory(addr: str | int, width: int = 8, size: int = 1) -> str:
    """读目标内存。addr 支持整数或十六进制字符串；width=8/16/32 为单元位宽；
    size 为单元个数（按 width 对齐递增）。返回每个单元的数值列表。"""
    return _call(proto.READ_MEMORY, {"addr": addr, "width": width, "size": size})


@mcp.tool()
def ude_write_memory(addr: str | int, value: list[int] | int, width: int = 8) -> str:
    """写目标内存。value 为单个整数或整数列表（按 width 依次写入）；addr/width 同读内存。"""
    return _call(proto.WRITE_MEMORY, {"addr": addr, "value": value, "width": width})


# ---------------- 断点 ----------------

@mcp.tool()
def ude_breakpoint_add(location: str) -> str:
    """加断点。location 支持：源文件行号 "main.c 352"、函数名 "func_name"、或地址 "0x80000000"。"""
    return _call(proto.BP_ADD, {"location": location})


@mcp.tool()
def ude_breakpoint_remove(location: str) -> str:
    """删除断点，location 同添加。"""
    return _call(proto.BP_REMOVE, {"location": location})


@mcp.tool()
def ude_breakpoint_clear_all() -> str:
    """删除全部断点。"""
    return _call(proto.BP_CLEAR)


@mcp.tool()
def ude_breakpoint_list() -> str:
    """列出断点（记录经本 bridge 添加/删除的断点）。"""
    return _call(proto.BP_LIST)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
