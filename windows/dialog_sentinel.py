r"""UDE 弹框哨兵：自动处理模态对话框（源文件定位框、Memtool/OTP 弹框）。

背景：模态框会阻塞 UDE 的 COM 调用，把 bridge 的 BackendWorker 整条队列卡死。
两类典型弹框：

1. 源文件定位框——UDE 命中断点打开源码窗口时，按 DWARF 里的编译路径找不到
   .c 文件就弹框等用户指定。哨兵在 source_roots 里找到同名文件后自动填路径
   确认（或直接取消）。
2. 烧写类弹框——Memtool 下载后的确认框、OTP Memory Programming tool 等。
   正常情况下 _load_memtool 用 ScriptIsRunning=True 预防；万一还是弹了，
   哨兵按 dialog_blockers 文本特征识别并直接关掉（宁可中止操作也不误确认）。

config.json:
  "dialog_sentinel": true,          # 总开关（缺省 true，非 Windows 自动关闭）
  "dialog_action": "auto_fill",     # auto_fill：查到源文件就填路径确认
                                    # cancel：直接关掉（不显示源码，但不挡调试）
  "source_roots": ["D:\\proj"]      # auto_fill 的源文件搜索根目录
  "dialog_blockers": ["OTP Memory Programming", "Memory Programming"]
                                    # 文本命中即关的烧写类弹框（子串匹配，可改）
"""

import os
import re
import sys
import threading

# 严格匹配：文本里必须出现明确的 C/C++ 源文件名才动手，
# 避免误关其它软件（浏览器/编辑器）的通用文件对话框
_FILE_RE = re.compile(r"([\w\- ]+\.(?:c|h|cpp|hpp|cc|cxx))\b", re.IGNORECASE)
_SKIP_DIRS = {".git", "_bin", "_out", "node_modules", "__pycache__",
              "Debug", "Release", ".vs"}

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
IDOK = 1

# 烧写类弹框：文本命中即关（下载链路的正解在 _load_memtool 的 ScriptIsRunning，
# 这里只是兜底；关掉=中止该弹框对应的操作，不会误确认烧写）
DEFAULT_BLOCKERS = ["OTP Memory Programming", "Memory Programming"]


class SourceIndex:
    """basename(小写) → 源文件完整路径。首次查询时惰性扫描。"""

    def __init__(self, roots):
        self.roots = list(roots or [])
        self._map = None
        self._lock = threading.Lock()

    def add_root(self, root):
        """追加搜索根（如加载 ELF 时顺带登记它所在目录），幂等。"""
        if not root:
            return
        root = os.path.abspath(root)
        with self._lock:
            if root in self.roots:
                return
            self.roots.append(root)
            if self._map is not None:
                self._map.update(self._scan(root))

    def find(self, basename):
        with self._lock:
            if self._map is None:
                self._map = self._build()
        return self._map.get(basename.lower())

    @staticmethod
    def _scan(root):
        mapping = {}
        if not os.path.isdir(root):
            return mapping
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.lower().endswith((".c", ".h", ".cpp", ".hpp", ".cc", ".cxx")):
                    mapping.setdefault(fn.lower(), os.path.join(dirpath, fn))
        return mapping

    def _build(self):
        mapping = {}
        for root in self.roots:
            mapping.update(self._scan(root))
        return mapping

    def _build(self):
        mapping = {}
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for fn in filenames:
                    if fn.lower().endswith((".c", ".h", ".cpp", ".hpp", ".cc", ".cxx")):
                        mapping.setdefault(fn.lower(), os.path.join(dirpath, fn))
        return mapping


class DialogSentinel:
    def __init__(self, config):
        config = config or {}
        self.enabled = bool(config.get("dialog_sentinel", True)) and os.name == "nt"
        self.action = (config.get("dialog_action") or "auto_fill").lower()
        if self.action not in ("auto_fill", "cancel"):
            self.action = "auto_fill"
        self.index = SourceIndex(config.get("source_roots") or [])
        blockers = config.get("dialog_blockers")
        if blockers is None:
            blockers = DEFAULT_BLOCKERS
        self.blockers = [str(b).lower() for b in blockers if str(b).strip()]
        self._handled = {}          # (hwnd, filename) -> ts（句柄会复用，满了清空）
        self._stop = threading.Event()
        self._thread = None

    def add_root(self, root):
        """登记源文件搜索根（backend 加载程序后把 ELF 所在目录送进来）。"""
        try:
            self.index.add_root(root)
        except Exception:
            pass

    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ude-dialog-sentinel")
        self._thread.start()

    # ---- 轮询 ----

    def _loop(self):
        while not self._stop.wait(0.4):
            try:
                self._scan_once()
            except Exception:
                pass  # 哨兵绝不能影响主服务

    def _scan_once(self):
        import win32gui

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.GetClassName(hwnd) != "#32770":
                    return
                texts = self._collect_texts(hwnd)
                # 1) 烧写类弹框：文本特征命中直接关（在源文件逻辑之前）
                low = (texts or "").lower()
                for pat in self.blockers:
                    if pat not in low:
                        continue
                    key = (hwnd, f"blocker:{pat}")
                    if key in self._handled:
                        return
                    if len(self._handled) > 512:
                        self._handled.clear()
                    self._handled[key] = True
                    self._close(hwnd, f"blocker:{pat}", "closed (flash blocker)")
                    return
                # 2) 源文件定位框
                fname = self._extract_filename(texts)
                if not fname:
                    return
                key = (hwnd, fname.lower())
                if key in self._handled:
                    return
                if len(self._handled) > 512:
                    self._handled.clear()
                self._handled[key] = True
                self._handle_dialog(hwnd, fname)
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)

    @staticmethod
    def _collect_texts(hwnd):
        import win32gui
        parts = [win32gui.GetWindowText(hwnd) or ""]

        def child_cb(child, _):
            try:
                t = win32gui.GetWindowText(child)
                if t:
                    parts.append(t)
            except Exception:
                pass

        win32gui.EnumChildWindows(hwnd, child_cb, None)
        return "\n".join(parts)

    @staticmethod
    def _extract_filename(texts):
        m = _FILE_RE.search(texts or "")
        return m.group(1).strip() if m else None

    # ---- 处理 ----

    def _handle_dialog(self, hwnd, fname):
        if self.action == "cancel":
            self._close(hwnd, fname, "closed (cancel mode)")
            return
        path = self.index.find(fname)
        if not path:
            self._close(hwnd, fname, f"no local copy of {fname} under source_roots; closed")
            return
        import win32gui

        edit = None

        def find_edit(child, _):
            nonlocal edit
            if edit is None and win32gui.GetClassName(child) == "Edit":
                edit = child

        win32gui.EnumChildWindows(hwnd, find_edit, None)
        if edit is None:
            self._close(hwnd, fname, f"found {path} but no Edit control; closed")
            return
        win32gui.SetWindowText(edit, path)
        win32gui.SendMessage(hwnd, WM_COMMAND, IDOK, 0)
        self._log(f"auto-filled {fname} -> {path}")

    def _close(self, hwnd, fname, why):
        try:
            import win32gui
            win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        self._log(f"dialog [{fname}]: {why}")

    @staticmethod
    def _log(msg):
        print(f"[dialog-sentinel] {msg}", file=sys.stderr, flush=True)
