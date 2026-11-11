"""
Localiza e acompanha a janela de um app (X11 / XWayland) para o overlay cobri-la exatamente.

Usado quando a resolução é "automática": em vez de cobrir o monitor inteiro, o overlay assume a
posição e o tamanho da janela do app e os segue se ela for movida, redimensionada ou minimizada.

Só usa ctypes + libX11 (nada para instalar). Limite: enxerga janelas X11. Jogos via Proton/Wine e a
maioria dos apps X11 rodam no XWayland e aparecem; apps Wayland nativos NÃO aparecem (o Wayland não
deixa um programa comum ler a posição de janelas alheias). Nesse caso o chamador cai para o monitor
inteiro ou para --geometry.

Como escolhe a janela (nesta ordem):
  1. --window TEXTO: só janelas cujo título ou classe contém o texto.
  2. mesma proporção (largura/altura, até 4%; acima de 10% é descartada) do quadro capturado;
  3. a janela ativa/em foco; 4. a mais acima na pilha.
Janelas do próprio framegen (painel, overlay) e docks/menus/notificações são ignoradas.
"""
import contextlib
import ctypes
import ctypes.util
import os
from collections import namedtuple

MIN_W, MIN_H = 160, 120          # janelas menores que isso não são "o app"
ASPECT_TOL = 0.04                # diferença de proporção aceita (decoração de janela etc.)
ASPECT_MAX = 0.10                # acima disso não é a mesma janela: espera em vez de cobrir um app qualquer
SELF_WORDS = ("framegen",)       # título/classe do painel e do overlay
IGNORED_TYPES = ("_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP",
                 "_NET_WM_WINDOW_TYPE_SPLASH", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
                 "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_POPUP_MENU",
                 "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_COMBO",
                 "_NET_WM_WINDOW_TYPE_DND", "_NET_WM_WINDOW_TYPE_MENU")

Win = namedtuple("Win", "id x y w h rank title cls active")


class WinFindError(RuntimeError):
    pass


class _Attrs(ctypes.Structure):
    """XWindowAttributes completa (136 bytes em x86-64). Declarar só os primeiros campos faz o
    Xlib escrever além da memória reservada."""
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int), ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p), ("root", ctypes.c_ulong),
        ("cls", ctypes.c_int), ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long), ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p),
    ]


_ErrFn = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
IS_VIEWABLE, INPUT_OUTPUT = 2, 1


class XConn:
    """Conexão própria com o servidor X (separada da do Qt), usada só pela thread principal."""

    def __init__(self):
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        try:
            x = ctypes.CDLL(name)
        except OSError as e:
            raise WinFindError(f"libX11 indisponível ({e})")
        c = ctypes
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = c.c_void_p, [c.c_char_p]
        x.XCloseDisplay.argtypes = [c.c_void_p]
        x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = c.c_ulong, [c.c_void_p]
        x.XInternAtom.restype, x.XInternAtom.argtypes = c.c_ulong, [c.c_void_p, c.c_char_p, c.c_int]
        x.XGetWindowAttributes.restype = c.c_int
        x.XGetWindowAttributes.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(_Attrs)]
        x.XTranslateCoordinates.restype = c.c_int
        x.XTranslateCoordinates.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_int, c.c_int,
                                            c.POINTER(c.c_int), c.POINTER(c.c_int),
                                            c.POINTER(c.c_ulong)]
        x.XQueryTree.restype = c.c_int
        x.XQueryTree.argtypes = [c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong),
                                 c.POINTER(c.c_ulong), c.POINTER(c.POINTER(c.c_ulong)),
                                 c.POINTER(c.c_uint)]
        x.XGetWindowProperty.restype = c.c_int
        x.XGetWindowProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_long, c.c_long,
                                         c.c_int, c.c_ulong, c.POINTER(c.c_ulong),
                                         c.POINTER(c.c_int), c.POINTER(c.c_ulong),
                                         c.POINTER(c.c_ulong), c.POINTER(c.POINTER(c.c_ubyte))]
        x.XGetInputFocus.argtypes = [c.c_void_p, c.POINTER(c.c_ulong), c.POINTER(c.c_int)]
        x.XFree.argtypes = [c.c_void_p]
        x.XSync.argtypes = [c.c_void_p, c.c_int]
        x.XSetErrorHandler.restype, x.XSetErrorHandler.argtypes = c.c_void_p, [c.c_void_p]
        self.x = x
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise WinFindError("sem servidor X/XWayland (DISPLAY não definido?)")
        self.root = x.XDefaultRootWindow(self.dpy)
        self._atoms = {}
        self.errors = 0

        def on_error(_dpy, _ev):          # janela sumiu no meio da consulta: ignora, não aborta
            self.errors += 1
            return 0

        self._handler = _ErrFn(on_error)  # precisa ficar referenciado
        self._hptr = c.cast(self._handler, c.c_void_p)

    def close(self):
        if self.dpy:
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    @contextlib.contextmanager
    def quiet(self):
        """O handler padrão do Xlib encerra o processo em qualquer erro (ex.: BadWindow)."""
        old = self.x.XSetErrorHandler(self._hptr)
        try:
            yield
        finally:
            self.x.XSync(self.dpy, 0)
            self.x.XSetErrorHandler(old)

    # ------------------------------------------------------------ consultas
    def atom(self, name):
        a = self._atoms.get(name)
        if a is None:
            a = self._atoms[name] = self.x.XInternAtom(self.dpy, name.encode(), 0)
        return a

    def attrs(self, w):
        a, n0 = _Attrs(), self.errors
        ok = self.x.XGetWindowAttributes(self.dpy, w, ctypes.byref(a))
        return a if ok and self.errors == n0 else None

    def origin(self, w):
        """Canto superior esquerdo da área útil da janela, em coordenadas da TELA."""
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        ok = self.x.XTranslateCoordinates(self.dpy, w, self.root, 0, 0, ctypes.byref(x),
                                          ctypes.byref(y), ctypes.byref(child))
        return (x.value, y.value) if ok else None

    def tree(self, w):
        root, parent, kids, n = (ctypes.c_ulong(), ctypes.c_ulong(),
                                 ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint())
        if not self.x.XQueryTree(self.dpy, w, ctypes.byref(root), ctypes.byref(parent),
                                 ctypes.byref(kids), ctypes.byref(n)):
            return 0, []
        out = list(kids[:n.value]) if kids and n.value else []
        if kids:
            self.x.XFree(kids)
        return parent.value, out          # filhos em ordem de empilhamento: o último é o de cima

    def prop(self, w, name, longs=1024):
        """Valor bruto de uma propriedade: bytes (formato 8) ou lista de inteiros; None se não há."""
        at, fmt, cnt, after = (ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(),
                               ctypes.c_ulong())
        data = ctypes.POINTER(ctypes.c_ubyte)()
        r = self.x.XGetWindowProperty(self.dpy, w, self.atom(name), 0, longs, 0, 0,
                                      ctypes.byref(at), ctypes.byref(fmt), ctypes.byref(cnt),
                                      ctypes.byref(after), ctypes.byref(data))
        if r != 0 or not at.value or not data:
            if data:
                self.x.XFree(data)
            return None
        try:
            k, f = cnt.value, fmt.value
            if f == 8:
                return bytes(data[:k])
            if f == 16:
                return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ushort))[:k])
            return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:k])   # 32 = long
        finally:
            self.x.XFree(data)

    def title(self, w):
        for name in ("_NET_WM_NAME", "WM_NAME"):
            v = self.prop(w, name, 64)
            if isinstance(v, bytes) and v:
                return v.decode("utf-8", "replace")
        return ""

    def wm_class(self, w):
        v = self.prop(w, "WM_CLASS", 64)
        return " ".join(p.decode("utf-8", "replace") for p in v.split(b"\0") if p) \
            if isinstance(v, bytes) else ""

    def active_chain(self):
        """Ids considerados 'em foco': janela ativa (EWMH) e a do foco de teclado + ancestrais."""
        ids = set()
        v = self.prop(self.root, "_NET_ACTIVE_WINDOW", 1)
        if isinstance(v, list) and v and v[0]:
            ids.add(v[0])
        f, rev = ctypes.c_ulong(), ctypes.c_int()
        self.x.XGetInputFocus(self.dpy, ctypes.byref(f), ctypes.byref(rev))
        w = f.value if f.value > 1 else 0          # 0 = None, 1 = PointerRoot: não são janelas
        for _ in range(8):
            if not w or w == self.root:
                break
            ids.add(w)
            w, _kids = self.tree(w)
        return ids

    # ------------------------------------------------------------ janelas
    def geometry(self, w):
        """('ok', (x, y, largura, altura)) | ('hidden', None) minimizada/oculta | ('gone', None)."""
        a = self.attrs(w)
        if a is None:
            return "gone", None
        if a.map_state != IS_VIEWABLE:
            return "hidden", None
        o = self.origin(w)
        if o is None:
            return "gone", None
        return "ok", (o[0], o[1], a.width, a.height)

    def candidates(self, exclude_pids=()):
        """Janelas de app visíveis, da mais baixa para a mais alta na pilha."""
        active = self.active_chain()
        ignored = {self.atom(n) for n in IGNORED_TYPES}
        clients, tops = [], []
        _p, top_ids = self.tree(self.root)
        for rank, top in enumerate(top_ids):
            stack = [(top, 1)]
            while stack:
                w, depth = stack.pop()
                a = self.attrs(w)
                if a is not None and a.map_state == IS_VIEWABLE and a.cls == INPUT_OUTPUT \
                        and not a.override_redirect and a.width >= MIN_W and a.height >= MIN_H:
                    info = (w, a, rank, depth)
                    if self.prop(w, "WM_STATE", 2) is not None:
                        clients.append(info)
                    elif depth == 1:
                        tops.append(info)
                if depth < 3 and a is not None:
                    _p, kids = self.tree(w)
                    stack.extend((k, depth + 1) for k in kids)
        out = []
        for w, a, rank, _d in (clients or tops):
            pid = self.prop(w, "_NET_WM_PID", 1)
            if isinstance(pid, list) and pid and pid[0] in exclude_pids:
                continue
            types = self.prop(w, "_NET_WM_WINDOW_TYPE", 16)
            if isinstance(types, list) and ignored.intersection(types):
                continue
            title, cls = self.title(w), self.wm_class(w)
            low = (title + " " + cls).lower()
            if any(word in low for word in SELF_WORDS):
                continue
            o = self.origin(w)
            if o is None:
                continue
            out.append(Win(w, o[0], o[1], a.width, a.height, rank, title, cls, w in active))
        return out


def pick(wins, aspect=None, text=None):
    """Escolhe a janela alvo entre os candidatos (ver o texto do módulo)."""
    if text:
        t = text.lower()
        wins = [w for w in wins if t in w.title.lower() or t in w.cls.lower()]
    if not wins:
        return None

    def err(w):
        return abs(w.w / w.h - aspect) / aspect if aspect else 0.0

    if aspect:
        wins = [w for w in wins if err(w) <= ASPECT_MAX]
        if not wins:
            return None
    good = [w for w in wins if err(w) <= ASPECT_TOL]
    if good:
        return min(good, key=lambda w: (not w.active, -w.rank))
    return min(wins, key=lambda w: (err(w), not w.active, -w.rank))   # decoração/arredondamento


def find_once(aspect=None, text=None, exclude_pids=None):
    """Consulta única (ex.: para escolher a resolução de captura antes de o overlay existir).
    Devolve Win ou None; nunca levanta exceção."""
    try:
        conn = XConn()
    except WinFindError:
        return None
    try:
        with conn.quiet():
            wins = conn.candidates(exclude_pids or default_exclude())
        return pick(wins, aspect, text)
    except Exception:
        return None
    finally:
        conn.close()


def default_exclude():
    return {os.getpid(), os.getppid()}


class WindowTracker:
    """Acompanha UMA janela. poll() devolve (x, y, w, h), ou None se ela está minimizada/oculta
    ou ainda não foi encontrada. Uma vez escolhida, a janela só é trocada se for destruída."""

    def __init__(self, aspect=None, text=None, exclude_pids=None, log=print, retry=1.0):
        self.conn = XConn()
        self.aspect, self.text = aspect, text
        self.exclude = exclude_pids or default_exclude()
        self.log, self.retry = log, retry
        self.win = None                 # id da janela escolhida
        self.ever_found = False
        self.geo = None
        self._next_pick = 0.0

    def poll(self, now):
        geo = None
        with self.conn.quiet():
            if self.win is not None:
                state, geo = self.conn.geometry(self.win)
                if state == "gone":
                    self.log("winfind: a janela alvo foi fechada; procurando de novo...")
                    self.win = None
            if self.win is None and now >= self._next_pick:
                self._next_pick = now + self.retry
                w = pick(self.conn.candidates(self.exclude), self.aspect, self.text)
                if w is not None:
                    self.win = w.id
                    geo = (w.x, w.y, w.w, w.h)
                    self.log(f"winfind: janela alvo '{w.title}' [{w.cls}] "
                             f"{w.w}x{w.h}+{w.x}+{w.y}")
        if geo is not None:
            self.ever_found = True
        self.geo = geo
        return geo

    def close(self):
        self.conn.close()
