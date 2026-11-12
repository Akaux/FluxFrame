#!/usr/bin/env python3
"""
Adapta o FluxFrame para Windows. Idempotente.
- Escreve arquivos novos: winfind_win.py, hotkeys_win.py, build_exe.py, requirements_windows.txt
- Aplica patches em overlay.py, live.py, framegen_gui.py
"""
import ast
import sys
from pathlib import Path

FILES = ("overlay.py", "live.py", "framegen_gui.py")
for f in FILES:
    if not Path(f).exists():
        sys.exit(f"ERRO: {f} nao encontrado. Rode este script na pasta do projeto.")


def replace_once(src, old, new, tag):
    if old not in src:
        raise AssertionError(f"ancora nao encontrada: {tag}")
    return src.replace(old, new, 1)


# ============================================================================
# winfind_win.py
# ============================================================================
WINFIND = r'''"""Localiza e segue a janela do app no Windows (pywin32). API igual a winfind.py."""
import os
import win32con
import win32gui
import win32process
from collections import namedtuple

MIN_W, MIN_H = 160, 120
ASPECT_TOL = 0.04
ASPECT_MAX = 0.10
SELF_WORDS = ("framegen",)

Win = namedtuple("Win", "id x y w h rank title cls active")


class WinFindError(RuntimeError):
    pass


def _text(h):
    try: return win32gui.GetWindowText(h) or ""
    except Exception: return ""


def _cls(h):
    try: return win32gui.GetClassName(h) or ""
    except Exception: return ""


def _pid(h):
    try:
        _, p = win32process.GetWindowThreadProcessId(h)
        return p
    except Exception:
        return 0


def _rect(h):
    try:
        l, t, r, b = win32gui.GetWindowRect(h)
        return (l, t, r - l, b - t)
    except Exception:
        return None


class XConn:
    def __init__(self): self.errors = 0
    def close(self): pass
    from contextlib import contextmanager
    @contextmanager
    def quiet(self): yield

    def is_active(self, h):
        try: return win32gui.GetForegroundWindow() == h
        except Exception: return False

    def candidates(self, exclude_pids=()):
        exclude = set(exclude_pids) | {os.getpid(), os.getppid()}
        active = win32gui.GetForegroundWindow()
        found = []

        def cb(h, _):
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h): return True
            r = _rect(h)
            if r is None: return True
            x, y, w, hh = r
            if w < MIN_W or hh < MIN_H: return True
            if _pid(h) in exclude: return True
            title, cls = _text(h), _cls(h)
            if not title: return True
            low = (title + " " + cls).lower()
            if any(s in low for s in SELF_WORDS): return True
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            if ex & win32con.WS_EX_TOOLWINDOW: return True
            found.append([h, x, y, w, hh, 0, title, cls, h == active])
            return True

        win32gui.EnumWindows(cb, None)
        for i, w in enumerate(found): w[5] = i
        return [Win(*w) for w in found]

    def geometry(self, h):
        if not win32gui.IsWindow(h): return "gone", None
        if win32gui.IsIconic(h) or not win32gui.IsWindowVisible(h):
            return "hidden", None
        r = _rect(h)
        return ("ok", r) if r else ("gone", None)


def pick(wins, aspect=None, text=None):
    if text:
        t = text.lower()
        wins = [w for w in wins if t in w.title.lower() or t in w.cls.lower()]
    if not wins: return None
    def err(w): return abs(w.w / w.h - aspect) / aspect if aspect else 0.0
    if aspect:
        wins = [w for w in wins if err(w) <= ASPECT_MAX]
        if not wins: return None
    good = [w for w in wins if err(w) <= ASPECT_TOL]
    if good: return min(good, key=lambda w: (not w.active, -w.rank))
    return min(wins, key=lambda w: (err(w), not w.active, -w.rank))


def find_once(aspect=None, text=None, exclude_pids=None):
    try: conn = XConn()
    except WinFindError: return None
    try: return pick(conn.candidates(exclude_pids or {os.getpid(), os.getppid()}), aspect, text)
    except Exception: return None


def default_exclude(): return {os.getpid(), os.getppid()}


class WindowTracker:
    def __init__(self, aspect=None, text=None, exclude_pids=None, log=print, retry=1.0):
        self.conn = XConn()
        self.aspect, self.text = aspect, text
        self.exclude = exclude_pids or default_exclude()
        self.log, self.retry = log, retry
        self.win = None
        self.ever_found = False
        self.geo = None
        self._next_pick = 0.0
        self._was_hidden = False

    def is_active(self):
        return self.win is not None and self.conn.is_active(self.win)

    def poll(self, now):
        geo = None
        with self.conn.quiet():
            if self.win is not None:
                state, geo = self.conn.geometry(self.win)
                if state == "gone":
                    self.log("winfind: janela alvo fechada; procurando de novo...")
                    self.win = None
                elif state == "hidden":
                    if not self._was_hidden:
                        self.log("winfind: janela alvo minimizada"); self._was_hidden = True
                    geo = None
                else:
                    if self._was_hidden:
                        self.log("winfind: janela alvo restaurada"); self._was_hidden = False
            if self.win is None and now >= self._next_pick:
                self._next_pick = now + self.retry
                w = pick(self.conn.candidates(self.exclude), self.aspect, self.text)
                if w is not None:
                    self.win = w.id
                    geo = (w.x, w.y, w.w, w.h)
                    self.log(f"winfind: janela alvo '{w.title}' [{w.cls}] {w.w}x{w.h}+{w.x}+{w.y}")
        if geo is not None: self.ever_found = True
        self.geo = geo
        return geo

    def close(self): self.conn.close()
'''

# ============================================================================
# hotkeys_win.py
# ============================================================================
HOTKEYS = r'''"""Hotkeys globais Ctrl+Alt+F/H/N via RegisterHotKey + QAbstractNativeEventFilter."""
import ctypes
import ctypes.wintypes as wt
import sys
import win32con
import win32gui
from PySide6.QtCore import QAbstractNativeEventFilter


class GlobalHotkeysWindows(QAbstractNativeEventFilter):
    ID_FG, ID_HUD, ID_QUIT = 1, 2, 3

    def __init__(self, on_fg, on_hud, on_quit, app):
        super().__init__()
        self.on_fg, self.on_hud, self.on_quit, self.app = on_fg, on_hud, on_quit, app
        self.hwnd, self.enabled = None, False

    def start(self, hwnd):
        self.hwnd = int(hwnd)
        mods = win32con.MOD_CONTROL | win32con.MOD_ALT
        pairs = [(self.ID_FG, ord("F"), self.on_fg, "Ctrl+Alt+F"),
                 (self.ID_HUD, ord("H"), self.on_hud, "Ctrl+Alt+H"),
                 (self.ID_QUIT, ord("N"), self.on_quit, "Ctrl+Alt+N")]
        for hid, vk, _, name in pairs:
            if not win32gui.RegisterHotKey(self.hwnd, hid, mods, vk):
                print(f"[hotkeys] {name} indisponivel (ja usado)", file=sys.stderr)
        self.app.installNativeEventFilter(self)
        self.enabled = True
        print("[hotkeys] Windows: Ctrl+Alt+F/H/N", file=sys.stderr)

    def stop(self):
        if not self.enabled: return
        for hid in (self.ID_FG, self.ID_HUD, self.ID_QUIT):
            try: win32gui.UnregisterHotKey(self.hwnd, hid)
            except Exception: pass
        try: self.app.removeNativeEventFilter(self)
        except Exception: pass
        self.enabled = False

    def nativeEventFilter(self, eventType, message):
        try:
            if eventType == b"windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(wt.MSG)).contents
                if msg.message == win32con.WM_HOTKEY:
                    if   msg.wParam == self.ID_FG:   self.on_fg()
                    elif msg.wParam == self.ID_HUD:  self.on_hud()
                    elif msg.wParam == self.ID_QUIT: self.on_quit()
                    return True, 0
        except Exception:
            pass
        return False, 0
'''

# ============================================================================
# build_exe.py
# ============================================================================
BUILD = r'''#!/usr/bin/env python3
"""Gera dist\\framegen.exe no Windows (PyInstaller onefile)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
BUILD = HERE / "build"

HIDDEN = [
    "PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtOpenGL", "PySide6.QtNetwork",
    "OpenGL", "OpenGL.GL", "OpenGL.GL.shaders", "OpenGL.platform.win32",
    "cv2", "numpy", "mss",
    "win32gui", "win32api", "win32con", "win32process",
    "framegen", "overlay", "dlss5", "bench", "winfind_win", "hotkeys_win",
]


def main():
    if sys.platform != "win32":
        sys.exit("Rode este script no Windows.")
    try:
        import PyInstaller  # noqa
    except ImportError:
        sys.exit("pip install pyinstaller")
    DIST.mkdir(exist_ok=True)
    BUILD.mkdir(exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "framegen",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--collect-submodules", "PySide6",
    ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for asset in ("framegen.svg", "framegen.ico"):
        p = HERE / asset
        if p.exists():
            cmd += ["--add-data", f"{p};."]
            if asset.endswith(".ico"):
                cmd += ["--icon", str(p)]
    cmd.append(str(HERE / "framegen_gui.py"))

    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"\nPronto: {DIST / 'framegen.exe'}")


if __name__ == "__main__":
    main()
'''

# ============================================================================
# requirements_windows.txt
# ============================================================================
REQS = """PySide6>=6.6
PyOpenGL>=3.1
opencv-python>=4.8
numpy>=1.24
mss>=9.0
pywin32>=306
pyinstaller>=6.0
"""


# ============================================================================
# Aplicar patches
# ============================================================================
def patch_overlay():
    src = Path("overlay.py").read_text()
    if "_IS_WIN" in src:
        print("overlay.py: ja patcheado, pulando.")
        return

    src = replace_once(
        src,
        'os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)\n'
        'os.environ["QT_QPA_PLATFORM"] = "xcb"',
        '_IS_WIN = sys.platform == "win32"\n'
        'if _IS_WIN:\n'
        '    os.environ.pop("QT_QPA_PLATFORM", None)\n'
        'else:\n'
        '    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)\n'
        '    os.environ["QT_QPA_PLATFORM"] = "xcb"',
        "os.environ block")

    src = replace_once(
        src,
        "def set_click_through(win_id):\n    try:",
        '''def set_click_through(win_id):
    if _IS_WIN:
        try:
            import win32gui, win32con
            h = int(win_id)
            ex = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
            ex |= (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
                   | win32con.WS_EX_NOACTIVATE)
            win32gui.SetWindowLong(h, win32con.GWL_EXSTYLE, ex)
            win32gui.SetLayeredWindowAttributes(h, 0, 255, win32con.LWA_ALPHA)
            win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                                  | win32con.SWP_NOACTIVATE)
            return True
        except Exception as e:
            _log(f"set_click_through (win) falhou: {e}")
            return False
    try:''',
        "set_click_through")

    src = replace_once(
        src,
        "def set_transient_for(overlay_id, parent_id):\n    try:",
        "def set_transient_for(overlay_id, parent_id):\n    if _IS_WIN:\n        return False\n    try:",
        "set_transient_for")

    src = replace_once(
        src,
        "            self.dpy = None\n\n\nclass GLView(QOpenGLWindow):",
        '''            self.dpy = None


def make_hotkeys(on_fg, on_hud, on_quit, app, view):
    if _IS_WIN:
        from hotkeys_win import GlobalHotkeysWindows
        h = GlobalHotkeysWindows(on_fg, on_hud, on_quit, app)
        h.start(int(view.winId()))
        return h
    return X11Hotkeys(on_fg=on_fg, on_hud=on_hud, on_quit=on_quit)


class GLView(QOpenGLWindow):''',
        "make_hotkeys")

    src = replace_once(
        src,
        '''    hotkeys = X11Hotkeys(
        on_fg=on_fg,
        on_hud=lambda: hud_on.__setitem__(0, not hud_on[0]),
        on_quit=app.quit
    )''',
        '''    hotkeys = make_hotkeys(
        on_fg=on_fg,
        on_hud=lambda: hud_on.__setitem__(0, not hud_on[0]),
        on_quit=app.quit,
        app=app, view=view,
    )''',
        "X11Hotkeys call")

    src = replace_once(
        src,
        "    if mask:\n        flags |= Qt.WindowType.X11BypassWindowManagerHint",
        "    if mask and not _IS_WIN:\n        flags |= Qt.WindowType.X11BypassWindowManagerHint",
        "mask flag")

    src = replace_once(
        src,
        '''    signal.signal(signal.SIGUSR1, lambda *_: on_fg())
    signal.signal(signal.SIGUSR2, lambda *_: hud_on.__setitem__(0, not hud_on[0]))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())''',
        '''    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: on_fg())
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, lambda *_: hud_on.__setitem__(0, not hud_on[0]))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: app.quit())''',
        "signals in run_overlay")

    Path("overlay.py").write_text(src)
    print("overlay.py: patcheado.")


def patch_live():
    src = Path("live.py").read_text()
    if "winfind_win" in src:
        print("live.py: ja patcheado, pulando.")
        return

    src = replace_once(
        src,
        "import winfind\nfrom bench import Bench",
        'if sys.platform == "win32":\n'
        '    import winfind_win as winfind\n'
        'else:\n'
        '    import winfind\n'
        'from bench import Bench',
        "import winfind")

    src = replace_once(
        src,
        'os.environ.setdefault("QT_QPA_PLATFORM", "xcb")\n'
        'os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")',
        'if sys.platform == "win32":\n'
        '    os.environ.pop("QT_QPA_PLATFORM", None)\n'
        'else:\n'
        '    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")\n'
        'os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")',
        "os.environ live")

    src = replace_once(
        src,
        "    def _use_portal(self):\n        if self.args.overlay:",
        '    def _use_portal(self):\n        if sys.platform == "win32":\n            return False\n        if self.args.overlay:',
        "_use_portal")

    src = replace_once(
        src,
        "        os.kill(os.getpid(), signal.SIGINT)",
        '        if sys.platform != "win32":\n            os.kill(os.getpid(), signal.SIGINT)',
        "request_quit kill")

    src = replace_once(
        src,
        "    if args.overlay and (args.headless or args.x11):",
        '    if args.overlay and (args.headless or (args.x11 and sys.platform != "win32")):',
        "overlay/x11 check")

    Path("live.py").write_text(src)
    print("live.py: patcheado.")


def patch_gui():
    src = Path("framegen_gui.py").read_text()
    if 'getattr(sys, "frozen", False) and "--core"' in src:
        print("framegen_gui.py: ja patcheado, pulando.")
        return

    src = replace_once(
        src,
        "import sys\nfrom pathlib import Path",
        '''import sys
if getattr(sys, "frozen", False) and "--core" in sys.argv:
    sys.argv = [a for a in sys.argv if a != "--core"]
    from live import main as _live_main
    _live_main()
    sys.exit(0)
from pathlib import Path''',
        "core dispatch")

    src = replace_once(
        src,
        "def find_folder(saved):\n    for c in (saved, APP_DIR, APP_DIR / \"framegen\", Path.cwd()):\n        if c and (Path(c) / \"live.py\").is_file():\n            return Path(c)\n    return None",
        '''def find_folder(saved):
    for c in (saved, APP_DIR, APP_DIR / "framegen", Path.cwd()):
        if c and (Path(c) / "live.py").is_file():
            return Path(c)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return None''',
        "find_folder")

    src = replace_once(
        src,
        '''    def start(self, folder, args):
        venv_py = folder / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        CACHE.mkdir(parents=True, exist_ok=True)
        self.logf = open(LOG, "w")
        self.pos, self.tail = 0, ""
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [py, "-u", str(folder / "live.py"), *args], cwd=folder, env=env,
            stdin=subprocess.DEVNULL, stdout=self.logf, stderr=subprocess.STDOUT,
            start_new_session=True)''',
        '''    def start(self, folder, args):
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--core", *args]
        else:
            venv_py = folder / ".venv" / "bin" / "python"
            py = str(venv_py) if venv_py.exists() else sys.executable
            cmd = [py, "-u", str(folder / "live.py"), *args]
        CACHE.mkdir(parents=True, exist_ok=True)
        self.logf = open(LOG, "w")
        self.pos, self.tail = 0, ""
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            cmd, cwd=folder, env=env,
            stdin=subprocess.DEVNULL, stdout=self.logf, stderr=subprocess.STDOUT,
            start_new_session=True)''',
        "Runner.start")

    src = replace_once(
        src,
        '''    def terminate(self):
        if self.alive():
            os.kill(self.proc.pid, signal.SIGTERM)''',
        '''    def terminate(self):
        if not self.alive():
            return
        if sys.platform == "win32":
            self.proc.terminate()
        else:
            os.kill(self.proc.pid, signal.SIGTERM)''',
        "Runner.terminate")

    src = replace_once(
        src,
        '''    def _group(self, sig):
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass''',
        '''    def _group(self, sig):
        if self.proc is None:
            return
        if sys.platform == "win32":
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True)
            except Exception:
                pass
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass''',
        "Runner._group")

    Path("framegen_gui.py").write_text(src)
    print("framegen_gui.py: patcheado.")


def main():
    Path("winfind_win.py").write_text(WINFIND)
    Path("hotkeys_win.py").write_text(HOTKEYS)
    Path("build_exe.py").write_text(BUILD)
    Path("requirements_windows.txt").write_text(REQS)
    print("Arquivos novos escritos.")

    patch_overlay()
    patch_live()
    patch_gui()

    print("\nVerificando sintaxe...")
    for f in ("overlay.py", "live.py", "framegen_gui.py", "winfind_win.py",
              "hotkeys_win.py", "build_exe.py", "dlss5.py", "framegen.py", "bench.py"):
        ast.parse(Path(f).read_text())
        print(f"  OK {f}")
    print("\nPronto para buildar: python build_exe.py")


if __name__ == "__main__":
    main()