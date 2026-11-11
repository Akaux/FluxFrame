"""
Overlay "máscara" do framegen.

Mostra os quadros gerados por cima da tela e deixa mouse e teclado ATRAVESSAREM a
janela (click-through): você continua controlando o jogo/app que está embaixo, só que
vendo a imagem com frame generation.

  captura (portal, só a janela do jogo) -> flow/interpolação (CPU) -> este overlay (GPU)

Como funciona
  * Janela sem borda, sempre no topo, com região de entrada vazia (Qt.WindowTransparentForInput)
    e override-redirect (X11BypassWindowManagerHint). Roda via XWayland (xcb) de propósito:
    é o jeito que fica acima de jogos em tela cheia no KDE/GNOME/Hyprland/Sway sem layer-shell.
  * Upscale no shader, inspirado no FSR 1 da AMD (MIT):
      EASU-lite : Lanczos-2 4x4 com kernel orientado pela direção da borda + anti-ringing
      RCAS      : nitidez adaptativa ao contraste (fórmula do FSR 1)
    É uma reimplementação simplificada, não o ffx_fsr1.h original.
  * Apresentação por prazo: a cada vsync escolhe o quadro mais recente cujo horário já
    chegou (`when` vindo do agendador do live.py). Quadros atrasados são pulados, nunca acumulados.
  * Teclado não chega numa janela click-through, então os controles são sinais:
        SIGUSR1  liga/desliga a geração     SIGUSR2  mostra/esconde o HUD
        SIGINT / SIGTERM  sai
    ex.: pkill -USR1 -f live.py   (ligue isso a um atalho do seu ambiente)
"""
import collections
import os
import queue
import signal
import time

# O OpenCV (import cv2) aponta o Qt para os plugins DELE; o PySide6 precisa dos próprios.
os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
os.environ["QT_QPA_PLATFORM"] = "xcb"          # XWayland no Wayland; X11 nativo no X11
os.environ.setdefault("PYOPENGL_PLATFORM", "glx")

import numpy as np
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImage, QPainter,
                           QRasterWindow, QSurfaceFormat)
from PySide6.QtOpenGL import QOpenGLWindow

# ------------------------------------------------------------------ shaders
VERT = """#version 330 core
out vec2 uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);   // triângulo cobrindo a tela
    uv = vec2(p.x, 1.0 - p.y);                                // linha 0 da imagem = topo
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

FRAG = """#version 330 core
in vec2 uv;
out vec4 fragColor;
uniform sampler2D tex;
uniform vec2  srcSize;
uniform vec2  dstSize;
uniform float rcas;          // 0 = RCAS desligado; senão exp2(-nitidez), como no FSR 1

vec3 T(ivec2 p) { return texelFetch(tex, clamp(p, ivec2(0), ivec2(srcSize) - 1), 0).rgb; }
float L(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

float lanczos2(float x) {
    x = abs(x);
    if (x < 1e-4) return 1.0;
    if (x >= 2.0) return 0.0;
    float px = 3.14159265 * x;
    return 2.0 * sin(px) * sin(px * 0.5) / (px * px);
}

// EASU-lite: reamostra a origem na posição `pos` (em pixels da origem).
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

    // gradiente de luma no centro da célula (Sobel simplificado)
    float gx = 0.5 * (l[2] - l[1]) + (l[6] - l[5]) + (l[10] - l[9]) + 0.5 * (l[14] - l[13]);
    float gy = 0.5 * (l[8] - l[4]) + (l[9] - l[5]) + (l[10] - l[6]) + 0.5 * (l[11] - l[7]);
    vec2 g = vec2(gx, gy);
    float gl = length(g);
    vec2 n = gl > 1e-4 ? g / gl : vec2(1.0, 0.0);      // normal da borda
    vec2 t = vec2(-n.y, n.x);                          // tangente da borda
    float s = clamp(gl * 2.0, 0.0, 1.0);
    float across = 1.0 + 0.5 * s;     // atravessando a borda: kernel mais curto = mais nítido
    float along  = 1.0 - 0.3 * s;     // ao longo da borda: kernel mais longo = sem serrilhado

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
    vec3 mn = min(min(c[5], c[6]), min(c[9], c[10]));   // anti-ringing: limita ao 2x2 central
    vec3 mx = max(max(c[5], c[6]), max(c[9], c[10]));
    return clamp(o, mn, mx);
}

void main() {
    vec2 pos = uv * srcSize;
    vec3 e = easu(pos);
    if (rcas <= 0.0) { fragColor = vec4(e, 1.0); return; }

    vec2 st = srcSize / dstSize;                         // 1 pixel de saída, em pixels da origem
    vec3 b = easu(pos + vec2(0.0, -st.y));
    vec3 d = easu(pos + vec2(-st.x, 0.0));
    vec3 f = easu(pos + vec2( st.x, 0.0));
    vec3 h = easu(pos + vec2(0.0,  st.y));

    // RCAS (FSR 1): lobo negativo limitado para não estourar preto/branco
    vec3 mn4 = min(min(b, d), min(f, h));
    vec3 mx4 = max(max(b, d), max(f, h));
    vec3 hitMin = min(mn4, e) / (4.0 * mx4 + 1e-5);
    vec3 hitMax = (1.0 - max(mx4, e)) / min(4.0 * mn4 - 4.0, -1e-5);
    vec3 lobe3 = max(-hitMin, hitMax);
    float lobe = max(lobe3.r, max(lobe3.g, lobe3.b));
    lobe = max(-0.1875, min(lobe, 0.0)) * rcas;          // 0.1875 = 0.25 - 1/16
    vec3 o = (lobe * (b + d + f + h) + e) / (4.0 * lobe + 1.0);
    fragColor = vec4(clamp(o, 0.0, 1.0), 1.0);
}
"""


# --------------------------------------------------------------- fila / prazo
class Feed:
    """Recebe (hora_de_exibir, quadro, fg_on) do agendador e escolhe o quadro de cada vsync.
    `present(agora, fg_on)` é chamado a cada quadro único que vai para a tela (benchmark)."""
    MAX_BUF = 24

    def __init__(self, q, st, present, hud_lines, hud_on, refresh):
        self.q, self.st, self.present = q, st, present
        self.hud_lines, self.hud_on = hud_lines, hud_on
        self.period = 1.0 / refresh
        self.lead = 0.5 * self.period          # um quadro que vence antes do próximo vsync já serve
        self.buf = collections.deque()
        self.cur = None                        # (quadro, fg_on)
        self.new = False                       # há quadro novo para subir à GPU

    def drain(self):
        try:
            while True:
                self.buf.append(self.q.get_nowait())
        except queue.Empty:
            pass
        while len(self.buf) > self.MAX_BUF:    # janela parada? não deixa a memória crescer
            self.buf.popleft()
            self.st["stale"] += 1

    def advance(self):
        self.drain()
        now = time.perf_counter()
        pick, n = None, 0
        while self.buf and self.buf[0][0] <= now + self.lead:
            pick = self.buf.popleft()
            n += 1
        if pick is not None:
            when, frame, on = pick
            self.st["stale"] += n - 1          # venceram juntos: mostra só o mais novo
            if now - when > self.period:
                self.st["late"] += 1
            self.st["shown"] += 1
            self.present(now, on)
            self.cur, self.new = (frame, on), True
        return now

    def hud(self, now):
        if not self.hud_on[0] or self.cur is None:
            return None
        return self.hud_lines(now, self.cur[1])


FIT_MODE = "fit"      # "fit" = barras pretas | "stretch" = preenche (deforma) | "crop" = preenche (corta)


def _fit(w, h, W, H):
    """Retângulo (x, y, largura, altura) onde a imagem w x h é desenhada na janela W x H.
    fit: mantém a proporção (barras pretas); stretch: ocupa tudo, deformando se as proporções
    diferem; crop: ocupa tudo mantendo a proporção, cortando o que passa das bordas."""
    if FIT_MODE == "stretch":
        return 0, 0, W, H
    s = max(W / w, H / h) if FIT_MODE == "crop" else min(W / w, H / h)
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


def _paint_qt(p, frame, W, H):
    """Caminho sem shader: QPainter com filtro suave (fallback)."""
    h, w = frame.shape[:2]
    img = QImage(frame.data, w, h, w * 3, QImage.Format.Format_BGR888)
    x, y, vw, vh = _fit(w, h, W, H)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawImage(QRect(x, y, vw, vh), img)


# ------------------------------------------------------- click-through (X11)
def set_click_through(win_id):
    """Zera a região de ENTRADA da janela (XFixes, ShapeInput) direto no servidor X/XWayland.

    Com a região vazia, o compositor entrega mouse e teclado à janela que está embaixo (o jogo).
    O Qt já tenta isso via WindowTransparentForInput, mas só na criação da janela; aqui ela é
    reaplicada de forma explícita (depois do show, de resize e periodicamente), usando só
    ctypes (libX11 + libXfixes), sem dependências extras. Retorna True se aplicou."""
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
            region = xfx.XFixesCreateRegion(dpy, None, 0)        # região vazia
            xfx.XFixesSetWindowShapeRegion(dpy, int(win_id), 2, 0, 0, region)   # 2 = ShapeInput
            xfx.XFixesDestroyRegion(dpy, region)
            x11.XFlush(dpy)
        finally:
            x11.XCloseDisplay(dpy)
        return True
    except Exception as e:
        print(f"overlay: não consegui aplicar o click-through via XFixes ({e})")
        return False


def keep_click_through(view):
    """Aplica o click-through agora e o reaplica a cada 1,5 s (cobre resize/remap da janela)."""
    ok = [set_click_through(view.winId())]
    if not ok[0]:
        print("overlay: AVISO: click-through não confirmado; o mouse pode não atravessar.")

    def tick():
        set_click_through(view.winId())

    timer = QTimer(view)
    timer.timeout.connect(tick)
    timer.start(1500)
    view._click_timer = timer
    return ok[0]


# ------------------------------------------------------------------ janelas
class GLView(QOpenGLWindow):
    def __init__(self, feed, rcas):
        super().__init__()
        self.feed, self.rcas = feed, rcas
        self.gl_ok = True
        self.tex = self.tex_size = None
        self.last = 0.0
        watchdog = QTimer(self)                 # garante progresso se o vsync parar
        watchdog.timeout.connect(self.update)
        watchdog.start(250)
        self._watchdog = watchdog

    def initializeGL(self):
        try:
            from OpenGL import GL
            from OpenGL.GL import shaders
            self.GL = GL
            self.prog = shaders.compileProgram(
                shaders.compileShader(VERT, GL.GL_VERTEX_SHADER),
                shaders.compileShader(FRAG, GL.GL_FRAGMENT_SHADER),
                validate=False)                 # validar sem VAO falha no core profile
            self.vao = GL.glGenVertexArrays(1)
            self.tex = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
            for k, v in ((GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST),
                         (GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST),
                         (GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE),
                         (GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, k, v)
            self.loc = {n: GL.glGetUniformLocation(self.prog, n)
                        for n in ("tex", "srcSize", "dstSize", "rcas")}
            print(f"overlay: GL {GL.glGetString(GL.GL_VERSION).decode()} | "
                  f"{GL.glGetString(GL.GL_RENDERER).decode()}")
        except Exception as e:
            print(f"overlay: OpenGL indisponível ({e}); usando QPainter (mais lento).")
            self.gl_ok = False

    def _upload(self, frame):
        GL = self.GL
        h, w = frame.shape[:2]
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        frame = np.ascontiguousarray(frame)
        if self.tex_size != (w, h):
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, w, h, 0,
                            GL.GL_BGR, GL.GL_UNSIGNED_BYTE, frame)
            self.tex_size = (w, h)
        else:
            GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, w, h,
                               GL.GL_BGR, GL.GL_UNSIGNED_BYTE, frame)

    def paintGL(self):
        feed = self.feed
        now = feed.advance()
        dpr = self.devicePixelRatio()
        W, H = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        if feed.cur is not None and self.gl_ok:
            try:
                self._draw_gl(feed, W, H)
            except Exception as e:
                print(f"overlay: erro no OpenGL ({e}); passando para QPainter.")
                self.gl_ok = False
        lines = feed.hud(now)
        if feed.cur is not None and not self.gl_ok:
            p = QPainter(self)
            p.fillRect(0, 0, W, H, Qt.GlobalColor.black)
            _paint_qt(p, feed.cur[0], W, H)
            if lines:
                _draw_hud(p, lines)
            p.end()
        elif lines:
            p = QPainter(self)
            _draw_hud(p, lines)
            p.end()
        feed.new = False
        # proteção: se o vsync não bloquear, não gira a 100% de CPU disputando o GIL
        dt = time.perf_counter() - self.last
        if dt < 0.5 * feed.period:
            time.sleep(0.5 * feed.period - dt)
        self.last = time.perf_counter()
        self.update()                           # próximo vsync

    def _draw_gl(self, feed, W, H):
        GL = self.GL
        frame = feed.cur[0]
        h, w = frame.shape[:2]
        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        if feed.new or self.tex_size is None:
            self._upload(frame)
        x, y, vw, vh = _fit(w, h, W, H)
        GL.glViewport(x, y, vw, vh)
        GL.glUseProgram(self.prog)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glUniform1i(self.loc["tex"], 0)
        GL.glUniform2f(self.loc["srcSize"], float(w), float(h))
        GL.glUniform2f(self.loc["dstSize"], float(vw), float(vh))
        GL.glUniform1f(self.loc["rcas"], self.rcas)
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)


class SoftView(QRasterWindow):
    """Sem OpenGL: escala com QPainter (use --soft; é bem mais pesado para a CPU)."""

    def __init__(self, feed, refresh):
        super().__init__()
        self.feed = feed
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.update)
        self.timer.start(max(1, int(1000 / refresh)))

    def paintEvent(self, _):
        feed = self.feed
        now = feed.advance()
        W, H = self.width(), self.height()
        p = QPainter(self)
        p.fillRect(0, 0, W, H, Qt.GlobalColor.black)
        if feed.cur is not None:
            _paint_qt(p, feed.cur[0], W, H)
            lines = feed.hud(now)
            if lines:
                _draw_hud(p, lines)
        p.end()
        feed.new = False


# --------------------------------------------------------------------- main
def run_overlay(q, size, st, present, hud_lines, hud_on, on_fg, *, output=0, geometry=None,
                soft=False, managed=False, sharpness=0.2, seconds=0, fit="fit"):
    """Abre o overlay e bloqueia até sair. `q` é a fila (hora, quadro, fg) do live.py."""
    global FIT_MODE
    FIT_MODE = fit
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QGuiApplication(["framegen-overlay"])
    app.setApplicationName("framegen-overlay")
    screens = app.screens()
    scr = screens[min(max(output, 0), len(screens) - 1)]
    refresh = scr.refreshRate() or 60.0
    if geometry:
        x, y, w, h = (int(v) for v in geometry.split(","))
    else:
        g = scr.geometry()
        x, y, w, h = g.x(), g.y(), g.width(), g.height()

    feed = Feed(q, st, present, hud_lines, hud_on, refresh)
    view = SoftView(feed, refresh) if soft else GLView(
        feed, 0.0 if sharpness < 0 else 2.0 ** -sharpness)
    view.setTitle("framegen-overlay")
    view.setScreen(scr)
    flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
    if managed:
        flags |= Qt.WindowType.Tool            # janela normal: o WM pode reordenar
    else:
        flags |= Qt.WindowType.X11BypassWindowManagerHint   # override-redirect: fica acima de tudo
    view.setFlags(flags)
    view.setGeometry(x, y, w, h)
    view.show()
    view.setGeometry(x, y, w, h)
    # Entrada atravessa a janela: região de input vazia + foco nunca vai para o overlay.
    keep_click_through(view)
    app.focusWindowChanged.connect(
        lambda win: print("overlay: AVISO: o overlay recebeu foco") if win is view else None)
    print(f"overlay: {w}x{h}+{x}+{y} @ {refresh:.0f} Hz | origem {size[0]}x{size[1]} | "
          f"{'QPainter' if soft else 'GL + FSR-like'} | "
          "SIGUSR1 = FG on/off, SIGUSR2 = HUD, Ctrl+C no terminal = sair")

    signal.signal(signal.SIGUSR1, lambda *_: on_fg())
    signal.signal(signal.SIGUSR2, lambda *_: hud_on.__setitem__(0, not hud_on[0]))
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    pump = QTimer()                              # deixa o Python tratar sinais durante o loop do Qt
    pump.timeout.connect(feed.drain)
    pump.start(100)
    if seconds:
        QTimer.singleShot(int(seconds * 1000), app.quit)

    app.exec()
    pump.stop()
    view.close()