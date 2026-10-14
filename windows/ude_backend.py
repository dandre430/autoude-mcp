"""UDE 后端抽象：MockUdeBackend（内存假目标）与 RealUdeBackend（win32com 调 UDE）。

后端方法抛 BackendError(code, message)，返回值均为可 JSON 序列化的普通 Python 数据。
"""

import json
import os
import re
import time

import protocol as proto


class BackendError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _require_session(state: dict):
    if not state.get("session"):
        raise BackendError(proto.ERR_NO_SESSION, "no UDE session; call start_session or attach_session first")


def _parse_addr(addr) -> int:
    """地址允许 int 或字符串（"0x80000000" / "1234"）。"""
    if isinstance(addr, bool):
        raise BackendError(proto.ERR_BAD_REQUEST, f"bad address: {addr!r}")
    if isinstance(addr, int):
        return addr
    if isinstance(addr, str):
        try:
            return int(addr, 0)
        except ValueError:
            raise BackendError(proto.ERR_BAD_REQUEST, f"bad address: {addr!r}")
    raise BackendError(proto.ERR_BAD_REQUEST, f"bad address: {addr!r}")


def _parse_int(value) -> int:
    if isinstance(value, bool):
        raise BackendError(proto.ERR_BAD_REQUEST, f"bad integer value: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            raise BackendError(proto.ERR_BAD_REQUEST, f"bad integer value: {value!r}")
    raise BackendError(proto.ERR_BAD_REQUEST, f"bad integer value: {value!r}")


# =====================================================================
# Mock 后端
# =====================================================================

class MockUdeBackend:
    """内存假目标：变量/寄存器/内存/断点 + halted/running 状态机。"""

    name = "mock"

    STATE_NAMES = {-1: "not_connected", 0: "halted", 1: "running", 2: "inactive"}

    def __init__(self):
        self.session = False
        self.state_code = -1              # UDE GetState 语义
        self.program_file = ""
        self.workspace_path = ""
        self.project_title = "MockWorkspace"
        self.variables = {
            "g_counter": 42,
            "g_SharedData.Seconds": 7,
            "g_enabled": True,
            "g_pi": 3.5,
        }
        self.registers = {"PC": 0x80001000, "SP": 0x70000F00, "PSW": 0x00000000}
        self.memory = {}                  # addr -> byte
        self.breakpoints = []             # 位置描述字符串
        self.log = []                     # [{"kind","message"}]
        self.callstack_frames = [
            {"index": 0, "function": "main", "file": "main.c", "line": 120, "address": "0x80001000"},
            {"index": 1, "function": "_start", "file": "crt0.s", "line": 42, "address": "0x80000100"},
        ]

    # ---- 会话 ----
    def ping(self, params):
        return {"pong": True, "backend": self.name}

    def start_session(self, params):
        wsx = params.get("wsx") or "mock.wsx"
        cfg = params.get("cfg") or "mock.cfg"
        self.session = True
        self.state_code = 0
        self.workspace_path = wsx
        self._log("info", f"mock session started: wsx={wsx} cfg={cfg}")
        result = self.status({})
        elf = params.get("elf")
        if elf:
            result.update(self.load_program({"path": elf, "flash": True, "verify_only": False}))
        return result

    def attach_session(self, params):
        self.session = True
        if self.state_code == -1:
            self.state_code = 0
        self._log("info", "mock session attached")
        return self.status({})

    def close_session(self, params):
        self.session = False
        self.state_code = -1
        self._log("info", "mock session closed")
        return {"closed": True}

    def status(self, params):
        _require_session(self.__dict__)
        return {
            "backend": self.name,
            "connected": self.state_code != -1,
            "state": self.STATE_NAMES.get(self.state_code, str(self.state_code)),
            "state_code": self.state_code,
            "program_file": self.program_file,
            "workspace_path": self.workspace_path,
            "project_title": self.project_title,
            "breakpoint_count": len(self.breakpoints),
        }

    def load_program(self, params):
        _require_session(self.__dict__)
        path = params.get("path")
        if not path:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: path")
        self.program_file = path
        mode = "verify" if params.get("verify_only") else ("flash" if params.get("flash", True) else "load")
        self._log("success", f"[mock] {mode} program: {path}")
        return {"loaded": True, "path": path, "mode": mode, "strategy": "mock"}

    def flash_info(self, params):
        _require_session(self.__dict__)
        return {
            "num_of_file_sections": 3,
            "flash_modules": [
                {"index": 0, "name": "PFLASH0", "description": "[mock] program flash",
                 "enabled": True, "num_of_sections": 1, "num_of_sectors": 8, "is_ready": True},
                {"index": 1, "name": "DFLASH", "description": "[mock] data flash",
                 "enabled": True, "num_of_sections": 0, "num_of_sectors": 4, "is_ready": True},
            ],
        }

    def loader_info(self, params):
        _require_session(self.__dict__)
        return {
            "cores": ["CPU0", "CPU1"],
            "files": [
                {"index": 0, "path": "[mock] project_A.elf", "hex_elf": True,
                 "cores": {"CPU0": {"load_bin": True, "load_sym": True},
                           "CPU1": {"load_bin": True, "load_sym": True}}},
            ],
        }

    def callstack(self, params):
        _require_session(self.__dict__)
        if self.state_code != 0:
            raise BackendError(proto.ERR_BACKEND, "call stack available only when target is halted")
        return {"frames": list(self.callstack_frames)}

    def message_log(self, params):
        limit = int(params.get("limit") or 50)
        entries = self.log[-limit:] if limit > 0 else list(self.log)
        return {"entries": entries, "total": len(self.log)}

    # ---- 运行控制 ----
    def _check_target_connected(self):
        _require_session(self.__dict__)
        if self.state_code == -1:
            raise BackendError(proto.ERR_NOT_CONNECTED, "target not connected")

    def go(self, params):
        self._check_target_connected()
        self.state_code = 1
        self._log("info", "[mock] go")
        return self.target_state(params)

    def do_break(self, params):
        self._check_target_connected()
        self.state_code = 0
        self._log("info", "[mock] break")
        return self.target_state(params)

    def _step(self, kind):
        self._check_target_connected()
        if self.state_code != 0:
            raise BackendError(proto.ERR_BACKEND, f"cannot {kind} while target is not halted")
        frame = self.callstack_frames[0]
        frame["line"] += 1
        self._log("info", f"[mock] {kind}")
        return self.target_state({})

    def step_in(self, params):
        return self._step("step_in")

    def step_over(self, params):
        return self._step("step_over")

    def step_out(self, params):
        return self._step("step_out")

    def reset(self, params):
        self._check_target_connected()
        self.state_code = 2
        self._log("info", "[mock] reset")
        return self.target_state(params)

    def wait_halt(self, params):
        self._check_target_connected()
        if self.state_code == 0:
            return {"halted": True, "state": "halted"}
        if self.state_code == 1:
            # mock 里 break 条件命中即停住；简化为直接停住
            self.state_code = 0
            return {"halted": True, "state": "halted"}
        return {"halted": False, "state": self.STATE_NAMES.get(self.state_code, str(self.state_code))}

    def target_state(self, params):
        _require_session(self.__dict__)
        return {
            "state": self.STATE_NAMES.get(self.state_code, str(self.state_code)),
            "state_code": self.state_code,
            "connected": self.state_code != -1,
        }

    def connect_target(self, params):
        _require_session(self.__dict__)
        if self.state_code == -1:
            self.state_code = 0
        self._log("info", "[mock] connect target")
        return self.target_state(params)

    def disconnect_target(self, params):
        _require_session(self.__dict__)
        self.state_code = -1
        self._log("info", "[mock] disconnect target")
        return self.target_state(params)

    # ---- 变量 / 寄存器 / 内存 ----
    def read_variable(self, params):
        self._check_target_connected()
        expr = params.get("expr")
        if not expr:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: expr")
        if self.state_code != 0:
            raise BackendError(proto.ERR_BACKEND, "variable read requires halted target")
        if expr not in self.variables:
            raise BackendError(proto.ERR_BACKEND, f"unknown symbol: {expr}")
        value = self.variables[expr]
        return {"expr": expr, "value": value, "type": type(value).__name__}

    def write_variable(self, params):
        self._check_target_connected()
        expr = params.get("expr")
        if expr is None or "value" not in params:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing params: expr/value")
        if self.state_code != 0:
            raise BackendError(proto.ERR_BACKEND, "variable write requires halted target")
        self.variables[expr] = params["value"]
        return {"expr": expr, "value": params["value"]}

    def read_register(self, params):
        self._check_target_connected()
        name = params.get("name")
        if not name:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: name")
        if name not in self.registers:
            raise BackendError(proto.ERR_BACKEND, f"unknown register: {name}")
        value = self.registers[name]
        return {"name": name, "value": value, "hex": hex(value) if isinstance(value, int) else None}

    def write_register(self, params):
        self._check_target_connected()
        name = params.get("name")
        if name is None or "value" not in params:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing params: name/value")
        self.registers[name] = _parse_int(params["value"])
        return {"name": name, "value": self.registers[name]}

    def _read_mem_bytes(self, addr: int, nbytes: int) -> list:
        return [self.memory.get(addr + i, 0) for i in range(nbytes)]

    def read_memory(self, params):
        self._check_target_connected()
        addr = _parse_addr(params.get("addr"))
        width = int(params.get("width") or 8)
        size = params.get("size")
        if width not in (8, 16, 32):
            raise BackendError(proto.ERR_BAD_REQUEST, "width must be 8, 16 or 32")
        unit = width // 8
        if size is None:
            count = 1
        else:
            count = _parse_int(size)
            if count <= 0 or count > 65536:
                raise BackendError(proto.ERR_BAD_REQUEST, "size must be 1..65536 units")
        data = []
        for i in range(count):
            raw = self._read_mem_bytes(addr + i * unit, unit)
            value = 0
            for j, b in enumerate(raw):
                value |= (b & 0xFF) << (8 * j)
            data.append(value)
        return {"addr": addr, "addr_hex": hex(addr), "width": width, "count": count, "data": data}

    def write_memory(self, params):
        self._check_target_connected()
        addr = _parse_addr(params.get("addr"))
        width = int(params.get("width") or 8)
        if width not in (8, 16, 32):
            raise BackendError(proto.ERR_BAD_REQUEST, "width must be 8, 16 or 32")
        values = params.get("value")
        if not isinstance(values, list):
            values = [values]
        if not values:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: value")
        unit = width // 8
        for i, v in enumerate(values):
            iv = _parse_int(v)
            for j in range(unit):
                self.memory[addr + i * unit + j] = (iv >> (8 * j)) & 0xFF
        return {"addr": addr, "addr_hex": hex(addr), "width": width, "count": len(values)}

    # ---- 断点 ----
    def bp_add(self, params):
        _require_session(self.__dict__)
        location = params.get("location")
        if not location:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: location")
        self.breakpoints.append(location)
        self._log("info", f"[mock] breakpoint added: {location}")
        return {"location": location, "count": len(self.breakpoints)}

    def bp_remove(self, params):
        _require_session(self.__dict__)
        location = params.get("location")
        if not location:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: location")
        if location in self.breakpoints:
            self.breakpoints.remove(location)
            return {"location": location, "removed": True, "count": len(self.breakpoints)}
        raise BackendError(proto.ERR_BACKEND, f"breakpoint not found: {location}")

    def bp_clear(self, params):
        _require_session(self.__dict__)
        n = len(self.breakpoints)
        self.breakpoints = []
        return {"removed": n}

    def bp_list(self, params):
        _require_session(self.__dict__)
        return {"breakpoints": [{"location": loc} for loc in self.breakpoints]}

    # ---- 内部 ----
    def _log(self, kind, message):
        self.log.append({"kind": kind, "message": message})


# =====================================================================
# 真实 UDE COM 后端
# =====================================================================

_STATE_NAMES = {-1: "not_connected", 0: "halted", 1: "running", 2: "inactive"}


class RealUdeBackend:
    """通过 win32com 驱动 UDE Studio。仅在 Windows 上可用。"""

    name = "real"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.ude_exe = self.config.get("ude_exe") or self._probe_ude_exe()
        self.launcher = None       # UDELauncher 或 UDERunningInstanceManager
        self.app = None            # IUDEApplication
        self.workspace = None
        self.debugger = None       # 当前 CoreDebugger
        self.attached_mode = False
        self._local_breakpoints = []   # COM 枚举能力弱，自行记录描述
        self.last_program_path = None  # 最近加载过的程序文件（符号来源）

    # ---- COM 基础 ----
    def _dispatch(self, prog_id):
        try:
            import win32com.client
        except ImportError:
            raise BackendError(proto.ERR_BACKEND, "pywin32 (win32com) is not installed on this machine")
        try:
            return win32com.client.Dispatch(prog_id)
        except Exception as e:
            raise BackendError(proto.ERR_BACKEND, f"COM Dispatch({prog_id}) failed: {e}")

    def _com_call(self, fn, *args):
        """调用 COM 方法，异常转 BackendError。"""
        try:
            return fn(*args)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(proto.ERR_BACKEND, f"UDE COM error: {e}")

    @staticmethod
    def _probe_ude_exe():
        candidates = [
            r"C:\Program Files\pls\UDE Starterkit 2021\UDEVisualPlatform.exe",
            r"D:\Program Files (x86)\pls\UDE 4.10\UDEVisualPlatform.exe",
            r"C:\Program Files (x86)\pls\UDE 4.10\UDEVisualPlatform.exe",
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return "UDEVisualPlatform.exe"

    def _get_debugger(self, core=0):
        if self.debugger is not None:
            return self.debugger
        if self.workspace is None:
            raise BackendError(proto.ERR_NO_SESSION, "no UDE session; call start_session or attach_session first")
        # 优先取活动核（TC397 多核时 CoreDebugger(0) 未必是活动的）
        dbg = None
        try:
            dbg = self._com_call(lambda: self.workspace.GetActiveCoreDebugger())
        except Exception:
            dbg = None
        if dbg is None:
            dbg = self._com_call(self.workspace.CoreDebugger, core)
        # 刚启动时可能拿到无效桩对象（任何调用都 E_INVALIDARG），不缓存，
        # 等调试服务器就绪后重解析
        try:
            self._com_call(dbg.GetState)
        except Exception:
            return dbg
        self.debugger = dbg
        return dbg

    def _check_target(self):
        dbg = self._get_debugger()
        try:
            connected = bool(self._com_call(lambda: dbg.Connected))
        except BackendError:
            raise
        if not connected:
            raise BackendError(proto.ERR_NOT_CONNECTED, "target not connected")
        return dbg

    def _iter_core_debuggers(self):
        """枚举工作区所有核调试器，产出 (index, dbg)。取不到的条目跳过。"""
        if self.workspace is None:
            return
        count = 0
        for name in ("CoreDebuggerCnt", "GetCoreDebuggerCnt", "CoreDebuggerCount"):
            try:
                count = int(self._com_call(lambda n=name: getattr(self.workspace, n)))
                break
            except Exception:
                continue
        for i in range(max(0, min(count, 32))):
            try:
                dbg = self._com_call(self.workspace.CoreDebugger, i)
            except Exception:
                continue
            if dbg is not None:
                yield i, dbg

    @staticmethod
    def _program_file_of(dbg):
        """ProgramFile 属性在部分实例上 PROPERTYGET 报 TYPEMISMATCH，多形态兜底。"""
        for fn in (lambda: getattr(dbg, "ProgramFile"),
                   lambda: dbg.GetProgramFile(),
                   lambda: getattr(dbg, "get_ProgramFile")()):
            try:
                v = fn()
                if v is not None:
                    return str(v)
            except Exception:
                continue
        return ""

    def _cores_summary(self):
        """每个核的名称/符号文件/连接状态——诊断符号是否加载。"""
        cores = []
        for i, dbg in self._iter_core_debuggers():
            info = {"index": i}
            try:
                info["name"] = str(self._com_call(lambda d=dbg: d.Name))
            except Exception as e:
                info["name"] = f"<err {e}>"
            try:
                info["program_file"] = self._com_call(self._program_file_of, dbg) or ""
            except Exception as e:
                info["program_file"] = f"<err {e}>"
            try:
                info["connected"] = bool(self._com_call(lambda d=dbg: d.Connected))
            except Exception:
                info["connected"] = None
            try:
                state = int(self._com_call(dbg.GetState))
                info["state"] = _STATE_NAMES.get(state, str(state))
            except Exception:
                info["state"] = None
            cores.append(info)
        return cores

    def _get_live_app(self, mgr, count=None):
        """逐实例探测 UDERunningInstanceManager，取第一个能返回 Workspace 的
        活实例。0 号可能是已退出实例的死桩（调用报 RPC 服务器不可用）。
        返回 (app, probed_notes)；无活实例 app 为 None。"""
        if count is None:
            try:
                count = int(self._com_call(lambda: mgr.InstanceCount))
            except Exception:
                count = 1
        probed = []
        for i in range(max(count, 1)):
            try:
                cand = self._com_call(mgr.GetInstance, i)
            except Exception as e:
                probed.append(f"{i}: GetInstance failed ({e})")
                continue
            if cand is None:
                probed.append(f"{i}: None")
                continue
            try:
                ws = self._com_call(lambda a=cand: a.Workspace)
                if ws is not None:
                    return cand, probed
                probed.append(f"{i}: Workspace is None")
            except Exception as e:
                probed.append(f"{i}: {e}")
        return None, probed

    def _wait_debuggers_ready(self, timeout_s=30.0):
        """UDE 刚拉起时 workspace 在异步加载，CoreDebugger 未就绪前一切调试调用
        都 E_INVALIDARG。轮询到核调试器出现且可调用；超时则尝试经
        UDERunningInstanceManager 重取 app。返回诊断 dict。"""
        t0 = time.time()
        waited = 0.0
        last_err = None
        while waited < timeout_s:
            cores = list(self._iter_core_debuggers())
            if cores:
                try:
                    self._com_call(cores[0][1].GetState)
                    self.debugger = None
                    return {"ready": True, "waited_s": round(waited, 1),
                            "core_count": len(cores)}
                except Exception as e:
                    last_err = str(e)
            try:
                # UDE 自己的 Sleep 会泵 COM 消息
                self._com_call(self.workspace.Sleep, 250)
            except Exception:
                time.sleep(0.25)
            waited = time.time() - t0
        # 超时：launcher 返回的 app 可能只是壳，用 RunningInstanceManager 重取
        try:
            mgr = self._dispatch("UDERunningInstanceManager")
            app, _probed = self._get_live_app(mgr)
            if app is not None:
                self.app = app
                self.workspace = self._com_call(lambda: self.app.Workspace)
                self.debugger = None
                cores = list(self._iter_core_debuggers())
                if cores:
                    self._com_call(cores[0][1].GetState)
                    return {"ready": True, "waited_s": round(waited, 1),
                            "core_count": len(cores), "via": "reattach"}
        except Exception as e:
            last_err = str(e)
        return {"ready": False, "waited_s": round(waited, 1), "core_count": 0,
                "last_error": last_err}

    def _get_expression(self, dbg, expr):
        """取 IUDEExpression 对象：dbg.GetExpression / dbg.Expression / dbg.Symbols.*。"""
        last = None
        for obj, name in ((dbg, "GetExpression"), (dbg, "Expression"),
                          (getattr(dbg, "Symbols", None), "GetExpression"),
                          (getattr(dbg, "Symbols", None), "Expression")):
            if obj is None:
                continue
            getter = getattr(obj, name, None)
            if getter is None:
                continue
            try:
                eobj = getter(expr)
                if eobj is not None:
                    return eobj
            except Exception as e:
                last = e
        if last is not None:
            raise RuntimeError(f"no Expression accessor ({last})")
        raise RuntimeError("no Expression accessor")

    @staticmethod
    def _prop(obj, name):
        v = getattr(obj, name)
        return v() if callable(v) else v

    def _resolve_symbol_address(self, dbg, expr):
        """符号 → 地址（0x... 字符串）；解析不了返回 None。"""
        try:
            eobj = self._get_expression(dbg, expr)
        except Exception:
            return None
        for attr in ("LocationString", "Location"):
            try:
                v = self._prop(eobj, attr)
            except Exception:
                continue
            if v is None:
                continue
            s = str(v).strip()
            try:
                return hex(_parse_addr(s))
            except Exception:
                if s:
                    return s
        return None

    # ---- 会话 ----
    def ping(self, params):
        return {"pong": True, "backend": self.name, "ude_exe": self.ude_exe}

    def start_session(self, params):
        wsx = params.get("wsx")
        cfg = params.get("cfg")
        exe = params.get("exe") or self.ude_exe
        if not wsx:
            raise BackendError(proto.ERR_BAD_REQUEST, "start_session requires param: wsx")
        # What 参数只接受产品 exe 名（文档 Valid values: UDEVisualPlatform.exe /
        # UDEDesktop.exe），传完整路径会 E_INVALIDARG
        what = "UDEVisualPlatform.exe"
        if exe:
            base = os.path.basename(str(exe).replace("/", "\\"))
            if base.lower() in ("udevisualplatform.exe", "udedesktop.exe"):
                what = base
        self.launcher = self._dispatch("UDELauncher")
        if cfg:
            attempts = [
                ("StartAndCreateWorkspace(what,wsx,cfg,False)",
                 lambda: self._com_call(self.launcher.StartAndCreateWorkspace,
                                        what, wsx, cfg, False)),
                ("StartAndCreateWorkspace(what,wsx,cfg,True)",
                 lambda: self._com_call(self.launcher.StartAndCreateWorkspace,
                                        what, wsx, cfg, True)),
            ]
        else:
            attempts = [
                ("StartAndOpenWorkspace(what,wsx)",
                 lambda: self._com_call(self.launcher.StartAndOpenWorkspace, what, wsx)),
                ("StartAndOpenWorkspace(what,wsx,None)",
                 lambda: self._com_call(self.launcher.StartAndOpenWorkspace, what, wsx, None)),
                # Options 是命令行参数，把 wsx 当命令行传给新实例
                ("StartInstance(what,wsx)",
                 lambda: self._com_call(self.launcher.StartInstance, what, wsx)),
            ]
        self.app = None
        errors = []
        used_label = None
        for label, fn in attempts:
            try:
                self.app = fn()
                if self.app is not None:
                    used_label = label
                    break
                errors.append(f"{label}: returned no application object")
            except BackendError as e:
                errors.append(f"{label}: {e.message}")
        if self.app is None:
            raise BackendError(proto.ERR_BACKEND,
                               f"failed to start UDE instance; attempts={errors}")
        self.workspace = self._com_call(lambda: self.app.Workspace)
        self.debugger = None
        self.attached_mode = False
        self._local_breakpoints = []
        self.last_program_path = None
        ready = self._wait_debuggers_ready(30.0)
        result = self.status({})
        result["start_method"] = used_label
        result["ready"] = ready
        elf = params.get("elf")
        if elf:
            result.update(self.load_program({"path": elf, "flash": True, "verify_only": False}))
        return result

    def attach_session(self, params):
        mgr = self._dispatch("UDERunningInstanceManager")
        try:
            count = int(self._com_call(lambda: mgr.InstanceCount))
        except Exception:
            count = None
        app, probed = self._get_live_app(mgr, count)
        if app is None:
            raise BackendError(
                proto.ERR_BACKEND,
                "no accessible running UDE instance (InstanceCount="
                f"{count}; probed: {probed}). Open UDE Studio first; it must "
                "run as the same Windows user as this bridge server.")
        self.app = app
        self.workspace = self._com_call(lambda: self.app.Workspace)
        self.debugger = None
        self.attached_mode = True
        self.launcher = None
        self._local_breakpoints = []
        ready = self._wait_debuggers_ready(15.0)
        result = self.status({})
        result["ready"] = ready
        return result

    def close_session(self, params):
        closed = False
        try:
            if self.app is not None and not self.attached_mode and self.launcher is not None:
                self._com_call(self.launcher.StopInstance, self.app)
                closed = True
        finally:
            self.debugger = None
            self.workspace = None
            self.app = None
            self.launcher = None
            self.attached_mode = False
            self._local_breakpoints = []
        return {"closed": closed}

    def status(self, params):
        if self.workspace is None:
            raise BackendError(proto.ERR_NO_SESSION, "no UDE session; call start_session or attach_session first")
        try:
            dbg = self._get_debugger()
            state_code = int(self._com_call(dbg.GetState))
            connected = bool(self._com_call(lambda: dbg.Connected))
            try:
                program_file = self._com_call(self._program_file_of, dbg) or ""
            except Exception:
                program_file = ""
        except BackendError:
            state_code, connected, program_file = -1, False, ""
        try:
            project_title = str(self._com_call(lambda: self.workspace.ProjectTitle))
        except Exception:
            project_title = ""
        try:
            workspace_path = str(self._com_call(lambda: self.workspace.WorkspacePath))
        except Exception:
            workspace_path = ""
        try:
            cores = self._cores_summary()
        except Exception:
            cores = []
        return {
            "backend": self.name,
            "attached": self.attached_mode,
            "connected": connected,
            "state": _STATE_NAMES.get(state_code, str(state_code)),
            "state_code": state_code,
            "program_file": program_file,
            "workspace_path": workspace_path,
            "project_title": project_title,
            "breakpoint_count": len(self._local_breakpoints),
            "cores": cores,
        }

    # ---- Flash / 烧写 ----

    def _get_memtool(self, dbg):
        """取当前核关联的 Memtool（FLASH 编程插件），即 UI「Program All」的执行体。"""
        return self._com_call(dbg.GetMemtool)

    def _memtool_info(self, memtool):
        info = {}
        try:
            info["num_of_file_sections"] = int(self._com_call(lambda: memtool.NumOfFileSections))
        except Exception:
            info["num_of_file_sections"] = None
        mods = []
        try:
            count = int(self._com_call(lambda: memtool.NumOfFlashMods))
        except Exception:
            count = 0
        for i in range(count):
            entry = {"index": i}
            try:
                mod = self._com_call(memtool.GetFlashMod, i)
                for attr, key, conv in (
                        ("Name", "name", str),
                        ("Description", "description", str),
                        ("Enabled", "enabled", bool),
                        ("NumOfSections", "num_of_sections", int),
                        ("NumOfSectors", "num_of_sectors", int),
                        ("IsReady", "is_ready", bool)):
                    try:
                        entry[key] = conv(self._com_call(lambda m=mod, a=attr: getattr(m, a)))
                    except Exception:
                        pass
                # 地址窗（首扇区首 range 起点 → 末扇区末 range 终点）
                try:
                    nsec = entry.get("num_of_sectors") or 0
                    if nsec:
                        sec0 = self._com_call(mod.GetSector, 0)
                        rng0 = self._com_call(sec0.Range, 0)
                        start = int(self._com_call(lambda r=rng0: r.ProgTimeStartAddr))
                        last = self._com_call(mod.GetSector, nsec - 1)
                        nrange = int(self._com_call(lambda s=last: s.NumOfRanges))
                        rlast = self._com_call(last.Range, nrange - 1)
                        end = (int(self._com_call(lambda r=rlast: r.ProgTimeStartAddr))
                               + int(self._com_call(lambda r=rlast: r.Size)))
                        entry["addr_range"] = f"0x{start & 0xFFFFFFFF:08X}-0x{end & 0xFFFFFFFF:08X}"
                except Exception:
                    pass
            except Exception as e:
                entry["error"] = str(e)
            mods.append(entry)
        info["flash_modules"] = mods
        return info

    def flash_info(self, params):
        """诊断：列出 Memtool 的 Flash 模块（名称/使能/扇区数）与文件段数量。"""
        dbg = self._get_debugger()
        memtool = self._get_memtool(dbg)
        return self._memtool_info(memtool)

    def loader_info(self, params):
        """诊断：列出 MultiCoreLoader 的核与下载文件清单（含每核 bin/sym 勾选）。"""
        ws = self.workspace
        loader = self._com_call(lambda: ws.MultiCoreLoader)
        try:
            core_count = int(self._com_call(loader.GetCoreCount))
            cores = [str(self._com_call(loader.GetCoreName, i)) for i in range(core_count)]
        except Exception as e:
            return {"error": f"GetCoreCount failed: {e}", "cores": [], "files": []}
        try:
            file_count = int(self._com_call(loader.GetFileCount))
        except Exception:
            file_count = 0
        files = []
        for i in range(file_count):
            entry = {"index": i}
            try:
                entry["path"] = str(self._com_call(loader.GetFilePath, i))
            except Exception as e:
                entry["path_error"] = str(e)
            try:
                entry["hex_elf"] = bool(self._com_call(loader.GetHexElf, i))
            except Exception:
                pass
            per_core = {}
            for ci, cname in enumerate(cores):
                flags = {}
                try:
                    flags["load_bin"] = bool(self._com_call(loader.GetLoadBin, i, ci))
                except Exception as e:
                    flags["load_bin_error"] = str(e)
                try:
                    flags["load_sym"] = bool(self._com_call(loader.GetLoadSym, i, ci))
                except Exception as e:
                    flags["load_sym_error"] = str(e)
                per_core[cname] = flags
            entry["cores"] = per_core
            files.append(entry)
        return {"cores": cores, "files": files}

    def load_program(self, params):
        path = params.get("path")
        if not path:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: path")
        dbg = self._check_target()
        strategy = (params.get("strategy") or "auto").lower()
        # auto 不含 multicore：DoDownload 会跑完整下载配置（曾拉起 OTP Memory
        # Programming tool 模态框卡死），必须显式指定 strategy="multicore" 才用
        orders = {
            "auto": ("memtool", "load_and_flash"),
            "memtool": ("memtool",),
            "load_and_flash": ("load_and_flash",),
            "multicore": ("multicore",),
        }
        if strategy not in orders:
            raise BackendError(proto.ERR_BAD_REQUEST,
                               f"unknown strategy: {strategy} (auto|memtool|load_and_flash|multicore)")
        attempts = []
        for name in orders[strategy]:
            fn = getattr(self, f"_load_{name}")
            try:
                result = fn(dbg, path, params)
            except BackendError as e:
                attempts.append({"strategy": name, "error": e.message})
                continue
            except Exception as e:
                attempts.append({"strategy": name, "error": f"{type(e).__name__}: {e}"})
                continue
            if result.get("ok"):
                self.last_program_path = path
                result.update({"loaded": True, "path": path, "strategy": name,
                               "attempts": attempts,
                               "symbols": self._load_symbols_all_cores(path)})
                return result
            attempts.append({"strategy": name, "error": result.get("error") or "returned false"})
        raise BackendError(
            proto.ERR_BACKEND,
            f"load program failed for {path}; attempts={attempts}")

    def _load_symbols_all_cores(self, path):
        """把程序文件（符号）装进每个核调试器——变量/断点依赖符号。

        多核 ELF（如 project_A.elf 含 6 核）需要逐核 LoadProgramFile。
        """
        out = []
        for i, dbg in self._iter_core_debuggers():
            entry = {"index": i}
            try:
                entry["name"] = str(self._com_call(lambda d=dbg: d.Name))
            except Exception:
                entry["name"] = None
            try:
                entry["load_program_file"] = bool(self._com_call(dbg.LoadProgramFile, path))
            except Exception as e:
                entry["load_program_file"] = f"err {e}"
            try:
                entry["program_file"] = self._com_call(self._program_file_of, dbg) or None
            except Exception:
                entry["program_file"] = None
            out.append(entry)
        return out

    # 擦除/校验模式取值（UDE 官方宏样例 page_usage_internal_macros）
    ERASE_MODES = ("AutoChipErase", "AutoErase", "SimulateRAM")
    VERIFY_MODES = ("AutoVerify", "VerifyByCrc")

    @staticmethod
    def _set_config_param(obj, name, value):
        """设 ConfigParam：优先 SetConfigParam 方法，退回带参属性 put。"""
        try:
            return obj.SetConfigParam(name, value)
        except AttributeError:
            pass
        import pythoncom
        from win32com.client import DISPATCH_PROPERTYPUT
        ole = obj._oleobj_
        dispid = ole.GetIDsOfNames("ConfigParam")
        if isinstance(dispid, (list, tuple)):
            dispid = dispid[0]
        return ole.Invoke(dispid, 0, DISPATCH_PROPERTYPUT, value, name)

    def _load_memtool(self, dbg, path, params):
        """官方宏样例 LoadHexToFlash 的自动化等价
        （page_usage_internal_macros.html）：

        0. 关 UDE service 模式（否则不允许下载 programming pattern）
           + MemTool.ConfigParam("ScriptIsRunning")=True（关掉下载后的 memtool 弹窗）
        1. Flash.ConfigParam(erase_mode)=True（不设会弹选项界面等选擦除模式）
        2. 下载填满 flash pattern ranges（样例用 LoadProgramFile，hex/elf 皆可）
        3. 逐 Flash 模块对 NumOfSections<>0 执行 ProgramSections（= UI Program All）
        """
        memtool = self._get_memtool(dbg)
        erase = params.get("erase_mode") or "AutoErase"
        if erase not in self.ERASE_MODES:
            raise BackendError(proto.ERR_BAD_REQUEST,
                               f"bad erase_mode: {erase} ({'|'.join(self.ERASE_MODES)})")
        verify_mode = params.get("verify_mode")
        if verify_mode and verify_mode not in self.VERIFY_MODES:
            raise BackendError(proto.ERR_BAD_REQUEST,
                               f"bad verify_mode: {verify_mode} ({'|'.join(self.VERIFY_MODES)})")
        notes = []

        # 0. service 模式（样例注释：Switch off UDE service mode to allow
        #    download of programming pattern）；结束时恢复原值
        cfg = self._com_call(lambda: self.workspace.UserProfile)
        saved = {}
        for entry, section in (("Service", "System"), ("PROGRAM1_SERVICE_MODE", "System")):
            try:
                saved[(entry, section)] = self._com_call(cfg.GetConfigParam, entry, section)
            except Exception:
                saved[(entry, section)] = None
        try:
            self._com_call(cfg.SetConfigParam, "Service", "System", False)
            self._com_call(cfg.SetConfigParam, "PROGRAM1_SERVICE_MODE", "System", 0)
        except Exception as e:
            notes.append(f"service-mode switch failed: {e}")
        # 关掉下载后的 memtool 对话框（样例：ScriptIsRunning=True）
        try:
            self._set_config_param(memtool, "ScriptIsRunning", True)
        except Exception as e:
            notes.append(f"ScriptIsRunning not set ({e}); 下载后若弹 memtool 框需手关")

        try:
            return self._load_memtool_inner(dbg, memtool, path, params,
                                            erase, verify_mode, notes)
        finally:
            for (entry, section), val in saved.items():
                if val is None:
                    continue
                try:
                    self._com_call(cfg.SetConfigParam, entry, section, val)
                except Exception:
                    pass

    def _load_memtool_inner(self, dbg, memtool, path, params, erase, verify_mode, notes):
        # memtool 自身的目标连接
        try:
            conn = bool(self._com_call(memtool.IsTargetConnected, True))
        except Exception:
            conn = True
        if not conn:
            ok = bool(self._com_call(memtool.ConnectTarget))
            notes.append(f"ConnectTarget -> {ok}")

        # 1. 擦除/校验模式（仅使能模块）+ 收集参与烧写的模块
        mods = []
        count = int(self._com_call(lambda: memtool.NumOfFlashMods))
        for i in range(count):
            mod = self._com_call(memtool.GetFlashMod, i)
            try:
                enabled = bool(self._com_call(lambda m=mod: m.Enabled))
            except Exception:
                enabled = True
            if not enabled:
                continue
            self._call_mod_config(mod, erase, True, notes)
            if verify_mode:
                self._call_mod_config(mod, verify_mode, True, notes)
            mods.append(mod)

        # 2. 填充 flash pattern ranges（多候选，记录每次尝试）
        fill_attempts = []

        def try_fill(label, fn):
            try:
                r = fn()
                ok = True if r is None else bool(r)
                err = None
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"
            fill_attempts.append({"method": label, "ok": ok,
                                  **({"error": err} if err else {})})
            return ok

        filled = try_fill("LoadFile(path,0)",
                          lambda: self._com_call(memtool.LoadFile, path, 0))
        if not filled:
            filled = try_fill("LoadProgramFile(path)",
                              lambda: self._com_call(dbg.LoadProgramFile, path))
        if not filled:
            filled = try_fill("OpenFile+AddFileSections", lambda: (
                self._com_call(memtool.OpenFile, path, True, 0),
                self._com_call(memtool.AddFileSections, False))[1])
        if not filled:
            return {"ok": False,
                    "error": "all fill methods failed (service mode? section outside "
                             "enabled flash modules?)",
                    "fill_attempts": fill_attempts, "notes": notes,
                    "memtool": self._memtool_info(memtool)}
        fill = fill_attempts[-1]["method"]
        diag = self._memtool_info(memtool)

        if params.get("flash", True) is False:
            return {"ok": True, "mode": "load_only", "fill": fill,
                    "fill_attempts": fill_attempts, "notes": notes, "memtool": diag}

        # 3. 逐模块烧写/校验（只处理有数据段的模块）
        programmed, verified, skipped = [], [], []
        for mod in mods:
            try:
                n = int(self._com_call(lambda m=mod: m.NumOfSections))
                name = str(self._com_call(lambda m=mod: m.Name))
            except Exception:
                continue
            if n == 0:
                skipped.append(name)
                continue
            if params.get("verify_only"):
                try:
                    res = self._com_call(mod.VerifySectionsExt, "", None)
                except Exception:
                    res = self._com_call(mod.VerifySections, "")
                if not res:
                    return {"ok": False, "error": f"VerifySections failed on {name}",
                            "verified": verified, "fill": fill, "notes": notes,
                            "memtool": diag}
                verified.append(name)
                continue
            ok, err = False, None
            try:
                res = self._com_call(mod.ProgramSectionsExt, None)
                ok = True if res is None else bool(res)
            except Exception as e:
                err = e
            if not ok:
                try:
                    self._com_call(mod.ProgramSections)  # 无返回值，抛异常即失败
                    ok = True
                except Exception as e:
                    err = e
            if not ok:
                return {"ok": False,
                        "error": f"ProgramSections failed on {name}: {err}",
                        "programmed": programmed, "fill": fill, "notes": notes,
                        "memtool": diag}
            programmed.append(name)
        return {"ok": True,
                "mode": "verify" if params.get("verify_only") else "program_all",
                "fill": fill, "fill_attempts": fill_attempts, "erase_mode": erase,
                "verify_mode": verify_mode, "programmed": programmed,
                "verified": verified, "skipped": skipped, "notes": notes, "memtool": diag}

    def _call_mod_config(self, mod, name, value, notes):
        try:
            self._com_call(mod.SetConfigParam, name, value)
        except Exception as e:
            try:
                self._set_config_param(mod, name, value)
            except Exception as e2:
                notes.append(f"ConfigParam({name}) failed: {e} / {e2}")

    def _load_load_and_flash(self, dbg, path, params):
        """官方示例的单核一把梭：LoadAndFlash(Path, Options)。

        Options 文档未给取值；按宏样例的命名习惯试擦除模式串（默认 AutoErase）。
        """
        options = params.get("options")
        if options is None:
            if params.get("verify_only"):
                options = "VerifyOnly"
            else:
                options = params.get("erase_mode") or "AutoErase"
        try:
            ok = bool(self._com_call(dbg.LoadAndFlash, path, options))
        except Exception as e:
            return {"ok": False, "error": f"LoadAndFlash({path!r}, {options!r}) exception: {e}"}
        return {"ok": ok, "mode": "load_and_flash", "options": options,
                "error": None if ok else f"LoadAndFlash({path!r}, {options!r}) returned False"}

    def _load_multicore(self, dbg, path, params):
        """MultiCoreLoader 路径（对应 UI: Download → multicore 确认）。

        注意：DoDownload 会执行完整下载配置，可能弹模态框（如 OTP 工具），
        仅在显式 strategy=multicore 时使用。
        """
        ws = self.workspace
        loader = self._com_call(lambda: ws.MultiCoreLoader)
        idx = None
        try:
            idx = int(self._com_call(loader.GetFileIndex, path))
        except Exception:
            idx = None
        if idx is None or idx < 0:
            idx = int(self._com_call(loader.AddFile, path))
        try:
            core_count = int(self._com_call(loader.GetCoreCount))
            cores = [str(self._com_call(loader.GetCoreName, i)) for i in range(core_count)]
        except Exception:
            cores = []
        options = params.get("options")
        if options is None and params.get("verify_only"):
            options = "VerifyOnly"
        candidates = [options] if options is not None else [None, "", "ProgramAll"]
        ok, used, err = False, None, None
        for opt in candidates:
            try:
                ok = bool(self._com_call(loader.DoDownload, opt))
                used = opt
            except Exception as e:
                err = e
                ok = False
            if ok:
                break
        return {"ok": ok, "mode": "download", "file_index": idx, "cores": cores,
                "options": used,
                "error": None if ok else f"DoDownload failed (tried {candidates}): {err}"}

    def _function_at(self, dbg, addr):
        """地址 → 函数名：Symbols.Functions.ItemFromAddress(addr).Name。

        COM 失败向上抛（调用方 fail-fast），查不到符号才返回 None。
        """
        funcs = self._com_call(lambda: dbg.Symbols.Functions)
        if funcs is None:
            return None
        fn = None
        last = None
        for a in (addr, _hex32(addr)):
            try:
                fn = self._com_call(funcs.ItemFromAddress, a)
            except Exception as e:
                last = e
                continue
            if fn is not None:
                break
        if fn is None:
            if last is not None:
                raise last
            return None
        if isinstance(fn, str):
            return fn
        name = self._com_call(lambda o=fn: o.Name)
        return str(name) if name else None

    def callstack(self, params):
        # 保守取栈：只对指定/活动核读 CallStack。断点命中后 UDE 的栈回溯
        # 是异步完成的——太早读会残栈（"unable to get callframe N"），而对
        # 陪停核批量读 CallStack 会把 debug server 堵死（UDE 未响应）。
        # 其它核只报 Location 符号串概况（该属性安全、瞬回）。
        dbg_active = self._get_debugger()
        cores = list(self._iter_core_debuggers())
        want = params.get("core")
        target_dbg = dbg_active
        target_label = "active"
        if want is not None and cores:
            idx = int(want)
            for i, cdbg in cores:
                if i == idx:
                    target_dbg, target_label = cdbg, f"CoreDebugger({i})"
                    break
            else:
                raise BackendError(proto.ERR_BAD_REQUEST, f"no such core: {want}")
        elif cores:
            # 默认选「命中」的核：陪停核的 CallStack 是残栈。
            # 优先 Breakpoints 命中标志（文档 IUDEBreakpoints.HitBreakpoint/
            # BreakpointHit），失败则退回第一个已连接核。
            hit = None
            for i, cdbg in cores:
                try:
                    bps = self._com_call(lambda d=cdbg: d.Breakpoints)
                    v = None
                    for attr in ("HitBreakpoint", "BreakpointHit"):
                        try:
                            v = self._com_call(lambda b=bps, a=attr: getattr(b, a))
                            if callable(v):
                                v = v()
                        except Exception:
                            continue
                        break
                    if v is not None and v is not False and v != 0:
                        hit = (i, cdbg)
                        break
                except Exception:
                    continue
            if hit:
                target_dbg, target_label = hit[1], f"CoreDebugger({hit[0]})"
            else:
                for i, cdbg in cores:
                    try:
                        if self._com_call(lambda d=cdbg: d.Connected):
                            target_dbg, target_label = cdbg, f"CoreDebugger({i})"
                            break
                    except Exception:
                        continue

        def core_overview(cdbg, label):
            item = {"core": label}
            try:
                item["state"] = _STATE_NAMES.get(int(self._com_call(cdbg.GetState)), "?")
            except Exception:
                pass
            try:
                loc = self._com_call(lambda d=cdbg: d.Location)
                item["location"] = _parse_location(loc) or (loc if isinstance(loc, str) else None)
            except Exception:
                pass
            return item

        # 概况：全部核只读 state+Location
        per_core = []
        for i, cdbg in cores:
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            if cdbg is target_dbg:
                continue
            per_core.append(core_overview(cdbg, label))

        # 栈：只读目标核；残栈（读不满 Count）稍候重试一次
        frames = []
        total = 0
        for attempt in (0, 1):
            try:
                cs = self._com_call(lambda d=target_dbg: d.CallStack)
                total = int(self._com_call(lambda c=cs: c.Count))
            except Exception:
                cs, total = None, 0
            frames = []
            if cs is not None:
                for k in range(total):
                    try:
                        frame = self._com_call(cs.Item, k)
                    except Exception:
                        try:
                            frame = self._com_call(cs.GetItem, k)
                        except Exception:
                            continue
                    entry = {"index": k}
                    for attr, key in (("FunctionName", "function"), ("Name", "function"),
                                      ("Function", "function"), ("SymbolName", "function"),
                                      ("Description", "function"),
                                      ("SourceFileName", "file"), ("FileName", "file"),
                                      ("SourceFile", "file"),
                                      ("LastLineNumber", "line"), ("LineNumber", "line"),
                                      ("Line", "line"),
                                      ("LastInstruction", "address"), ("Address", "address"),
                                      ("PC", "address")):
                        if key in entry:
                            continue
                        try:
                            v = self._com_call(lambda f=frame, a=attr: getattr(f, a))
                            if callable(v):
                                v = v()
                            if v is not None:
                                entry[key] = _jsonify(v)
                        except Exception:
                            pass
                    # CallFrame 文档只暴露寄存器访问：PC 是兜底的帧标识
                    if "address" not in entry:
                        try:
                            pc = self._com_call(frame.ReadRegister, "PC")
                            entry["address"] = _hex32(pc)
                        except Exception:
                            pass
                    frames.append(entry)
            if total and len(frames) == total:
                break
            if attempt == 0:
                time.sleep(0.8)  # 等 UDE 把栈回溯完再读一遍

        result = {"core": target_label, "frames": frames,
                  "frames_total": total, "frames_read": len(frames),
                  "cores": per_core}
        try:
            loc = self._com_call(lambda d=target_dbg: d.Location)
            result["location"] = _parse_location(loc) or (loc if isinstance(loc, str) else None)
        except Exception:
            pass
        # 符号反查（第二阶段，仅目标核）
        sym_ok = bool(self._com_call(self._program_file_of, target_dbg) or "")
        if sym_ok:
            for entry in frames:
                if entry.get("address") and "function" not in entry:
                    try:
                        name = self._function_at(target_dbg, _parse_addr(entry["address"]))
                    except Exception:
                        break
                    if name:
                        entry["function"] = name
        return result

    def message_log(self, params):
        if self.workspace is None:
            raise BackendError(proto.ERR_NO_SESSION, "no UDE session")
        log = self._com_call(lambda: self.workspace.MessageLog)
        limit = int(params.get("limit") or 50)
        entries = []
        try:
            count = int(self._com_call(lambda: log.Count))
        except Exception:
            count = 0
        start = max(0, count - limit) if limit > 0 else 0
        for i in range(start, count):
            try:
                e = self._com_call(log.GetEntry, i)
                item = {}
                for attr, key in (("Kind", "kind"), ("Type", "kind"), ("Message", "message"),
                                  ("Msg", "message"), ("Source", "source"), ("TimeStamp", "time")):
                    try:
                        item[key] = self._com_call(lambda ent=e, a=attr: getattr(ent, a))
                    except Exception:
                        pass
                entries.append(item)
            except Exception:
                entries.append({"index": i, "message": "<unreadable entry>"})
        return {"entries": entries, "total": count}

    # ---- 运行控制 ----
    def go(self, params):
        # 多核语义：逐核 Go。断点命中是全体核 co-stop，只 Go 活动核
        # 释放不了（状态停在 halted 且无报错）。
        # 实测（2026-10-13，UI 与 COM 一致）：断点命中/清点后直接 Go 可能
        # 跑不起来（或短暂跑动又立刻 break）；先 ResetTarget 再 Go 必定能跑。
        # 传 reset_first=True 走该恢复路径（会复位目标，慎用）。
        dbg_active = self._check_target()
        if params.get("reset_first") or params.get("reset"):
            self._com_call(dbg_active.ResetTarget)
            time.sleep(0.3)
        per_core = {}
        for i, cdbg in self._iter_core_debuggers():
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            try:
                self._com_call(cdbg.Go)
                per_core[label] = "ok"
            except Exception as e:
                per_core[label] = f"error: {e}"
        if not per_core:
            self._com_call(dbg_active.Go)
        state = self.target_state(params)
        state["per_core_go"] = per_core
        return state

    def do_break(self, params):
        dbg_active = self._check_target()
        per_core = {}
        for i, cdbg in self._iter_core_debuggers():
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            try:
                self._com_call(cdbg.Break)
                per_core[label] = "ok"
            except Exception as e:
                per_core[label] = f"error: {e}"
        if not per_core:
            self._com_call(dbg_active.Break)
        state = self.target_state(params)
        state["per_core_break"] = per_core
        return state

    def step_in(self, params):
        dbg = self._check_target()
        self._com_call(dbg.StepIn)
        return self.target_state(params)

    def step_over(self, params):
        dbg = self._check_target()
        self._com_call(dbg.StepOver)
        return self.target_state(params)

    def step_out(self, params):
        dbg = self._check_target()
        self._com_call(dbg.StepOut)
        return self.target_state(params)

    def reset(self, params):
        dbg = self._check_target()
        self._com_call(dbg.ResetTarget)
        return self.target_state(params)

    def wait_halt(self, params):
        dbg = self._check_target()
        timeout = int(params.get("timeout_ms") or 5000)
        halted = bool(self._com_call(dbg.WaitForHalt, timeout))
        state = self.target_state(params)
        state["halted"] = halted
        return state

    def target_state(self, params):
        if self.workspace is None:
            raise BackendError(proto.ERR_NO_SESSION, "no UDE session")
        dbg = self._get_debugger()
        state_code = int(self._com_call(dbg.GetState))
        try:
            connected = bool(self._com_call(lambda: dbg.Connected))
        except Exception:
            connected = state_code != -1
        return {
            "state": _STATE_NAMES.get(state_code, str(state_code)),
            "state_code": state_code,
            "connected": connected,
        }

    def connect_target(self, params):
        dbg = self._get_debugger()
        # VARIANT_BOOL 显式封送：Python bool 是 int 子类，动态 dispatch 可能
        # 按 VT_I4 发过去 → DISP_E_TYPEMISMATCH
        try:
            import pythoncom
            from win32com.client import VARIANT
            self._com_call(dbg.SetConnectState, VARIANT(pythoncom.VARIANT_BOOL, True))
        except Exception:
            self._com_call(dbg.SetConnectState, True)
        # 等连接就绪，最多 10s
        deadline = time.time() + float(params.get("timeout_ms", 10000)) / 1000.0
        state = self.target_state(params)
        while not state["connected"] and time.time() < deadline:
            time.sleep(0.3)
            state = self.target_state(params)
        return state

    def disconnect_target(self, params):
        dbg = self._get_debugger()
        self._com_call(dbg.SetConnectState, False)
        return self.target_state(params)

    # ---- 变量 / 寄存器 / 内存 ----
    def read_variable(self, params):
        expr = params.get("expr")
        if not expr:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: expr")
        dbg = self._check_target()
        attempts = []

        def try_read(label, fn):
            """每种 COM 入口都试一遍；None/异常记入 attempts 后继续。"""
            try:
                value = fn()
            except Exception as e:
                attempts.append({"method": label, "ok": False, "error": str(e)})
                return None
            attempts.append({"method": label, "ok": value is not None,
                             "value": _jsonify(value), "type": type(value).__name__})
            return value

        # 活动核：ReadVariable 方法 + Variable 属性的 Python 入口（GetVariable）
        value = try_read("ReadVariable(expr)", lambda: self._com_call(dbg.ReadVariable, expr))
        if value is None:
            value = try_read("GetVariable(expr)", lambda: self._com_call(dbg.GetVariable, expr))
        if value is None:
            value = try_read("Variable(expr)", lambda: self._com_call(dbg.Variable, expr))
        if value is None:
            def via_expression():
                eobj = self._get_expression(dbg, expr)
                last = None
                for attr in ("Value", "ValueString"):
                    try:
                        v = self._prop(eobj, attr)
                    except Exception as e:
                        last = e
                        continue
                    if v is not None:
                        return v
                raise RuntimeError(f"expression Value/ValueString empty ({last})")
            value = try_read("Expression(expr).Value/ValueString", via_expression)

        # 逐核回退：符号可能挂在别的 CoreDebugger 上
        if value is None:
            for i, cdbg in self._iter_core_debuggers():
                value = try_read(f"CoreDebugger({i}).ReadVariable(expr)",
                                 lambda d=cdbg: self._com_call(d.ReadVariable, expr))
                if value is None:
                    value = try_read(f"CoreDebugger({i}).GetVariable(expr)",
                                     lambda d=cdbg: self._com_call(d.GetVariable, expr))
                if value is None:
                    def via_expr_core(d=cdbg):
                        eobj = self._get_expression(d, expr)
                        for attr in ("Value", "ValueString"):
                            try:
                                v = self._prop(eobj, attr)
                            except Exception:
                                continue
                            if v is not None:
                                return v
                        raise RuntimeError("expression Value/ValueString empty")
                    value = try_read(f"CoreDebugger({i}).Expression(expr).Value",
                                     via_expr_core)
                if value is not None:
                    break

        # 最后兜底：符号 → 地址 → 按 32 位读内存（丢类型，但能出数）
        if value is None:
            addr_s = self._resolve_symbol_address(dbg, expr)
            if addr_s:
                def via_memory():
                    addr = _parse_addr(addr_s)
                    return self._com_call(dbg.ReadMemory32, addr)
                value = try_read(f"ReadMemory32({addr_s})", via_memory)
                if value is not None:
                    attempts.append({"method": "resolve", "ok": True, "address": addr_s,
                                     "note": "value read as raw 32-bit memory"})

        if value is None:
            return {"expr": expr, "value": None, "type": "NoneType", "ok": False,
                    "attempts": attempts,
                    "hint": "all COM surfaces returned no value; if cores[].program_file "
                            "is empty in status, load symbols first "
                            "(ude_load_program with flash=False)"}
        result = {"expr": expr, "value": _jsonify(value), "type": type(value).__name__,
                  "ok": True, "attempts": attempts}
        # ReadVariable 对某些符号返回的是**地址**而不是值（实测 g_CoreCounter：
        # 返回 0x700298B8 == 符号地址，真值在该地址的内存里）。附上地址与
        # mem32 对照；若 value 恰等于地址，把 value 换成内存里的真值。
        try:
            addr_s = self._resolve_symbol_address(dbg, expr)
            if addr_s:
                result["address"] = addr_s
                try:
                    mem32 = _jsonify(self._com_call(dbg.ReadMemory32, _parse_addr(addr_s)))
                    result["mem32"] = mem32
                    try:
                        same = int(value) == _parse_addr(addr_s)
                    except (TypeError, ValueError):
                        same = False
                    if same and mem32 is not None:
                        result["com_return"] = _jsonify(value)
                        result["value"] = mem32
                        result["value_source"] = f"ReadMemory32({addr_s})"
                except Exception:
                    pass
        except Exception:
            pass
        return result

    def write_variable(self, params):
        expr = params.get("expr")
        if expr is None or "value" not in params:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing params: expr/value")
        dbg = self._check_target()
        new_val = params["value"]
        attempts = []

        def readback():
            # ReadVariable 对部分符号只回地址（恒定不变），拿它验写等于没验。
            # 有符号地址就用该地址的内存当回读真值。
            try:
                addr_s = self._resolve_symbol_address(dbg, expr)
                if addr_s:
                    return _jsonify(self._com_call(dbg.ReadMemory32, _parse_addr(addr_s)))
            except Exception:
                pass
            try:
                return _jsonify(self._com_call(dbg.ReadVariable, expr))
            except Exception:
                return None

        def val_eq(a, b):
            if a is None or b is None:
                return False
            try:
                return int(a) == int(b)
            except (TypeError, ValueError):
                return str(a) == str(b)

        def try_write(label, fn):
            """执行写入口后必须回读验证——WriteVariable 会返回 True 但值不变。"""
            try:
                ok = fn()
            except Exception as e:
                attempts.append({"method": label, "ok": False, "error": str(e)})
                return False
            got = readback()
            verified = val_eq(got, new_val)
            attempts.append({"method": label, "ok": bool(ok), "verified": verified,
                             "readback": got})
            return verified

        if try_write("WriteVariable(expr,value)",
                     lambda: self._com_call(dbg.WriteVariable, expr, new_val)):
            return {"expr": expr, "value": new_val, "method": "WriteVariable",
                    "verified": True, "attempts": attempts}
        if try_write("SetVariable(expr,value)",
                     lambda: self._com_call(dbg.SetVariable, expr, new_val)):
            return {"expr": expr, "value": new_val, "method": "SetVariable",
                    "verified": True, "attempts": attempts}

        def via_expr_put():
            eobj = self._get_expression(dbg, expr)
            try:
                setattr(eobj, "Value", new_val)
                return True
            except Exception:
                pass
            import pythoncom
            from win32com.client import DISPATCH_PROPERTYPUT
            ole = eobj._oleobj_
            dispid = ole.GetIDsOfNames("Value")
            if isinstance(dispid, (list, tuple)):
                dispid = dispid[0]
            ole.Invoke(dispid, 0, DISPATCH_PROPERTYPUT, new_val)
            return True

        if try_write("Expression.Value put", via_expr_put):
            return {"expr": expr, "value": new_val, "method": "Expression.Value",
                    "verified": True, "attempts": attempts}

        # 兜底：符号地址 + WriteMemory32（用 ReadMemory32 验证）
        addr_s = self._resolve_symbol_address(dbg, expr)
        if addr_s:
            try:
                addr = _parse_addr(addr_s)
                self._com_call(dbg.WriteMemory32, addr, _parse_int(new_val))
                got = _jsonify(self._com_call(dbg.ReadMemory32, addr))
                verified = val_eq(got, new_val)
                attempts.append({"method": f"WriteMemory32({addr_s})", "ok": True,
                                 "verified": verified, "mem32": got})
                if verified:
                    return {"expr": expr, "value": new_val,
                            "method": f"WriteMemory32({addr_s})",
                            "verified": True, "attempts": attempts}
            except Exception as e:
                attempts.append({"method": f"WriteMemory32({addr_s})",
                                 "ok": False, "error": str(e)})

        raise BackendError(proto.ERR_BACKEND,
                           f"failed to write variable {expr!r}: no method verified. "
                           + json.dumps(attempts, ensure_ascii=False))

    def read_register(self, params):
        name = params.get("name")
        if not name:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: name")
        dbg = self._check_target()
        if str(name).upper() in ("PC", "IP"):
            # PC 优先走 Location 属性（BSTR get/set，宏样例里就是指令指针）。
            # Location 常是符号串 "'Func {path} .line+';file;..."，顺带解析出
            # 函数名/源文件/行号。ReadRegister("PC") 在部分状态读回 0；空
            # callstack 上取 Item(0) 曾把 UDE 主线程卡死，兜底必须先查 Count。
            value = None
            source = "Location"
            loc_info = {}
            try:
                loc = self._com_call(lambda: dbg.Location)
                loc_info = _parse_location(loc)
                if isinstance(loc, str):
                    try:
                        value = int(loc, 0)
                    except ValueError:
                        value = None
                elif loc is not None:
                    value = int(loc)
            except Exception:
                value = None
            if not value:
                try:
                    value = self._com_call(dbg.ReadRegister, name)
                    source = "ReadRegister"
                except Exception:
                    value = None
            if not value:
                try:
                    cs = self._com_call(lambda: dbg.CallStack)
                    cnt = int(self._com_call(lambda: cs.Count))
                    if cnt > 0:
                        frame = self._com_call(cs.Item, 0)
                        pc = self._com_call(frame.ReadRegister, "PC")
                        if pc is not None and int(pc) != 0:
                            value = pc
                            source = "CallStack.frame0"
                except Exception:
                    pass
            if value is None:
                value = 0
                source = "unavailable"
        else:
            loc_info = {}
            value = self._com_call(dbg.ReadRegister, name)
            source = "ReadRegister"
        result = {"name": name, "value": _jsonify(value), "hex": _hex32(value), "source": source}
        result.update(loc_info)
        return result

    def write_register(self, params):
        name = params.get("name")
        if name is None or "value" not in params:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing params: name/value")
        dbg = self._check_target()
        ok = bool(self._com_call(dbg.WriteRegister, name, _parse_int(params["value"])))
        if not ok:
            raise BackendError(proto.ERR_BACKEND, f"failed to write register: {name}")
        return {"name": name, "value": params["value"]}

    def _mem_fn(self, dbg, width, kind):
        attr = f"{kind}Memory{width}"
        fn = getattr(dbg, attr, None)
        if fn is None:
            raise BackendError(proto.ERR_BAD_REQUEST, f"no such memory op: {attr}")
        return fn

    def read_memory(self, params):
        addr = _parse_addr(params.get("addr"))
        width = int(params.get("width") or 8)
        if width not in (8, 16, 32):
            raise BackendError(proto.ERR_BAD_REQUEST, "width must be 8, 16 or 32")
        size = params.get("size")
        count = 1 if size is None else _parse_int(size)
        if count <= 0 or count > 65536:
            raise BackendError(proto.ERR_BAD_REQUEST, "size must be 1..65536 units")
        dbg = self._check_target()
        read_fn = self._mem_fn(dbg, width, "Read")
        unit = width // 8
        data = []
        for i in range(count):
            value = self._com_call(read_fn, addr + i * unit)
            if isinstance(value, int):
                value &= (1 << width) - 1
            data.append(_jsonify(value))
        return {"addr": addr, "addr_hex": hex(addr), "width": width, "count": count, "data": data}

    def write_memory(self, params):
        addr = _parse_addr(params.get("addr"))
        width = int(params.get("width") or 8)
        if width not in (8, 16, 32):
            raise BackendError(proto.ERR_BAD_REQUEST, "width must be 8, 16 or 32")
        values = params.get("value")
        if not isinstance(values, list):
            values = [values]
        if not values:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: value")
        dbg = self._check_target()
        write_fn = self._mem_fn(dbg, width, "Write")
        unit = width // 8
        for i, v in enumerate(values):
            ok = bool(self._com_call(write_fn, addr + i * unit, _parse_int(v)))
            if not ok:
                raise BackendError(proto.ERR_BACKEND,
                                   f"failed to write memory at {hex(addr + i * unit)}")
        return {"addr": addr, "addr_hex": hex(addr), "width": width, "count": len(values)}

    # ---- 断点 ----
    def _bp_attempt(self, dbg, location, method, attempts, label):
        """在一个 Breakpoints 集合上加断点。method: 'add_result' | 'add'。

        AddAndGetResult 失败返回结果码（不抛异常），故以 Count 是否增长
        或结果码 0 判定成功；Add 抛异常即失败。
        """
        try:
            bps = self._com_call(lambda d=dbg: d.Breakpoints)
            try:
                before = int(self._com_call(lambda b=bps: b.Count))
            except Exception:
                before = None
            if method == "add_result":
                rc = self._com_call(bps.AddAndGetResult, location)
            else:
                self._com_call(bps.Add, location)
                rc = None
            try:
                after = int(self._com_call(lambda b=bps: b.Count))
            except Exception:
                after = None
            grew = before is not None and after is not None and after > before
            if method == "add_result":
                ok = grew or rc == 0
            else:
                ok = True  # Add 抛异常即失败，能走到这里就是成功
            attempts.append({"method": f"{label}.{method}({location!r})", "ok": ok,
                             "result_code": _jsonify(rc),
                             "count_before": before, "count_after": after})
            return ok
        except Exception as e:
            attempts.append({"method": f"{label}.{method}({location!r})",
                             "ok": False, "error": str(e)})
            return False

    def bp_add(self, params):
        location = params.get("location")
        if not location:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: location")
        dbg = self._check_target()
        attempts = []
        # 符号形式解析出的地址形式（"0x..."），作为符号解析失败时的回退
        addr_s = None
        if not location.lower().startswith("0x"):
            addr_s = self._resolve_symbol_address(dbg, location)
        locs = [location] + ([addr_s] if addr_s and addr_s != location else [])

        # 多核语义（对应 UI 断点对话框的 MultiCore 勾选）：每个核的
        # Breakpoints 集合都加一遍，断点才会在真正执行它的核上命中
        cores = list(self._iter_core_debuggers())
        if not cores:
            cores = [("active", dbg)]
        added_to = []
        for i, cdbg in cores:
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            for loc in locs:
                if self._bp_attempt(cdbg, loc, "add_result", attempts, label):
                    added_to.append(label)
                    break
                if self._bp_attempt(cdbg, loc, "add", attempts, label):
                    added_to.append(label)
                    break
        if not added_to:
            return {"location": location, "ok": False, "attempts": attempts,
                    "symbol_address": addr_s,
                    "hint": "all add attempts failed; check UDE for a modal dialog "
                            "and cores[].program_file in status (symbols?)"}
        self._local_breakpoints.append(location)
        return {"location": location, "ok": True, "added_to": added_to,
                "symbol_address": addr_s,
                "count": len(self._local_breakpoints), "attempts": attempts}

    def bp_remove(self, params):
        location = params.get("location")
        if not location:
            raise BackendError(proto.ERR_BAD_REQUEST, "missing param: location")
        # 与 bp_add 对称：每个核的 Breakpoints 集合都删一遍
        errors = []
        removed_from = []
        cores = list(self._iter_core_debuggers())
        if not cores:
            cores = [("active", self._get_debugger())]
        for i, cdbg in cores:
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            try:
                bps = self._com_call(lambda d=cdbg: d.Breakpoints)
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            try:
                self._com_call(bps.Remove, location)
                removed_from.append(label)
            except Exception:
                try:
                    self._com_call(bps.RemoveAndGetResult, location)
                    removed_from.append(label)
                except Exception as e2:
                    errors.append(f"{label}: {e2}")
        self._local_breakpoints = [loc for loc in self._local_breakpoints if loc != location]
        return {"location": location, "removed": bool(removed_from),
                "removed_from": removed_from, "errors": errors,
                "count": len(self._local_breakpoints)}

    def bp_clear(self, params):
        # 与 bp_add 对称：每个核的 Breakpoints 集合都清一遍（否则其它核上残留）
        cleared = []
        errors = []
        cores = list(self._iter_core_debuggers())
        if not cores:
            cores = [("active", self._get_debugger())]
        for i, cdbg in cores:
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            try:
                bps = self._com_call(lambda d=cdbg: d.Breakpoints)
                self._com_call(bps.RemoveAll)
                cleared.append(label)
            except Exception as e:
                errors.append(f"{label}: {e}")
        n = len(self._local_breakpoints)
        self._local_breakpoints = []
        return {"removed": n, "cleared_cores": cleared, "errors": errors}

    def bp_list(self, params):
        dbg = self._get_debugger()
        entries = []
        try:
            bps = self._com_call(lambda: dbg.Breakpoints)
            count = int(self._com_call(lambda: bps.Count))
            for i in range(count):
                try:
                    item = self._com_call(bps.Item, i)
                    entries.append(_describe_bp(item, i))
                except Exception as e:
                    entries.append({"index": i, "error": str(e)})
            note = "enumerated from UDE Breakpoints collection"
        except Exception as e:
            note = f"UDE enumeration failed ({e}); bridge-local list"
            entries = [{"location": loc} for loc in self._local_breakpoints]
        # 各核概况：MultiCore 语义下断点在多个集合里各有一份
        per_core = {}
        for i, cdbg in self._iter_core_debuggers():
            label = f"CoreDebugger({i})" if isinstance(i, int) else str(i)
            try:
                bps = self._com_call(lambda d=cdbg: d.Breakpoints)
                per_core[label] = int(self._com_call(lambda b=bps: b.Count))
            except Exception as e:
                per_core[label] = f"err {e}"
        return {"breakpoints": entries, "count": len(entries), "note": note,
                "per_core": per_core, "local": list(self._local_breakpoints)}


def _parse_location(loc):
    """解析 UDE Location 串，例：
    "'Core0_main {D:\\...\\main.c} .37+';main.c;1;0;0x0;"
    → {"function": "Core0_main", "source_path": "D:\\...\\main.c",
       "line": 37, "file": "main.c"}
    """
    info = {}
    if not isinstance(loc, str) or not loc:
        return info
    parts = loc.split(";")
    head = parts[0].strip().strip("'")
    m = re.match(r"([^\s{]+)\s*\{(.*)\}\s*\.(\d+)", head)
    if m:
        info["function"] = m.group(1)
        info["source_path"] = m.group(2)
        info["line"] = int(m.group(3))
    else:
        # 短格式：'I2c_SyncWrite+0xD0';I2c.c;  → function/offset/file
        m = re.match(r"([^\s{+]+)(?:\+(0x[0-9A-Fa-f]+|\d+))?", head)
        if m and m.group(1):
            info["function"] = m.group(1)
            if m.group(2):
                info["offset"] = m.group(2)
    if len(parts) > 1 and parts[1]:
        info["file"] = parts[1]
    return info


def _hex32(value):
    """寄存器/地址值按无符号十六进制显示（COM 可能返回 int32 补码）。"""
    if not isinstance(value, int):
        return None
    return hex(value & 0xFFFFFFFF)


def _describe_bp(item, index):
    """Breakpoints.Item(i) 的条目 → 可读 dict（对象取属性，标量直接记）。"""
    if item is None:
        return {"index": index, "value": None}
    if isinstance(item, (bool, int, float, str)):
        return {"index": index, "value": _jsonify(item)}
    info = {"index": index}
    for attr in ("Location", "Description", "Name", "Enabled", "Address"):
        try:
            v = getattr(item, attr)
            if callable(v):
                v = v()
            info[attr.lower()] = _jsonify(v)
        except Exception:
            pass
    if "location" not in info:
        try:
            info["str"] = str(item)
        except Exception:
            pass
    return info


def _jsonify(value):
    """COM VARIANT → 可 JSON 化的值。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def create_backend(config: dict = None):
    config = config or {}
    kind = (config.get("backend") or "mock").lower()
    if kind == "mock":
        return MockUdeBackend()
    if kind == "real":
        return RealUdeBackend(config)
    raise BackendError(proto.ERR_BAD_REQUEST, f"unknown backend: {kind}")
