r"""桥接客户端：管理到 ude_bridge_server 的持久连接（WSL / 纯 Windows 通用）。

模式（UDE_BRIDGE_MODE）：
  stdio —— 本地以子进程运行 windows/ude_bridge_server.py --stdio（纯 Windows 默认）
  pipe  —— 连命名管道 \\.\pipe\ude_bridge：Windows 用 win32file 直连；
           WSL/Linux 经 npiperelay.exe 桥接（WSL 默认）
"""

import atexit
import os
import subprocess
import sys
import threading
import time

import protocol as proto
from protocol import BridgeError

_ROOT = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(_ROOT, "windows", "ude_bridge_server.py")

DEFAULT_NPIPERELAY = "/mnt/d/npiperelay/npiperelay.exe"   # 仅 WSL 用
DEFAULT_PIPE = r"\\.\pipe\ude_bridge"


class _WinPipeProcess:
    """把 Windows 命名管道句柄包装成 Popen 风格接口（纯 Windows pipe 模式用）。"""

    def __init__(self, handle):
        self._h = handle
        self.stdin = self      # write/flush/close
        self.stdout = self     # readline
        self._buf = b""
        self._closed = False

    # ---- Popen 兼容 ----
    def poll(self):
        return 0 if self._closed else None

    def kill(self):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                import win32file
                win32file.CloseHandle(self._h)
            except Exception:
                pass

    # ---- stdin 侧 ----
    def write(self, data: bytes):
        import win32file
        import pywintypes
        try:
            win32file.WriteFile(self._h, data)
        except pywintypes.error as e:
            if getattr(e, "winerror", None) in (109, 233):
                raise BrokenPipeError("named pipe broken") from e
            raise

    def flush(self):
        pass

    # ---- stdout 侧 ----
    def readline(self) -> bytes:
        """读一行（含换行）；EOF/断管返回 b''。"""
        import win32file
        import pywintypes
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl + 1], self._buf[nl + 1:]
                return line
            try:
                _, chunk = win32file.ReadFile(self._h, 65536)
            except pywintypes.error as e:
                # ERROR_BROKEN_PIPE(109) / ERROR_PIPE_NOT_CONNECTED(233)
                if getattr(e, "winerror", None) in (109, 233):
                    chunk = b""
                else:
                    raise
            if not chunk:
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line
                return b""
            self._buf += chunk


def to_windows_path(path: str) -> str:
    """/mnt/c/foo -> C:\\foo；Windows 路径原样返回。"""
    if not path:
        return path
    if path.startswith("/mnt/") and len(path) > 6 and path[5].isalpha() and path[6] == "/":
        drive = path[5].upper()
        rest = path[7:].replace("/", "\\")
        return f"{drive}:\\{rest}"
    return path


class BridgeClient:
    def __init__(self, config: dict = None):
        config = config or {}
        # 默认模式：WSL 走命名管道，纯 Windows 直接本地 stdio 子进程
        default_mode = "pipe" if os.name != "nt" else "stdio"
        self.mode = (config.get("mode") or os.environ.get("UDE_BRIDGE_MODE")
                     or default_mode).lower()
        self.pipe = config.get("pipe") or os.environ.get("UDE_BRIDGE_PIPE") or DEFAULT_PIPE
        self.npiperelay = (config.get("npiperelay")
                           or os.environ.get("UDE_BRIDGE_NPIPERELAY")
                           or DEFAULT_NPIPERELAY)
        self.server_script = config.get("server_script") or _SERVER
        self.default_timeout = float(config.get("timeout") or 30.0)
        # 显式指定才覆盖服务端 windows/config.json 的 backend；否则交给服务端。
        # 非 Windows 上 stdio 子进程跑不了真实 COM，未指定时退回 mock（保持旧行为）
        self.backend = config.get("backend") or os.environ.get("UDE_BACKEND") or None
        if self.backend is None and self.mode == "stdio" and os.name != "nt":
            self.backend = "mock"
        self._proc = None
        self._lock = threading.Lock()
        self._next_id = 0
        atexit.register(self.close)

    # ---- 子进程管理 ----
    def _spawn(self):
        if self.mode == "stdio":
            cmd = [sys.executable, "-I", self.server_script, "--stdio"]
            if self.backend:          # 未显式指定时不传 --backend，由 windows/config.json 决定
                cmd += ["--backend", self.backend]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=_ROOT)
        elif self.mode == "pipe":
            if os.name == "nt":
                # 纯 Windows：直接连命名管道，不需要 npiperelay
                return self._connect_win_pipe()
            if not os.path.isfile(self.npiperelay):
                raise BridgeError(proto.ERR_COMM,
                                  f"npiperelay not found: {self.npiperelay}")
            proc = subprocess.Popen(
                [self.npiperelay, "-ep", "-s", self.pipe],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        else:
            raise BridgeError(proto.ERR_COMM, f"unknown mode: {self.mode}")
        return proc

    def _connect_win_pipe(self):
        import win32file
        import pywintypes
        try:
            handle = win32file.CreateFile(
                self.pipe,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
        except pywintypes.error as e:
            raise BridgeError(proto.ERR_COMM,
                              f"cannot connect to pipe {self.pipe}: {e} "
                              f"(is ude_bridge_server.py running?)")
        return _WinPipeProcess(handle)

    def _ensure_proc(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._proc = self._spawn()
        return self._proc

    def close(self):
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    # ---- 请求 ----
    def request(self, action: str, params: dict = None, timeout: float = None):
        """发送一个请求并等待响应。返回 result；出错抛 BridgeError。"""
        timeout = timeout if timeout is not None else self.default_timeout
        with self._lock:
            self._next_id += 1
            req_id = self._next_id
            request = proto.make_request(req_id, action, params)
            proc = self._ensure_proc()
            payload = proto.encode_line(request)
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except BrokenPipeError:
                self._close_unlocked()
                raise BridgeError(proto.ERR_COMM, "bridge connection lost (broken pipe)")

            deadline = time.monotonic() + timeout
            line = self._readline_with_deadline(proc, deadline)
            if line is None:
                # 超时：丢弃进程（残留读线程可能窃取后续响应，必须重建）
                self._close_unlocked()
                raise BridgeError(proto.ERR_TIMEOUT,
                                  f"no response in {timeout:.0f}s (action={action})")

            try:
                response = proto.decode(line)
            except Exception as e:
                raise BridgeError(proto.ERR_COMM, f"bad response JSON: {e}")

        if response.get("status") == "ok":
            return response.get("result")
        raise BridgeError(response.get("code", proto.ERR_BACKEND),
                          response.get("message", "unknown error"))

    @staticmethod
    def _readline_with_deadline(proc, deadline: float):
        """带超时的按行读。后台线程读一行放入队列，超时返回 None。"""
        result = {}

        def reader():
            try:
                result["line"] = proc.stdout.readline()
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            return None
        if "error" in result:
            raise BridgeError(proto.ERR_COMM, f"read failed: {result['error']}")
        line = result.get("line", b"")
        if not line:
            raise BridgeError(proto.ERR_COMM, "bridge closed the connection")
        return line

    # ---- 便捷方法 ----
    def ping(self):
        return self.request(proto.PING)

    def wsl_path(self, path: str) -> str:
        return to_windows_path(path)
