"""纯 Windows 部署验证：用 Windows Python 跑（不经过 WSL）。

用法: python D:\\ude-agent\\test_pure_win.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_client import BridgeClient

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"PASS  {name}  {detail}")
    else:
        fail += 1
        print(f"FAIL  {name}  {detail}")


print("== 1. stdio + mock（默认模式应为 stdio）==")
os.environ["UDE_BRIDGE_MODE"] = "stdio"
os.environ["UDE_BACKEND"] = "mock"
c = BridgeClient()
check("default_mode_is_stdio", c.mode == "stdio", c.mode)
check("ping_mock", (c.ping() or {}).get("backend") == "mock", str(c.ping()))
r = c.request("start_session", {"wsx": r"D:\x.wsx"})
v = c.request("read_variable", {"expr": "g_counter"})
check("mock_roundtrip", v.get("value") == 42, str(v))
c.close()

print("== 2. 不传 --backend → 应由 windows/config.json 决定（real）==")
os.environ.pop("UDE_BACKEND", None)
c = BridgeClient()
p = c.ping()
check("backend_from_config_json", (p or {}).get("backend") == "real", str(p))
check("real_probe_ude_exe", "UDEVisualPlatform" in str((p or {}).get("ude_exe", "")),
      str((p or {}).get("ude_exe")))
c.close()

print("== 3. pipe 模式：win32file 直连命名管道（无 npiperelay）==")
os.environ["UDE_BRIDGE_MODE"] = "pipe"
pipe_name = r"\\.\pipe\ude_bridge_test"
srv = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "windows", "ude_bridge_server.py"),
     "--pipe", pipe_name, "--backend", "mock"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.0)
try:
    c = BridgeClient({"pipe": pipe_name})
    check("win_pipe_connect", True, c.request("ping") and "pong")
    v = c.request("read_variable", {"expr": "g_counter"}) \
        if c.request("start_session", {"wsx": r"D:\x.wsx"}) is not None else None
    check("win_pipe_roundtrip", (v or {}).get("value") == 42, str(v))
    c.close()
finally:
    srv.kill()

print(f"\n===== {ok}/{ok + fail} PASS =====")
sys.exit(1 if fail else 0)
