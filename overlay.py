"""
Overlay do FluxFrame: janela Qt/OpenGL que mostra o frame capturado (ou gerado) com
upscale FSR/DLSS e click-through.

Modos de upscale (--fsr-mode):
  fsr1   espacial leve: EASU 4x4 + RCAS (16 taps)
  fsr2   espacial + acumulação temporal (TAA com motion vectors do flow)
  fsr3   fsr2 + frame gen (o FG já é ligado por padrão no overlay)
  dlss1  temporal + upscale bilateral 6x6 + Sobel (GPU-heavy)
  fsr4   comportamento antigo (sem temporal, sem upscale pesado)

Regra:
- Se `follow_size=True` (padrão do painel em modo "Automático"), o overlay assume
  exatamente a geometria da janela do jogo e a segue se ela for movida ou redimensionada.
- Sem `follow_size` (ex.: `--geometry` explícito), cobre o `geometry` ou o monitor inteiro.
- `mask=True` (`--mask`) bypassa o WM; nesse caso o overlay sempre cobre o monitor e
  Alt+Tab fica travado.
"""
import collections
import ctypes
import ctypes.util
import os
import queue
import signal
import sys
import time
import traceback

os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
os.environ["QT_QPA_PLATFORM"] = "xcb"
os.environ.setdefault("PYOPENGL_PLATFORM", "glx")
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ.pop("QT_SCALE_FACTOR", None)

import numpy as np
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLWindow

from framegen import GenRef


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

TEMPORAL_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D texHistory;
uniform sampler2D texCurrent;
uniform sampler2D texFlow;
uniform vec2  size;
uniform float alpha;

void main() {
    vec2 p = uv * size;
    vec2 flow = texture(texFlow, uv).rg;
    vec3 cur = texture(texCurrent, uv).rgb;
    vec3 hist = texture(texHistory, (p - flow) / size).rgb;

    vec2 px = 1.0 / size;
    vec3 mn = cur, mx = cur;
    for (int j = -1; j <= 1; j++)
        for (int i = -1; i <= 1; i++) {
            if (i == 0 && j == 0) continue;
            vec3 s = texture(texCurrent, uv + vec2(i, j) * px).rgb;
            mn = min(mn, s);
            mx = max(mx, s);
        }
    vec3 mid = 0.5 * (mn + mx);
    vec3 ext = 0.5 * (mx - mn) * 1.25;
    hist = clamp(hist, mid - ext, mid + ext);

    float m = clamp(length(flow) / 8.0, 0.0, 1.0);
    float a = mix(alpha, 0.9, m);
    fragColor = vec4(mix(hist, cur, a), 1.0);
}
"""

DLSS_FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
uniform vec2 srcSize;
uniform vec2 dstSize;

vec3 T(ivec2 p) { return texelFetch(tex, clamp(p, ivec2(0), ivec2(srcSize) - 1), 0).rgb; }
float L(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

float sinc(float x) {
    if (abs(x) < 1e-4) return 1.0;
    return sin(3.14159265 * x) / (3.14159265 * x);
}
float lanczos3(float x) {
    x = abs(x);
    if (x >= 3.0) return 0.0;
    return sinc(x) * sinc(x / 3.0);
}

void main() {
    vec2 p  = uv * srcSize - 0.5;
    ivec2 ip = ivec2(floor(p));
    vec2  f  = p - vec2(ip);

    vec3 c_center = T(ip);
    float l_center = L(c_center);

    float gx = 0.0, gy = 0.0;
    for (int j = -2; j <= 2; j++)
        for (int i = -2; i <= 2; i++) {
            float l = L(T(ip + ivec2(i, j)));
            gx += float(i) * l;
            gy += float(j) * l;
        }
    vec2 grad = vec2(gx, gy);
    float gmag = length(grad);
    vec2 gdir = gmag > 1e-4 ? grad / gmag : vec2(1.0, 0.0);
    vec2 perp = vec2(-gdir.y, gdir.x);

    float sharp = clamp(gmag * 3.0, 0.0, 1.0);
    float along = 1.0 - 0.4 * sharp;
    float across = 1.0 + 0.6 * sharp;

    vec3 sum = vec3(0.0);
    float wsum = 0.0;
    for (int j = -3; j <= 2; j++)
        for (int i = -3; i <= 2; i++) {
            vec2 d = vec2(float(i), float(j)) - f;
            float da = dot(d, gdir) * along;
            float dp = dot(d, perp) * across;
            float w = lanczos3(length(vec2(da, dp)));

            vec3 s = T(ip + ivec2(i, j));
            float dl = abs(L(s) - l_center);
            w *= 1.0 / (1.0 + dl * dl * 4.0);

            sum += s * w;
            wsum += w;
        }
    vec3 o = sum / max(wsum, 1e-5);

    vec3 mn = c_center, mx = c_center;
    for (int j = -1; j <= 1; j++)
        for (int i = -1; i <= 1; i++) {
            vec3 s = T(ip + ivec2(i, j));
            mn = min(mn, s);
            mx = max(mx, s);
        }
    o = clamp(o, mn, mx);

    fragColor = vec4(o, 1.0);
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


def _log(msg):
    print(f"overlay: {msg}", file=sys.stderr, flush=True)


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
            if len(pick) == 4:
                when, frame, on, flow = pick
            else:
                when, frame, on = pick
                flow = None
            self.st["stale"] += n - 1
            if now - when > self.period:
                self.st["late"] += 1
            self.st["shown"] += 1
            self.present(now, on)
            self.cur, self.new = (frame, on, flow), True
        return now

    def hud(self, now):
        if not self.hud_on[0] or self.cur is None:
            return None
        if now - self._hud_t >= 0.25 or self._hud_fg != self.cur[1]:
            self._hud_lines, self._hud_t, self._hud_fg = (
                self.hud_lines(now, self.cur[1]), now, self.cur[1])
        return self._hud_lines


FIT_MODE = "auto"


def _fit(w, h, W, H):
    global FIT_MODE
    mode = FIT_MODE
    if mode == "stretch":
        return 0, 0, W, H
    if mode == "auto":
        src_aspect = w / h
        dst_aspect = W / H
        if abs(src_aspect - dst_aspect) / dst_aspect < 0.06:
            return 0, 0, W, H
        mode = "fit"
    if mode == "crop":
        s = max(W / w, H / h)
    else:
        s = min(W / w, H / h)
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


def set_transient_for(overlay_id, parent_id):
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XSetTransientForHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            return False
        try:
            x11.XSetTransientForHint(dpy, int(overlay_id), int(parent_id))
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
    def __init__(self, feed, rcas, gpu_flag, fsr_mode="fsr1"):
        super().__init__()
        self.feed, self.rcas = feed, rcas
        self.gpu_flag = gpu_flag
        self.fsr_mode = fsr_mode
        self.gl_ok = True
        self.interp_ok = False
        self.temporal_ok = fsr_mode in ("fsr2", "fsr3", "dlss1")
        self.dlss_ok = fsr_mode == "dlss1"
        self.frames = collections.OrderedDict()
        self.flows = collections.OrderedDict()
        self.fbo = self.fbo_tex = self.fbo_size = None
        self.hist_tex_a = None
        self.hist_tex_b = None
        self.hist_size = None
        self.hist_initialized = False
        self.hist_fbo = None
        self.last = 0.0
        self._logged_first = False

        tick = QTimer(self)
        tick.setTimerType(Qt.TimerType.PreciseTimer)
        tick.timeout.connect(self._pump)
        tick.start(1)
        self._tick = tick

    def _pump(self):
        now = time.perf_counter()
        if self.feed.due(now) or now - self.last >= 0.1:
            self.update()

    def _compile(self, frag):
        from OpenGL.GL import shaders
        from OpenGL import GL
        return shaders.compileProgram(
            shaders.compileShader(VERT, GL.GL_VERTEX_SHADER),
            shaders.compileShader(frag, GL.GL_FRAGMENT_SHADER),
            validate=False)

    def initializeGL(self):
        try:
            from OpenGL import GL
            self.GL = GL
            self.prog = self._compile(FRAG)
            self.vao = GL.glGenVertexArrays(1)
            self.loc = {n: GL.glGetUniformLocation(self.prog, n)
                        for n in ("tex", "srcSize", "dstSize", "rcas")}
        except Exception as e:
            _log(f"FSR 1 indisponível: {e}")
            self.gl_ok = False
            self.gpu_flag[0] = False
            return
        if not self.gpu_flag[0]:
            return
        try:
            self.iprog = self._compile(INTERP_FRAG)
            self.iloc = {n: GL.glGetUniformLocation(self.iprog, n)
                         for n in ("texA", "texB", "flowF", "flowB", "size", "t")}
            self.fbo = GL.glGenFramebuffers(1)
            self.interp_ok = True
        except Exception:
            self.gpu_flag[0] = False
        if self.temporal_ok:
            try:
                self.tprog = self._compile(TEMPORAL_FRAG)
                self.tloc = {n: GL.glGetUniformLocation(self.tprog, n)
                             for n in ("texHistory", "texCurrent", "texFlow", "size", "alpha")}
                self.hist_fbo = GL.glGenFramebuffers(1)
            except Exception as e:
                _log(f"{self.fsr_mode} temporal indisponível: {e}; usando FSR 1")
                self.temporal_ok = False
        if self.dlss_ok:
            try:
                self.dprog = self._compile(DLSS_FRAG)
                self.dloc = {n: GL.glGetUniformLocation(self.dprog, n)
                             for n in ("tex", "srcSize", "dstSize")}
            except Exception as e:
                _log(f"DLSS 1 indisponível: {e}; usando FSR 1")
                self.dlss_ok = False

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

    def _make_rgb8_tex(self, w, h):
        GL = self.GL
        tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        self._tex_params(GL, GL.GL_LINEAR)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, w, h, 0, GL.GL_BGR,
                        GL.GL_UNSIGNED_BYTE, None)
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
        self.fbo_tex = self._make_rgb8_tex(w, h)
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

    def _temporal_accumulate(self, frame, flow):
        GL = self.GL
        h, w = frame.shape[:2]
        if self.hist_tex_a is None or self.hist_size != (w, h):
            if self.hist_tex_a is not None:
                GL.glDeleteTextures([self.hist_tex_a, self.hist_tex_b])
            self.hist_tex_a = self._make_rgb8_tex(w, h)
            self.hist_tex_b = self._make_rgb8_tex(w, h)
            self.hist_size = (w, h)
            self.hist_initialized = False
        if not self.hist_initialized:
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.hist_tex_a)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, w, h, 0, GL.GL_BGR,
                            GL.GL_UNSIGNED_BYTE, np.ascontiguousarray(frame))
            self.hist_initialized = True
            return self.hist_tex_a
        cur_tex = self._frame_tex(frame)
        flow_tex = self._flow_tex(flow)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.hist_fbo)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.hist_tex_b, 0)
        GL.glViewport(0, 0, w, h)
        GL.glUseProgram(self.tprog)
        for unit, (tex, name) in enumerate(zip(
                (self.hist_tex_a, cur_tex, flow_tex),
                ("texHistory", "texCurrent", "texFlow"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glUniform1i(self.tloc[name], unit)
        GL.glUniform2f(self.tloc["size"], float(w), float(h))
        GL.glUniform1f(self.tloc["alpha"], 0.15)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glActiveTexture(GL.GL_TEXTURE0)
        self.hist_tex_a, self.hist_tex_b = self.hist_tex_b, self.hist_tex_a
        return self.hist_tex_a

    def paintGL(self):
        self.last = time.perf_counter()
        feed = self.feed
        now = feed.advance()
        dpr = self.devicePixelRatio()
        W, H = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        if self.gl_ok:
            try:
                from OpenGL import GL as _GL
                _GL.glViewport(0, 0, W, H)
                _GL.glClearColor(0, 0, 0, 1)
                _GL.glClear(_GL.GL_COLOR_BUFFER_BIT)
            except Exception:
                pass
        if feed.cur is not None and self.gl_ok:
            try:
                self._draw_gl(feed, W, H)
            except Exception as e:
                _log(f"erro no desenho GL: {e}")
                traceback.print_exc()
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
        frame, on, flow = feed.cur

        if isinstance(frame, GenRef):
            h, w = frame.shape[:2]
            tex = None
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
                h, w = frame.shape[:2]
        elif self.temporal_ok and flow is not None:
            h, w = frame.shape[:2]
            tex = self._temporal_accumulate(frame, flow)
        else:
            h, w = frame.shape[:2]
            tex = None

        if tex is None:
            tex = self._frame_tex(frame)
            h, w = frame.shape[:2]

        if not self._logged_first:
            self._logged_first = True
            try:
                mean_str = (f"{float(frame.mean()):.1f}"
                            if isinstance(frame, np.ndarray) else "?")
                x_, y_, vw_, vh_ = _fit(w, h, W, H)
                _log(f"primeiro quadro {w}x{h} -> viewport {vw_}x{vh_} "
                     f"framebuffer {W}x{H} fit={FIT_MODE} brilho médio={mean_str}")
                if isinstance(frame, np.ndarray) and float(frame.mean()) < 5.0:
                    _log("AVISO: primeiro quadro quase preto. Se o jogo está em modo "
                         "janela, ele pode ter pausado ao perder o foco (clique na janela). "
                         "Verifique também a linha 'Primeiro quadro: WxH, brilho médio X' "
                         "no terminal do live.py.")
            except Exception:
                pass

        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        x, y, vw, vh = _fit(w, h, W, H)
        GL.glViewport(x, y, vw, vh)

        use_dlss = self.dlss_ok and self.fsr_mode == "dlss1"
        prog = self.dprog if use_dlss else self.prog
        loc  = self.dloc  if use_dlss else self.loc
        GL.glUseProgram(prog)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glUniform1i(loc["tex"], 0)
        GL.glUniform2f(loc["srcSize"], float(w), float(h))
        GL.glUniform2f(loc["dstSize"], float(vw), float(vh))
        if not use_dlss:
            GL.glUniform1f(loc["rcas"], self.rcas)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)


def run_overlay(q, size, st, present, hud_lines, hud_on, on_fg, *, output=0, geometry=None,
                soft=False, managed=False, mask=False, sharpness=0.2, seconds=0, fit="auto",
                gpu_interp=None, tracker=None, follow_size=False, fsr_mode="fsr1"):
    global FIT_MODE
    FIT_MODE = fit

    _log(f"run_overlay inicio: fsr_mode={fsr_mode} fit={fit} mask={mask} "
         f"follow_size={follow_size} tracker={'sim' if tracker else 'nao'}")

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(["framegen-overlay"])
    app.setQuitOnLastWindowClosed(False)
    screens = app.screens()
    if not screens:
        _log("QGuiApplication não encontrou telas; abortando")
        return
    scr = screens[min(max(output, 0), len(screens) - 1)]

    track_geo = None
    if tracker is not None:
        try:
            track_geo = tracker.poll(time.perf_counter())
        except Exception as e:
            _log(f"tracker indisponível ({e})")
            tracker = None

    if follow_size and track_geo:
        x, y, w, h = track_geo
        scr = app.screenAt(QPoint(x + w // 2, y + h // 2)) or scr
    elif geometry:
        x, y, w, h = (int(v) for v in geometry.split(","))
    else:
        g = scr.geometry()
        x, y, w, h = g.x(), g.y(), g.width(), g.height()
        if tracker is not None and follow_size:
            _log("janela do app ainda não encontrada; usando o monitor até achá-la.")

    refresh = scr.refreshRate() or 60.0
    _log(f"monitor '{scr.name()}' {w}x{h}+{x}+{y} refresh={refresh:.0f}Hz")

    gpu = gpu_interp if gpu_interp is not None else [False]
    if soft:
        gpu[0] = False
    feed = Feed(q, st, present, hud_lines, hud_on, refresh)
    view = GLView(feed, 0.0 if sharpness < 0 else 2.0 ** -sharpness, gpu, fsr_mode=fsr_mode)
    view.setTitle("framegen-overlay")
    view.setScreen(scr)

    flags = (Qt.WindowType.FramelessWindowHint
             | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowTransparentForInput
             | Qt.WindowType.WindowDoesNotAcceptFocus)
    if mask:
        flags |= Qt.WindowType.X11BypassWindowManagerHint

    view.setFlags(flags)
    view.setGeometry(x, y, w, h)
    view.show()
    view.setGeometry(x, y, w, h)
    _log(f"janela mostrada (winId=0x{int(view.winId()):x}) {w}x{h}+{x}+{y}")

    keep_click_through(view)

    def reparent(parent_id):
        if parent_id is None or mask:
            return False
        return set_transient_for(view.winId(), parent_id)

    if tracker is not None and tracker.win is not None:
        reparent(tracker.win)

    def follow():
        try:
            geo = tracker.poll(time.perf_counter())
        except Exception as e:
            _log(f"follow desligado ({e})")
            follow_timer.stop()
            return
        if geo is None:
            if tracker.ever_found and view.isVisible():
                view.hide()
            return
        if not view.isVisible():
            view.show()
            set_click_through(view.winId())
            if tracker.win is not None:
                reparent(tracker.win)
        if follow_size:
            if geo != cur_geo[0]:
                _log(f"follow: setGeometry {geo} (anterior {cur_geo[0]})")
                view.setGeometry(*geo)
                cur_geo[0] = geo
                if tracker.win is not None:
                    reparent(tracker.win)

    cur_geo = [(x, y, w, h)]
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

    _log("entrando no event loop")
    try:
        app.exec()
    finally:
        _log("saindo do event loop")
        pump.stop()
        follow_timer.stop()
        view.close()
        if tracker is not None:
            tracker.close()