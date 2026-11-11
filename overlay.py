"""
Overlay "máscara" do framegen otimizado.
"""
import collections
import os
import queue
import signal
import time

os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ.setdefault("PYOPENGL_PLATFORM", "glx")
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ.pop("QT_SCALE_FACTOR", None)

import numpy as np
from framegen import GenRef
from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QPainter,
                           QRasterWindow, QSurfaceFormat)
from PySide6.QtOpenGL import QOpenGLWindow

VERT = """#version 330 core
out vec2 uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    uv = vec2(p.x, 1.0 - p.y);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
uniform vec2  srcSize;
uniform vec2  dstSize;
uniform float rcas;

vec3 T(ivec2 p) { return texelFetch(tex, clamp(p, ivec2(0), ivec2(srcSize) - 1), 0).rgb; }
float L(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

float lanczos2(float x) {
    x = abs(x);
    if (x < 1e-4) return 1.0;
    if (x >= 2.0) return 0.0;
    float px = 3.14159265 * x;
    return 2.0 * sin(px) * sin(px * 0.5) / (px * px);
}

vec3 easu(vec2 pos) {
    vec2 p = pos - 0.5;
    ivec2 ip = ivec2(floor(p));
    vec2 f = p - vec2(ip);
    vec3 c[16];
    float l[16];
    for (int j = 0; j < 4; j++)
        for (int i = 0; i < 4; i++) {
            c[j * 4 + i] = T(ip + ivec2(i - 1, j - 1));
            l[j * 4 + i] = L(c[j * 4 + i]);
        }
    float gx = 0.5 * (l[2] - l[1]) + (l[6] - l[5]) + (l[10] - l[9]) + 0.5 * (l[14] - l[13]);
    float gy = 0.5 * (l[8] - l[4]) + (l[9] - l[5]) + (l[10] - l[6]) + 0.5 * (l[11] - l[7]);
    vec2 g = vec2(gx, gy);
    float gl = length(g);
    vec2 n = gl > 1e-4 ? g / gl : vec2(1.0, 0.0);
    vec2 t = vec2(-n.y, n.x);
    float s = clamp(gl * 2.0, 0.0, 1.0);
    float across = 1.0 + 0.5 * s;
    float along  = 1.0 - 0.3 * s;
    vec3 sum = vec3(0.0);
    float wsum = 0.0;
    for (int j = 0; j < 4; j++)
        for (int i = 0; i < 4; i++) {
            vec2 d = vec2(float(i - 1), float(j - 1)) - f;
            float w = lanczos2(length(vec2(dot(d, n) * across, dot(d, t) * along)));
            sum += c[j * 4 + i] * w;
            wsum += w;
        }
    vec3 o = sum / max(wsum, 1e-5);
    vec3 mn = min(min(c[5], c[6]), min(c[9], c[10]));
    vec3 mx = max(max(c[5], c[6]), max(c[9], c[10]));
    return clamp(o, mn, mx);
}

void main() {
    vec2 pos = uv * srcSize;
    vec3 e = easu(pos);
    if (rcas <= 0.0) { fragColor = vec4(e, 1.0); return; }
    vec2 st = srcSize / dstSize;
    vec3 b = easu(pos + vec2(0.0, -st.y));
    vec3 d = easu(pos + vec2(-st.x, 0.0));
    vec3 f = easu(pos + vec2( st.x, 0.0));
    vec3 h = easu(pos + vec2(0.0,  st.y));
    vec3 mn4 = min(min(b, d), min(f, h));
    vec3 mx4 = max(max(b, d), max(f, h));
    vec3 hitMin = min(mn4, e) / (4.0 * mx4 + 1e-5);
    vec3 hitMax = (1.0 - max(mx4, e)) / min(4.0 * mn4 - 4.0, -1e-5);
    vec3 lobe3 = max(-hitMin, hitMax);
    float lobe = max(lobe3.r, max(lobe3.g, lobe3.b));
    lobe = max(-0.1875, min(lobe, 0.0)) * rcas;
    vec3 o = (lobe * (b + d + f + h) + e) / (4.0 * lobe + 1.0);
    fragColor = vec4(clamp(o, 0.0, 1.0), 1.0);
}
"""

INTERP_FRAG = """#version 330 core
out vec4 fragColor;
uniform sampler2D texA;
uniform sampler2D texB;
uniform sampler2D flowF;
uniform sampler2D flowB;
uniform vec2  size;
uniform float t;
const float OCC = 18.0 / 255.0;

void warps(vec2 p, out vec3 wa, out vec3 wb) {
    vec2 uv = p / size;
    wa = texture(texA, (p - t * texture(flowF, uv).rg) / size).rgb;
    wb = texture(texB, (p - (1.0 - t) * texture(flowB, uv).rg) / size).rgb;
}
float diffAt(vec2 p) {
    vec3 a, b;
    warps(p, a, b);
    return dot(abs(a - b), vec3(1.0 / 3.0));
}
void main() {
    vec2 p = gl_FragCoord.xy;
    vec3 wa, wb;
    warps(p, wa, wb);
    float d = 0.4 * dot(abs(wa - wb), vec3(1.0 / 3.0))
            + 0.15 * (diffAt(p + vec2(3.0, 0.0)) + diffAt(p - vec2(3.0, 0.0))
                    + diffAt(p + vec2(0.0, 3.0)) + diffAt(p - vec2(0.0, 3.0)));
    float occ = clamp((d - OCC) / OCC, 0.0, 1.0);
    vec3 nearest = t < 0.5 ? wa : wb;
    fragColor = vec4(mix(mix(wa, wb, t), nearest, occ), 1.0);
}
"""

class Feed:
    MAX_BUF = 24

    def __init__(self, q, st, present, hud_lines, hud_on, refresh):
        self.q, self.st, self.present = q, st, present
        self.hud_lines, self.hud_on = hud_lines, hud_on
        self.period = 1.0 / (refresh if refresh > 0 else 60.0)
        self.lead = 0.5 * self.period
        self.buf = collections.deque()
        self.cur = None
        self.new = False
        self._hud_t, self._hud_lines, self._hud_fg = -1e9, None, None

    def drain(self):
        try:
            while True:
                self.buf.append(self.q.get_nowait())
        except queue.Empty:
            pass
        while len(self.buf) > self.MAX_BUF:
            self.buf.popleft()
            self.st["stale"] += 1

    def due(self, now):
        self.drain()
        return bool(self.buf) and self.buf[0][0] <= now + self.lead

    def advance(self):
        self.drain()
        now = time.perf_counter()
        pick, n = None, 0
        while self.buf and self.buf[0][0] <= now + self.lead:
            pick = self.buf.popleft()
            n += 1
        if pick is not None:
            when, frame, on = pick
            self.st["stale"] += n - 1
            if now - when > self.period:
                self.st["late"] += 1
            self.st["shown"] += 1
            self.present(now, on)
            self.cur, self.new = (frame, on), True
        return now

    def hud(self, now):
        if not self.hud_on[0] or self.cur is None:
            return None
        if now - self._hud_t >= 0.25 or self._hud_fg != self.cur[1]:
            self._hud_lines, self._hud_t, self._hud_fg = self.hud_lines(now, self.cur[1]), now, self.cur[1]
        return self._hud_lines


FIT_MODE = "auto"


def _fit(w, h, W, H):
    global FIT_MODE
    mode = FIT_MODE
    if mode == "auto":
        return 0, 0, W, H
    if mode == "stretch":
        return 0, 0, W, H
    s = max(W / w, H / h) if mode == "crop" else min(W / w, H / h)
    vw, vh = max(1, int(w * s)), max(1, int(h * s))
    return (W - vw) // 2, (H - vh) // 2, vw, vh


def _draw_hud(p, lines):
    p.setFont(QFont("monospace", 10))
    y = 22
    for t in lines:
        p.setPen(QColor(0, 0, 0))
        p.drawText(11, y + 1, t)
        p.setPen(QColor(0, 255, 0))
        p.drawText(10, y, t)
        y += 18


def set_click_through(win_id):
    import ctypes
    import ctypes.util
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xfx = ctypes.CDLL(ctypes.util.find_library("Xfixes") or "libXfixes.so.3")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xfx.XFixesCreateRegion.restype = ctypes.c_ulong
        xfx.XFixesCreateRegion.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        xfx.XFixesSetWindowShapeRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                                   ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        xfx.XFixesDestroyRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return False
        try:
            region = xfx.XFixesCreateRegion(dpy, None, 0)
            xfx.XFixesSetWindowShapeRegion(dpy, int(win_id), 2, 0, 0, region)
            xfx.XFixesDestroyRegion(dpy, region)
            x11.XFlush(dpy)
        finally:
            x11.XCloseDisplay(dpy)
        return True
    except Exception:
        return False


def keep_click_through(view):
    set_click_through(view.winId())
    timer = QTimer(view)
    timer.timeout.connect(lambda: set_click_through(view.winId()))
    timer.start(1500)
    view._click_timer = timer


class GLView(QOpenGLWindow):
    def __init__(self, feed, rcas, gpu_flag):
        super().__init__()
        self.feed, self.rcas = feed, rcas
        self.gpu_flag = gpu_flag
        self.gl_ok = True
        self.interp_ok = False
        self.frames = collections.OrderedDict()
        self.flows = collections.OrderedDict()
        self.fbo = self.fbo_tex = self.fbo_size = None
        self.last = 0.0
        
        tick = QTimer(self)
        tick.setTimerType(Qt.TimerType.PreciseTimer)
        tick.timeout.connect(self._pump)
        tick.start(1)
        self._tick = tick

    def _pump(self):
        now = time.perf_counter()
        if self.feed.due(now) or now - self.last >= 0.1:
            self.update()

    def initializeGL(self):
        try:
            from OpenGL import GL
            from OpenGL.GL import shaders
            self.GL = GL
            self.prog = shaders.compileProgram(
                shaders.compileShader(VERT, GL.GL_VERTEX_SHADER),
                shaders.compileShader(FRAG, GL.GL_FRAGMENT_SHADER),
                validate=False)
            self.vao = GL.glGenVertexArrays(1)
            self.loc = {n: GL.glGetUniformLocation(self.prog, n)
                        for n in ("tex", "srcSize", "dstSize", "rcas")}
        except Exception:
            self.gl_ok = False
            self.gpu_flag[0] = False
            return
        if not self.gpu_flag[0]:
            return
        try:
            self.iprog = shaders.compileProgram(
                shaders.compileShader(VERT, GL.GL_VERTEX_SHADER),
                shaders.compileShader(INTERP_FRAG, GL.GL_FRAGMENT_SHADER),
                validate=False)
            self.iloc = {n: GL.glGetUniformLocation(self.iprog, n)
                         for n in ("texA", "texB", "flowF", "flowB", "size", "t")}
            self.fbo = GL.glGenFramebuffers(1)
            self.interp_ok = True
        except Exception:
            self.gpu_flag[0] = False

    def _tex_params(self, GL, filt):
        for k, v in ((GL.GL_TEXTURE_MIN_FILTER, filt), (GL.GL_TEXTURE_MAG_FILTER, filt),
                     (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE),
                     (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
            GL.glTexParameteri(GL.GL_TEXTURE_2D, k, v)

    def _cached(self, cache, arr, limit, make):
        key = id(arr)
        hit = cache.get(key)
        if hit is not None:
            cache.move_to_end(key)
            return hit[1]
        tex = make(arr)
        cache[key] = (arr, tex)
        while len(cache) > limit:
            _, (_, old) = cache.popitem(last=False)
            self.GL.glDeleteTextures([old])
        return tex

    def _make_frame_tex(self, arr):
        GL = self.GL
        h, w = arr.shape[:2]
        tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        self._tex_params(GL, GL.GL_LINEAR)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, w, h, 0, GL.GL_BGR,
                        GL.GL_UNSIGNED_BYTE, np.ascontiguousarray(arr))
        return tex

    def _make_flow_tex(self, arr):
        GL = self.GL
        h, w = arr.shape[:2]
        tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        self._tex_params(GL, GL.GL_LINEAR)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RG16F, w, h, 0, GL.GL_RG, GL.GL_FLOAT,
                        np.ascontiguousarray(arr, dtype=np.float32))
        return tex

    def _frame_tex(self, arr):
        return self._cached(self.frames, arr, 6, self._make_frame_tex)

    def _flow_tex(self, arr):
        return self._cached(self.flows, arr, 6, self._make_flow_tex)

    def _ensure_fbo(self, w, h):
        GL = self.GL
        if self.fbo_size == (w, h):
            return
        if self.fbo_tex is not None:
            GL.glDeleteTextures([self.fbo_tex])
        self.fbo_tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.fbo_tex)
        self._tex_params(GL, GL.GL_NEAREST)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0, GL.GL_RGBA,
                        GL.GL_UNSIGNED_BYTE, None)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.fbo_tex, 0)
        self.fbo_size = (w, h)

    def _interp(self, g):
        GL = self.GL
        h, w = g.cur.shape[:2]
        self._ensure_fbo(w, h)
        texs = (self._frame_tex(g.prev), self._frame_tex(g.cur),
                self._flow_tex(g.fw), self._flow_tex(g.bw))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glViewport(0, 0, w, h)
        GL.glUseProgram(self.iprog)
        for unit, (tex, name) in enumerate(zip(texs, ("texA", "texB", "flowF", "flowB"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.iloc[name], unit)
        GL.glUniform2f(self.iloc["size"], float(w), float(h))
        GL.glUniform1f(self.iloc["t"], float(g.t))
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        return self.fbo_tex

    def paintGL(self):
        self.last = time.perf_counter()
        feed = self.feed
        now = feed.advance()
        dpr = self.devicePixelRatio()
        W, H = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        if feed.cur is not None and self.gl_ok:
            try:
                self._draw_gl(feed, W, H)
            except Exception:
                self.gl_ok = False
                self.gpu_flag[0] = False
        lines = feed.hud(now)
        if feed.cur is not None and not self.gl_ok:
            p = QPainter(self)
            p.fillRect(0, 0, W, H, Qt.GlobalColor.black)
            if lines:
                _draw_hud(p, lines)
            p.end()
        elif lines:
            p = QPainter(self)
            _draw_hud(p, lines)
            p.end()
        feed.new = False

    def _draw_gl(self, feed, W, H):
        GL = self.GL
        frame = feed.cur[0]
        tex = None
        if isinstance(frame, GenRef):
            h, w = frame.shape[:2]
            if self.interp_ok:
                try:
                    tex = self._interp(frame)
                except Exception:
                    self.interp_ok = False
                    self.gpu_flag[0] = False
                    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                    GL.glUseProgram(0)
            if tex is None:
                frame = frame.render()
        if tex is None:
            tex = self._frame_tex(frame)
            h, w = frame.shape[:2]
        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        x, y, vw, vh = _fit(w, h, W, H)
        GL.glViewport(x, y, vw, vh)
        GL.glUseProgram(self.prog)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glUniform1i(self.loc["tex"], 0)
        GL.glUniform2f(self.loc["srcSize"], float(w), float(h))
        GL.glUniform2f(self.loc["dstSize"], float(vw), float(vh))
        GL.glUniform1f(self.loc["rcas"], self.rcas)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                QGuiApplication.quit()           # (quitOnLastWindowClosed está desligado)
        super().changeEvent(event)


def run_overlay(q, size, st, present, hud_lines, hud_on, on_fg, *, output=0, geometry=None,
                soft=False, managed=False, sharpness=0.2, seconds=0, fit="auto",
                gpu_interp=None, tracker=None):
    """`tracker` (winfind.WindowTracker) faz o overlay assumir a posição e o tamanho da janela do
    app e segui-la; sem ele (ou se o app não for achado) cobre `geometry` ou o monitor inteiro."""
    global FIT_MODE
    FIT_MODE = fit
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(["framegen-overlay"])
    app.setQuitOnLastWindowClosed(False)   # esconder o overlay (app minimizado) não pode encerrar
    screens = app.screens()
    scr = screens[min(max(output, 0), len(screens) - 1)]
    track_geo = None
    if tracker is not None:
        try:
            track_geo = tracker.poll(time.perf_counter())
        except Exception as e:
            print(f"overlay: acompanhamento da janela indisponível ({e})")
            tracker = None
    if track_geo:
        x, y, w, h = track_geo
        scr = app.screenAt(QPoint(x + w // 2, y + h // 2)) or scr
    elif geometry:
        x, y, w, h = (int(v) for v in geometry.split(","))
    else:
        g = scr.geometry()
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
        if tracker is not None:
            print("overlay: janela do app não encontrada; cobrindo o monitor até achá-la "
                  "(apps Wayland nativos não são detectáveis: use --geometry x,y,l,a)")
    refresh = scr.refreshRate() or 60.0

    gpu = gpu_interp if gpu_interp is not None else [False]
    if soft:
        gpu[0] = False
    feed = Feed(q, st, present, hud_lines, hud_on, refresh)
    view = GLView(feed, 0.0 if sharpness < 0 else 2.0 ** -sharpness, gpu)
    view.setTitle("framegen-overlay")
    view.setScreen(scr)
    
    flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
    if managed:
        flags |= Qt.WindowType.Tool
    else:
        flags |= Qt.WindowType.X11BypassWindowManagerHint
        
    view.setFlags(flags)
    view.setGeometry(x, y, w, h)
    view.show()
    view.setGeometry(x, y, w, h)
    keep_click_through(view)

    cur_geo = [(x, y, w, h)]

    def follow():
        """Mantém o overlay exatamente sobre a janela do app: move, redimensiona e esconde
        quando ela é minimizada ou fechada."""
        try:
            geo = tracker.poll(time.perf_counter())
        except Exception as e:
            print(f"overlay: acompanhamento da janela desligado ({e})")
            follow_timer.stop()
            return
        if geo is None:
            if tracker.ever_found and view.isVisible():
                view.hide()
            return
        if not view.isVisible():
            view.show()
            set_click_through(view.winId())
        if geo != cur_geo[0]:
            view.setGeometry(*geo)
            cur_geo[0] = geo

    follow_timer = QTimer()
    if tracker is not None:
        follow_timer.timeout.connect(follow)
        follow_timer.start(250)

    signal.signal(signal.SIGUSR1, lambda *_: on_fg())
    signal.signal(signal.SIGUSR2, lambda *_: hud_on.__setitem__(0, not hud_on[0]))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    
    pump = QTimer()
    pump.timeout.connect(feed.drain)
    pump.start(100)
    if seconds:
        QTimer.singleShot(int(seconds * 1000), app.quit)

    app.exec()
    pump.stop()
    follow_timer.stop()
    view.close()
    if tracker is not None:
        tracker.close()