r"""UDE bridge 服务端。

两种运行方式：
  python ude_bridge_server.py             # Windows 命名管道 \\.\pipe\ude_bridge
  python ude_bridge_server.py --stdio     # stdin/stdout（WSL 内可直接跑 mock 自测）

线协议见 protocol.py：每行一个 JSON 请求/响应。
"""

import argparse
import json
import os
import queue
import sys
import threading
import traceback

# 允许直接以脚本运行：把项目根目录（protocol.py 所在）加入 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_protocol():
    """按显式路径加载本项目的 protocol.py，避免被同名第三方模块遮蔽。"""
    import importlib.util
    candidates = [os.path.join(_ROOT, "protocol.py"), os.path.join(_HERE, "protocol.py")]
    required = ("MAX_MESSAGE_BYTES", "make_request", "make_ok", "make_error",
                "encode_line", "decode", "ALL_ACTIONS")
    last_err = None
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("protocol", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            last_err = f"{path}: {e}"
            continue
        missing = [a for a in required if not hasattr(mod, a)]
        if missing:
            last_err = f"{path} is missing attributes {missing} (stale file?)"
            continue
        sys.modules["protocol"] = mod   # 供 ude_backend 的 import protocol 使用
        return mod
    raise ImportError(
        "failed to load a valid protocol.py; looked in:\n  "
        + "\n  ".join(candidates)
        + (f"\nlast error: {last_err}" if last_err else "")
        + "\nhint: copy the whole project folder (protocol.py + windows/) "
          "to the same parent directory, e.g. D:\\ude-agent\\{protocol.py, windows\\}")


proto = _load_protocol()
from ude_backend import BackendError, create_backend

DEFAULT_PIPE = r"\\.\pipe\ude_bridge"


# =====================================================================
# 传输层
# =====================================================================

class LineConnection:
    """字节流上的按行读写，供 pipe / stdio 两种传输共用。"""

    def __init__(self, read_fn, write_fn, close_fn):
        self._read_fn = read_fn      # (n) -> bytes（阻塞，空串=EOF）
        self._write_fn = write_fn    # (bytes) -> None
        self._close_fn = close_fn
        self._buf = b""

    def read_line(self):
        """读一行（不含换行）。EOF 抛 ConnectionError。消息过大抛 ValueError。"""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                return line
            if len(self._buf) > proto.MAX_MESSAGE_BYTES:
                raise ValueError("message too large")
            chunk = self._read_fn(65536)
            if not chunk:
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line
                raise ConnectionError("client disconnected")
            self._buf += chunk

    def write_line(self, obj):
        self._write_fn(proto.encode_line(obj))

    def close(self):
        try:
            self._close_fn()
        except Exception:
            pass


def make_stdio_connection():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    def read_fn(n):
        # read1: 有数据即返回，不等凑满 n 字节（read(n) 会阻塞到 EOF）
        return stdin.read1(n)

    def write_fn(data):
        stdout.write(data)
        stdout.flush()

    def close_fn():
        pass

    return LineConnection(read_fn, write_fn, close_fn)


def make_pipe_connection(pipe_name: str, max_instances: int = 4):
    """创建一个命名管道实例并等待客户端连接。仅 Windows。"""
    import win32file
    import win32pipe
    import pywintypes

    # 字节流 + 阻塞模式，与 npiperelay 的 stdio 字节流对齐，用换行定界
    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        max_instances, 65536, 65536, 0, None)
    win32pipe.ConnectNamedPipe(pipe, None)

    def read_fn(n):
        try:
            _, data = win32file.ReadFile(pipe, n)
            return data
        except pywintypes.error as e:
            # ERROR_BROKEN_PIPE (109) / ERROR_PIPE_NOT_CONNECTED (233)
            if e.winerror in (109, 233):
                return b""
            raise

    def write_fn(data):
        win32file.WriteFile(pipe, data)

    def close_fn():
        try:
            win32pipe.DisconnectNamedPipe(pipe)
        except Exception:
            pass
        win32file.CloseHandle(pipe)

    return LineConnection(read_fn, write_fn, close_fn)


# =====================================================================
# 分发
# =====================================================================

def dispatch(backend, request: dict) -> dict:
    req_id = request.get("id")
    action = request.get("action")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return proto.make_error(req_id, proto.ERR_BAD_REQUEST, "params must be an object")
    if action not in proto.ALL_ACTIONS:
        return proto.make_error(req_id, proto.ERR_UNKNOWN_ACTION, f"unknown action: {action}")

    handler_names = {
        proto.PING: "ping",
        proto.START_SESSION: "start_session",
        proto.ATTACH_SESSION: "attach_session",
        proto.CLOSE_SESSION: "close_session",
        proto.STATUS: "status",
        proto.LOAD_PROGRAM: "load_program",
        proto.FLASH_INFO: "flash_info",
        proto.LOADER_INFO: "loader_info",
        proto.CALLSTACK: "callstack",
        proto.MESSAGE_LOG: "message_log",
        proto.GO: "go",
        proto.BREAK: "do_break",
        proto.STEP_IN: "step_in",
        proto.STEP_OVER: "step_over",
        proto.STEP_OUT: "step_out",
        proto.RESET: "reset",
        proto.WAIT_HALT: "wait_halt",
        proto.TARGET_STATE: "target_state",
        proto.CONNECT_TARGET: "connect_target",
        proto.DISCONNECT_TARGET: "disconnect_target",
        proto.READ_VARIABLE: "read_variable",
        proto.WRITE_VARIABLE: "write_variable",
        proto.READ_MEMORY: "read_memory",
        proto.WRITE_MEMORY: "write_memory",
        proto.READ_REGISTER: "read_register",
        proto.WRITE_REGISTER: "write_register",
        proto.BP_ADD: "bp_add",
        proto.BP_REMOVE: "bp_remove",
        proto.BP_CLEAR: "bp_clear",
        proto.BP_LIST: "bp_list",
    }
    fn = getattr(backend, handler_names[action])
    try:
        result = fn(params)
        return proto.make_ok(req_id, result)
    except BackendError as e:
        return proto.make_error(req_id, e.code, e.message)
    except Exception as e:
        traceback.print_exc()
        return proto.make_error(req_id, proto.ERR_BACKEND, f"{type(e).__name__}: {e}")


class BackendWorker:
    """把所有 backend 调用集中到单一 STA 线程执行。

    好处：COM 对象只在创建它的线程里被使用（避免跨线程封送问题），
    多个管道连接的请求被自然串行化。

    call() 带超时：某个 COM 调用卡死（如 UDE 弹模态框）时，客户端能拿到
    timeout 错误而不是永久挂起；模态框关掉后队列自动恢复消化。
    """

    def __init__(self, backend):
        self.backend = backend
        self._q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pass  # stdio+mock 模式下没有 pywin32
        while True:
            request, reply_q = self._q.get()
            try:
                reply_q.put(dispatch(self.backend, request))
            except Exception as e:
                traceback.print_exc()
                reply_q.put(proto.make_error(request.get("id"), proto.ERR_BACKEND,
                                             f"{type(e).__name__}: {e}"))

    def call(self, request: dict, timeout: float = None) -> dict:
        reply_q = queue.Queue()
        self._q.put((request, reply_q))
        try:
            return reply_q.get(timeout=timeout or 120.0)
        except queue.Empty:
            return proto.make_error(
                request.get("id"), proto.ERR_TIMEOUT,
                f"backend did not answer in {timeout or 120.0:.0f}s "
                f"(action={request.get('action')}); a UDE COM call may be stuck "
                f"on a modal dialog — check the UDE window")


def _request_timeout(action, params):
    """后端看门狗超时：烧写/启动留长，其余 90s 足够。"""
    if action in (proto.LOAD_PROGRAM, proto.START_SESSION):
        return 600.0
    if action == proto.WAIT_HALT:
        try:
            return float(params.get("timeout_ms") or 5000) / 1000.0 + 30.0
        except Exception:
            return 60.0
    return 90.0


def serve_connection(conn: LineConnection, worker: BackendWorker, tag=""):
    """单连接内串行处理多个请求，直到断开。"""
    try:
        while True:
            try:
                line = conn.read_line()
            except ConnectionError:
                break
            except ValueError as e:
                conn.write_line(proto.make_error(-1, proto.ERR_BAD_REQUEST, str(e)))
                continue
            if not line.strip():
                continue
            try:
                request = proto.decode(line)
            except Exception as e:
                conn.write_line(proto.make_error(-1, proto.ERR_BAD_REQUEST, f"bad JSON: {e}"))
                continue
            params = request.get("params") or {}
            response = worker.call(request, timeout=_request_timeout(request.get("action"), params))
            conn.write_line(response)
    finally:
        conn.close()
        if tag:
            print(f"[{tag}] connection closed", file=sys.stderr)


def _run_interruptible(target):
    """在守护线程里执行阻塞的 target，主线程轮询等待以便响应 Ctrl+C。

    win32pipe/win32file 的阻塞调用不会被 Python 信号中断，
    所以把它们放进线程，主线程用短超时等待，Ctrl+C 才能及时生效。
    """
    box = {}
    done = threading.Event()

    def worker():
        try:
            target()
        except BaseException as e:
            box["exc"] = e
        finally:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while not done.wait(0.3):
        pass
    if "exc" in box:
        raise box["exc"]


def run_stdio(backend):
    print("[stdio] ude bridge server ready", file=sys.stderr)
    worker = BackendWorker(backend)
    serve_connection(make_stdio_connection(), worker, tag="stdio")


def _serve_in_thread(conn, worker):
    try:
        serve_connection(conn, worker, tag="pipe")
    except Exception:
        traceback.print_exc()


def run_pipe(backend, pipe_name):
    worker = BackendWorker(backend)
    while True:
        try:
            print(f"[pipe] waiting for client on {pipe_name}", file=sys.stderr)
            holder = {}

            def connect():
                holder["conn"] = make_pipe_connection(pipe_name)

            _run_interruptible(connect)
            conn = holder["conn"]
            print("[pipe] client connected", file=sys.stderr)
            # 每个连接独立线程服务，多个客户端可同时在线（请求经 worker 串行进后端）
            threading.Thread(target=_serve_in_thread, args=(conn, worker),
                             daemon=True).start()
        except KeyboardInterrupt:
            raise
        except Exception:
            # 单个连接出问题不影响后续服务
            traceback.print_exc()
            print("[pipe] connection error, waiting for next client", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="UDE bridge server")
    parser.add_argument("--stdio", action="store_true", help="use stdin/stdout instead of a named pipe")
    parser.add_argument("--pipe", default=None, help="named pipe path (Windows only)")
    parser.add_argument("--backend", default=None, choices=["mock", "real"], help="override backend")
    args = parser.parse_args()

    config = {}
    cfg_path = os.path.join(_HERE, "config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[init] config.json invalid: {e}", file=sys.stderr)
            print(r'[init] hint: JSON 里反斜杠要写两个，如 "D:\\path\\ude.exe"', file=sys.stderr)
            print(r'[init] example: {"backend": "real", "pipe_name": "\\\\.\\pipe\\ude_bridge", '
                  r'"ude_exe": "D:\\Program Files (x86)\\pls\\UDE 4.10\\UDEVisualPlatform.exe"}',
                  file=sys.stderr)
            sys.exit(1)
    if args.backend:
        config["backend"] = args.backend
    if os.environ.get("UDE_EXE"):
        config["ude_exe"] = os.environ["UDE_EXE"]
    config.setdefault("backend", "mock")

    backend = create_backend(config)
    print(f"[init] backend={backend.name} protocol={proto.__file__}", file=sys.stderr)

    # 弹框哨兵：自动处理 UDE 的源文件定位框等模态对话框（防止卡死 COM 队列）
    try:
        from dialog_sentinel import DialogSentinel
        sentinel = DialogSentinel(config)
        sentinel.start()
        # backend 加载程序后把 ELF 所在目录登记为源文件搜索根
        backend.dialog_sentinel = sentinel
        if sentinel.enabled:
            print(f"[init] dialog sentinel on (action={sentinel.action}, "
                  f"source_roots={len(config.get('source_roots') or [])}, "
                  f"blockers={len(sentinel.blockers)})",
                  file=sys.stderr)
    except Exception as e:
        print(f"[init] dialog sentinel not started: {e}", file=sys.stderr)

    try:
        if args.stdio:
            run_stdio(backend)
        else:
            pipe_name = args.pipe or config.get("pipe_name") or DEFAULT_PIPE
            if os.name != "nt":
                print("named pipe mode requires Windows; use --stdio on WSL", file=sys.stderr)
                sys.exit(1)
            run_pipe(backend, pipe_name)
    except KeyboardInterrupt:
        print("\n[init] interrupted, exiting", file=sys.stderr)


if __name__ == "__main__":
    main()
