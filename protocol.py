"""UDE bridge 线协议：每行一个 JSON 对象，UTF-8 编码，\\n 定界。

请求:  {"id": 1, "action": "read_variable", "params": {...}}
响应:  {"id": 1, "status": "ok", "result": ...}
错误:  {"id": 1, "status": "error", "code": "not_connected", "message": "..."}
"""

import json

# 单条消息上限，防止内存被撑爆
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

# ---- action 名 ----
PING = "ping"

# 会话管理
START_SESSION = "start_session"
ATTACH_SESSION = "attach_session"
CLOSE_SESSION = "close_session"
STATUS = "status"
LOAD_PROGRAM = "load_program"
FLASH_INFO = "flash_info"
LOADER_INFO = "loader_info"
CALLSTACK = "callstack"
MESSAGE_LOG = "message_log"

# 运行控制
GO = "go"
BREAK = "break"
STEP_IN = "step_in"
STEP_OVER = "step_over"
STEP_OUT = "step_out"
RESET = "reset"
WAIT_HALT = "wait_halt"
TARGET_STATE = "target_state"
CONNECT_TARGET = "connect_target"
DISCONNECT_TARGET = "disconnect_target"

# 变量 / 内存 / 寄存器
READ_VARIABLE = "read_variable"
WRITE_VARIABLE = "write_variable"
READ_MEMORY = "read_memory"
WRITE_MEMORY = "write_memory"
READ_REGISTER = "read_register"
WRITE_REGISTER = "write_register"

# 断点
BP_ADD = "bp_add"
BP_REMOVE = "bp_remove"
BP_CLEAR = "bp_clear"
BP_LIST = "bp_list"

ALL_ACTIONS = frozenset({
    PING, START_SESSION, ATTACH_SESSION, CLOSE_SESSION, STATUS,
    LOAD_PROGRAM, FLASH_INFO, LOADER_INFO, CALLSTACK, MESSAGE_LOG,
    GO, BREAK, STEP_IN, STEP_OVER, STEP_OUT, RESET, WAIT_HALT, TARGET_STATE,
    CONNECT_TARGET, DISCONNECT_TARGET,
    READ_VARIABLE, WRITE_VARIABLE, READ_MEMORY, WRITE_MEMORY,
    READ_REGISTER, WRITE_REGISTER,
    BP_ADD, BP_REMOVE, BP_CLEAR, BP_LIST,
})

# ---- 错误码 ----
ERR_NOT_CONNECTED = "not_connected"
ERR_NO_SESSION = "no_session"
ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_BACKEND = "backend_error"
ERR_TIMEOUT = "timeout"
ERR_COMM = "comm_error"


class BridgeError(Exception):
    """远端返回的错误或本地通信失败。"""

    def __init__(self, code, message):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def encode(obj) -> bytes:
    """把请求/响应对象编码为一行 JSON 字节（不含换行符本身）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_line(obj) -> bytes:
    return encode(obj) + b"\n"


def decode(line: bytes) -> dict:
    """解码一行 JSON，返回 dict。"""
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("protocol message must be a JSON object")
    return obj


def make_request(req_id: int, action: str, params: dict = None) -> dict:
    if action not in ALL_ACTIONS:
        raise ValueError(f"unknown action: {action}")
    return {"id": req_id, "action": action, "params": params or {}}


def make_ok(req_id: int, result) -> dict:
    return {"id": req_id, "status": "ok", "result": result}


def make_error(req_id, code: str, message: str) -> dict:
    return {"id": req_id if req_id is not None else -1,
            "status": "error", "code": code, "message": message}
