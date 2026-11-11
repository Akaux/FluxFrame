#!/usr/bin/env python3
"""
Visualizador de Frame Generation em tempo real (CPU).

Fontes:
  --video arquivo.mp4     reproduz o vídeo em tempo real
  --screen                captura a tela (Wayland: portal + PipeWire; X11: mss)

FPS: a fonte roda em --fps e o programa gera quadros extras até chegar perto de
--target-fps (60 por padrão).  --fps 30 -> x2 | --fps 20 -> x3 | --fps 15 -> x4.
Se o cálculo não cabe no tempo, a qualidade do flow cai sozinha (use --no-auto para desligar).

Modo overlay (--overlay): mostra o resultado POR CIMA da tela, com mouse/teclado atravessando a
janela (você controla o jogo normalmente). Escolha só a janela do jogo no diálogo do portal
e deixe o jogo em tela cheia "borderless" ou janelado. Sem teclado: use sinais, ex.
  Ctrl+Alt+F  liga/desliga FG   |   Ctrl+Alt+H  HUD   |   Ctrl+Alt+N  sair
  pkill -USR1 -f live.py (liga/desliga FG)    pkill -USR2 -f live.py (HUD)
Os atalhos globais usam evdev (pip install evdev; usuário no grupo input).
Detalhes visuais e click-through em overlay.py.

Benchmark (HUD e resumo ao sair):
  JOGO   fps real do jogo (quadros únicos entregues, antes do FG)
  SAIDA  fps único apresentado (originais + gerados)     ganho = SAIDA / JOGO - 1
  1% low pior caso de fluidez (é ele que mostra a "variação" que a média esconde)
  Alterne Ctrl+Alt+F durante o jogo: a linha "media FG ON | OFF" mostra o ganho medido na mesma cena.
  --bench-log bench.csv grava uma linha por segundo para você comparar sessões.
  Se o jogo entregar menos quadros que --fps, os repetidos são ignorados (não gastam CPU).

Teclas do modo janela (a janela framegen precisa estar em foco):
  F  liga/desliga a geração de quadros     H  mostra/esconde o HUD
  +/-  aumenta/diminui a janela            0  volta ao tamanho original
  Z  tela cheia                            [ ]  menos/mais qualidade do flow
  Q ou Esc  sai

Exemplos:
  python live.py --video demo.mp4
  python live.py --screen --fps 30 --width 720
  python live.py --screen --fps 20 --target-fps 60
  python live.py --screen --overlay --fps 30 --width 1280
  python live.py --screen --overlay --mult 4            # captura 15 fps -> saída 60 fps
  python live.py --screen --overlay --fps 20 --mult 3   # 20 fps -> 60 fps
  python live.py --screen --overlay --fps 30 --bench-log bench.csv --cv-threads 2
"""
import argparse
import collections
import os
import queue
import signal
import sys
import threading
import time

# O OpenCV traz Qt sem plugin Wayland: usa XWayland para a janela e evita avisos.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")

from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from bench import Bench
from framegen import (FLOW_METHODS, flow_from_gray, interpolate, set_flow_method,
                      small_gray)

WIN = "framegen"


def letterbox(img, w, h):
    """Encaixa a imagem em w x h mantendo a proporção (barras pretas nas sobras)."""
    ih, iw = img.shape[:2]
    if (iw, ih) == (w, h):
        return img
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((h, w, 3), np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = r
    return canvas


# ------------------------------------------------------------ fontes
class Source(threading.Thread):
    """Thread de captura. Guarda só o quadro mais recente: se o processamento
    atrasa, quadros antigos são descartados (e contados) em vez de acumular latência."""

    def __init__(self, args, bench=None):
        super().__init__(daemon=True)
        self.args = args
        self.bench = bench
        self._uniq = 0          # contador de quadros distintos (vídeo/mss); no portal vem do PipeWire
        self._thumb = None
        self.cond = threading.Condition()
        self.latest = None      # (frame, timestamp, uid)
        self.id = 0
        self.dropped = 0
        self.ended = False
        self.fps = args.fps
        self._consumed = True
        self.size = None
        self.ready = threading.Event()
        if args.video:
            cap = cv2.VideoCapture(args.video)
            if not cap.isOpened():
                raise SystemExit(f"Não consegui abrir {args.video}")
            self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            cap.release()

    def _resize(self, f):
        w = self.args.width
        if w and f.shape[1] > w:
            h = int(f.shape[0] * w / f.shape[1])
            f = cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
        return f

    def _next_uid(self, f):
        """Identificador do conteúdo: só muda quando o quadro é realmente diferente do anterior
        (miniatura 48x27). Vídeo e mss repetem quadros; o portal já traz o contador pronto."""
        t = cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (48, 27), interpolation=cv2.INTER_AREA)
        if self._thumb is None or cv2.absdiff(t, self._thumb).mean() > 0.25:
            self._uniq += 1
        self._thumb = t
        return self._uniq

    def _publish(self, f, uid=None):
        if uid is None:
            uid = self._next_uid(f)
        if self.bench is not None:
            self.bench.on_source(uid)
        with self.cond:
            if not self._consumed:
                self.dropped += 1
            self.latest = (f, time.perf_counter(), uid)
            self.id += 1
            self._consumed = False
            if self.size is None:
                self.size = f.shape[1::-1]
            self.cond.notify_all()
        self.ready.set()

    def get(self, last_id, timeout=2.0):
        with self.cond:
            self.cond.wait_for(lambda: self.id != last_id or self.ended, timeout)
            if self.id == last_id:
                return None, None, last_id, None
            self._consumed = True
            f, t, uid = self.latest
            return f, t, self.id, uid

    def run(self):
        try:
            self._run()
        except Exception as e:                       # erro de captura: avisa e encerra
            print(f"\nErro na captura: {e}")
            with self.cond:
                self.ended = True
                self.cond.notify_all()
            self.ready.set()                         # libera a espera do 1º quadro

    def _use_portal(self):
        if self.args.overlay:                        # só o portal captura uma janela sem pegar o overlay
            return True
        return os.environ.get("XDG_SESSION_TYPE") == "wayland" and not self.args.x11

    def _run(self):
        period = 1.0 / self.fps
        nxt = time.perf_counter()
        if self.args.video:
            cap = cv2.VideoCapture(self.args.video)
            while not self.ended:
                ok, f = cap.read()
                if not ok:                           # fim: volta ao começo
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                nxt += period
                time.sleep(max(0.0, nxt - time.perf_counter()))
                self._publish(self._resize(f))
        elif self._use_portal():
            import struct
            import subprocess
            here = os.path.dirname(os.path.abspath(__file__))
            proc = subprocess.Popen(
                [sys.executable, os.path.join(here, "capture_wayland.py"),
                 "--width", str(self.args.width), "--fps", str(self.fps),
                 *(["--overlay"] if self.args.overlay else [])],
                stdout=subprocess.PIPE)

            def read_exact(n):
                buf = proc.stdout.read(n)
                if buf is None or len(buf) < n:
                    raise RuntimeError("o processo de captura terminou (veja as mensagens acima)")
                return buf

            try:
                while not self.ended:
                    w, h, seq = struct.unpack("<IIQ", read_exact(16))
                    f = np.frombuffer(read_exact(w * h * 3), np.uint8).reshape(h, w, 3).copy()
                    self._publish(f, seq)
            finally:
                proc.terminate()
        else:
            try:
                import mss
            except ImportError:
                raise RuntimeError("Instale a captura de tela: pip install mss")
            with mss.mss() as sct:
                if self.args.region:
                    x, y, w, h = map(int, self.args.region.split(","))
                    mon = {"left": x, "top": y, "width": w, "height": h}
                else:
                    mon = sct.monitors[self.args.monitor]
                while not self.ended:
                    nxt += period
                    time.sleep(max(0.0, nxt - time.perf_counter()))
                    img = np.asarray(sct.grab(mon))[:, :, :3]
                    self._publish(self._resize(np.ascontiguousarray(img)))


# --------------------------------------------------------------- main
def sleep_until(t):
    d = t - time.perf_counter()
    if d > 0.002:
        time.sleep(d - 0.001)
    while time.perf_counter() < t:
        pass



class GlobalHotkeys:
    """Escuta atalhos globais sem consumir os eventos do teclado.

    O evdev lê os eventos diretamente dos dispositivos de entrada; não usa grab(),
    portanto Ctrl+Alt+F/H/N também continua chegando ao jogo/aplicativo em foco.
    Em Wayland, o acesso normalmente exige evdev instalado e permissão ao /dev/input.
    """

    def __init__(self, on_fg, on_hud, on_quit):
        self.on_fg = on_fg
        self.on_hud = on_hud
        self.on_quit = on_quit
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="framegen-hotkeys", daemon=True)
        self.available = False

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def _run(self):
        try:
            import select
            import evdev
            from evdev import ecodes
        except ImportError:
            print("Atalhos globais indisponíveis: instale com 'pip install evdev'.")
            return

        ctrl_codes = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL}
        alt_codes = {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT}
        action_codes = {
            ecodes.KEY_F: self.on_fg,
            ecodes.KEY_H: self.on_hud,
            ecodes.KEY_N: self.on_quit,
        }
        devices = {}
        pressed = {}
        self.available = True
        print("Atalhos globais: Ctrl+Alt+F (FG), Ctrl+Alt+H (HUD), Ctrl+Alt+N (sair).")

        warned_no_device = False
        try:
            while not self.stop_event.is_set():
                # Detecta teclados conectados depois da inicialização também.
                for path in evdev.list_devices():
                    if path in devices:
                        continue
                    try:
                        dev = evdev.InputDevice(path)
                        keys = dev.capabilities().get(ecodes.EV_KEY, [])
                        if not (ctrl_codes | alt_codes | set(action_codes)).intersection(keys):
                            dev.close()
                            continue
                        devices[path] = dev
                        pressed[path] = set()
                    except (PermissionError, OSError):
                        continue

                if not devices:
                    if not warned_no_device:
                        print("Nenhum teclado acessível via evdev; use os sinais USR1/USR2 ou verifique /dev/input.")
                        warned_no_device = True
                    self.stop_event.wait(1.0)
                    continue

                ready, _, _ = select.select([d.fd for d in devices.values()], [], [], 0.25)
                for fd in ready:
                    dev = next((d for d in devices.values() if d.fd == fd), None)
                    if dev is None:
                        continue
                    try:
                        events = list(dev.read())
                    except (OSError, RuntimeError):
                        path = dev.path
                        devices.pop(path, None)
                        pressed.pop(path, None)
                        try:
                            dev.close()
                        except OSError:
                            pass
                        continue

                    held = pressed[dev.path]
                    for event in events:
                        if event.type != ecodes.EV_KEY:
                            continue
                        if event.value == 0:  # soltou
                            held.discard(event.code)
                            continue
                        if event.value != 1:  # ignora repetição automática
                            continue
                        held.add(event.code)
                        if held.intersection(ctrl_codes) and held.intersection(alt_codes):
                            action = action_codes.get(event.code)
                            if action:
                                action()
        finally:
            for dev in devices.values():
                try:
                    dev.close()
                except OSError:
                    pass

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--video")
    g.add_argument("--screen", action="store_true")
    p.add_argument("--region", help="x,y,largura,altura (só com --screen --x11)")
    p.add_argument("--x11", action="store_true", help="força o mss mesmo no Wayland")
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--fps", type=float, default=None,
                   help="FPS de captura (--screen). Padrão: 30, ou alvo/multiplicador com --mult")
    p.add_argument("--target-fps", type=float, default=60.0, help="FPS de saída desejado")
    p.add_argument("--mult", type=int, choices=range(2, 11), metavar="2..10", default=None,
                   help="multiplicador de quadros (2x a 10x). Cada quadro do jogo vira N quadros. "
                        "Sem --fps, a captura roda em alvo/N (ex.: --mult 4 -> 15 fps -> 60 fps); "
                        "com --fps, a saída é fps*N. Seu jogo precisa rodar perto da taxa de captura")
    p.add_argument("--width", type=int, default=960, help="reduz a largura antes de processar")
    p.add_argument("--scale", type=float, default=0.25, help="escala máxima do cálculo do flow")
    p.add_argument("--no-auto", action="store_true", help="desliga a qualidade adaptativa")
    p.add_argument("--window-scale", type=float, default=1.0, help="zoom inicial da janela")
    p.add_argument("--headless", action="store_true", help="sem janela (só estatísticas)")
    p.add_argument("--overlay", action="store_true",
                   help="exibe por cima da tela, click-through (precisa de PySide6 e PyOpenGL)")
    p.add_argument("--output", type=int, default=0, help="overlay: monitor (0 = primeiro)")
    p.add_argument("--geometry", help="overlay: x,y,largura,altura (padrão: monitor inteiro)")
    p.add_argument("--sharpness", type=float, default=0.2,
                   help="overlay: nitidez RCAS (0 = máxima, 2 = suave, negativo desliga)")
    p.add_argument("--fit", choices=("fit", "stretch", "crop"), default="fit",
                   help="overlay: fit = mantém a proporção (barras pretas se diferir do monitor); "
                        "stretch = preenche a tela deformando; crop = preenche cortando as bordas")
    p.add_argument("--soft", action="store_true", help="overlay sem OpenGL (QPainter, mais lento)")
    p.add_argument("--managed", action="store_true",
                   help="overlay como janela normal (sem override-redirect), se o seu WM reclamar")
    p.add_argument("--no-global-hotkeys", action="store_true",
                   help="desativa os atalhos globais Ctrl+Alt+F/H/N (modo overlay)")
    p.add_argument("--seconds", type=float, default=0, help="duração (0 = até fechar)")
    p.add_argument("--flow", choices=FLOW_METHODS, default=None,
                   help="estimador de movimento (padrão farneback = melhor qualidade; "
                        "dis-* é bem mais rápido, com qualidade menor)")
    p.add_argument("--cv-threads", type=int, default=0,
                   help="limita as threads do OpenCV (0 = padrão). Menos threads deixam CPU "
                        "para o jogo e estabilizam o fps dele")
    p.add_argument("--bench-log", metavar="ARQ.csv",
                   help="grava uma linha por segundo (fps do jogo, saída, 1%% low...)")
    args = p.parse_args()
    if args.fps is None:
        args.fps = args.target_fps / args.mult if args.mult else 30.0
    if args.flow:
        set_flow_method(args.flow)
    if args.cv_threads > 0:
        cv2.setNumThreads(args.cv_threads)
    if args.overlay and (args.headless or args.x11):
        p.error("--overlay não combina com --headless nem --x11")
    if args.headless and not args.seconds:
        args.seconds = 8

    bench = Bench(args.bench_log)
    src = Source(args, bench)
    src.start()
    src.ready.wait(180)
    if src.size is None:
        return
    interval = 1.0 / src.fps
    factor = args.mult or max(1, round(args.target_fps / src.fps))
    L = 2 * interval - interval / factor          # latência fixa (orçamento = 1 quadro de origem)
    canvas_w, canvas_h = src.size

    fg_on = [True]
    hud_on = [True]
    qscale = [args.scale]
    proc_ms = collections.deque(maxlen=60)
    st = dict(late=0, skipped=0, stale=0, errors=0, shown=0, dup=0)
    pool = ThreadPoolExecutor(2)
    q = queue.Queue()                             # fila de saída: (hora_de_exibir, quadro, fg)
    stop = threading.Event()
    start = time.perf_counter()

    # Os sinais permitem controlar o FrameGen mesmo quando o jogo está em foco.
    # O evdev abaixo oferece os mesmos comandos por teclado, sem roubar o foco.
    def toggle_fg():
        fg_on[0] = not fg_on[0]
        bench.set_fg(fg_on[0])
        print(f"\nFrame Generation: {'ON' if fg_on[0] else 'OFF'}")

    def toggle_hud():
        hud_on[0] = not hud_on[0]
        print(f"\nHUD: {'ON' if hud_on[0] else 'OFF'}")

    def request_quit():
        stop.set()
        with src.cond:
            src.ended = True
            src.cond.notify_all()
        # SIGINT interrompe também o event loop Qt do overlay e executa o finally.
        os.kill(os.getpid(), signal.SIGINT)

    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: toggle_fg())
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, lambda *_: toggle_hud())

    hotkeys = None
    if args.overlay and not args.no_global_hotkeys:
        hotkeys = GlobalHotkeys(toggle_fg, toggle_hud, request_quit)
        hotkeys.start()

    def processor():
        """Gera os quadros intermediários e agenda a exibição.

        O jogo pode entregar menos quadros que a captura (que repete o último a taxa fixa).
        Quadro repetido é ignorado: não há movimento novo e calcular fluxo entre duas cópias
        iguais só gasta CPU que o jogo precisa. O espaço entre dois quadros únicos (gap) vira
        n = gap/intervalo * fator partes, e cada quadro gerado é agendado dentro desse espaço:
          mid_k em t_prev + L + k*gap/n   (com gap = intervalo: t_cap + L - I + k*I/fator)"""
        last_id = 0
        prev = prev_uid = prev_t = None
        while prev is None and not stop.is_set():
            prev, prev_t, last_id, prev_uid = src.get(last_id)
        if prev is not None:
            prev = letterbox(prev, canvas_w, canvas_h)
        pg = None                                  # (cinza reduzido de prev, escala) reaproveitado
        adapt_n = 0
        while not stop.is_set():
            cur, t_cap, last_id, uid = src.get(last_id)
            if cur is None:
                continue
            if uid == prev_uid:                    # o jogo não entregou quadro novo
                st["dup"] += 1
                continue
            cur = letterbox(cur, canvas_w, canvas_h)   # janela mudou de tamanho? encaixa
            gap = t_cap - prev_t                   # tempo real entre os dois quadros únicos
            n = int(round(gap / interval * factor))
            if fg_on[0] and factor > 1 and n > 1 and gap <= 3.2 * interval:
                try:
                    t0 = time.perf_counter()
                    sc = qscale[0]
                    if pg is None or pg[1] != sc:
                        pg = (small_gray(prev, sc), sc)
                    gc = small_gray(cur, sc)
                    size = (cur.shape[1], cur.shape[0])
                    f_fw = pool.submit(flow_from_gray, pg[0], gc, sc, size)
                    f_bw = pool.submit(flow_from_gray, gc, pg[0], sc, size)
                    fw, bw = f_fw.result(), f_bw.result()
                    for k in range(1, n):
                        when = prev_t + L + k * gap / n
                        if time.perf_counter() > when + 0.002:
                            st["skipped"] += 1             # já passou da hora: não adianta gerar
                            continue
                        mid, _ = interpolate(prev, cur, k / n, fw, bw, scale=sc)
                        q.put((when, mid, True))
                    pg = (gc, sc)
                    proc_ms.append((time.perf_counter() - t0) * 1000)
                except Exception as e:
                    pg = None
                    st["errors"] += 1
                    if st["errors"] <= 3:
                        print(f"Erro ao gerar quadro: {e}")
                # qualidade adaptativa: mantém o cálculo dentro do orçamento
                adapt_n += 1
                if not args.no_auto and adapt_n >= 8 and len(proc_ms) >= 8:
                    avg = sum(list(proc_ms)[-8:]) / 8
                    budget = interval * 1000
                    if avg > 0.8 * budget and qscale[0] > 0.12:
                        qscale[0] = max(0.12, qscale[0] - 0.04)
                        adapt_n = 0
                    elif avg < 0.45 * budget and qscale[0] < args.scale - 1e-6:
                        qscale[0] = min(args.scale, qscale[0] + 0.04)
                        adapt_n = 0
            else:
                pg = None
            q.put((t_cap + L, cur, fg_on[0]))
            prev, prev_uid, prev_t = cur, uid, t_cap

    # ------------------------------------------------ janela
    zoom = [args.window_scale]
    fullscreen = [False]
    win_wh = [0, 0]

    def apply_zoom():
        cv2.resizeWindow(WIN, int(canvas_w * zoom[0]), int(canvas_h * zoom[0]))

    if not args.headless and not args.overlay:
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL)
        apply_zoom()

    def hud_lines(now, label_on):
        game, out = bench.game_fps(now), bench.out_fps(now)
        gain = f"{(out / game - 1) * 100:+.0f}%" if game >= 1 else "--"
        l1 = (f"JOGO {game:3.0f} fps real | SAIDA {out:3.0f} fps | "
              f"FG {'ON x' + str(factor) if label_on else 'OFF'} | ganho {gain}")
        pc = bench.pacing(now)
        l2 = ("1% low --" if pc is None else
              f"1% low {pc[0]:3.0f} fps | frametime {pc[1]:4.1f} ms (pico {pc[2]:4.1f} ms)")
        avg = np.mean(proc_ms) if proc_ms else 0
        l3 = (f"proc {avg:.0f}/{interval * 1000:.0f} ms | escala {qscale[0]:.2f} | "
              f"atrasos {st['late']} pulos {st['skipped']} repetidos {st['dup']} "
              f"descartes {src.dropped}")
        lines = [l1, l2, l3]
        on, off = bench.state_avg(True, now), bench.state_avg(False, now)
        if on is not None and off is not None:
            lines.append(f"media FG ON {on:.0f} | OFF {off:.0f} fps | medido {(on / off - 1) * 100:+.0f}%")
        return lines

    def show(frame, label_on):
        now = time.perf_counter()
        st["shown"] += 1
        bench.present(now, label_on)
        if args.headless:
            return -1
        if st["shown"] % 15 == 1:                       # tamanho atual da janela
            _, _, ww, wh = cv2.getWindowImageRect(WIN)
            win_wh[:] = [ww, wh]
        view = frame
        if win_wh[0] > 0 and win_wh[1] > 0:
            view = letterbox(frame, win_wh[0], win_wh[1])   # mantém a proporção ao redimensionar
        if hud_on[0]:
            view = view.copy()
            for i, txt in enumerate(hud_lines(now, label_on)):
                y = 22 + i * 20
                cv2.putText(view, txt, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
                cv2.putText(view, txt, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        cv2.imshow(WIN, view)
        return cv2.waitKey(1) & 0xFF

    def handle_key(key):
        if key in (ord("f"), ord("F")):
            toggle_fg()
        elif key in (ord("h"), ord("H")):
            hud_on[0] = not hud_on[0]
        elif key in (ord("+"), ord("="), ord("-"), ord("_"), ord("0")):
            zoom[0] = 1.0 if key == ord("0") else max(0.25, min(4.0, zoom[0] * (
                1.15 if key in (ord("+"), ord("=")) else 1 / 1.15)))
            if fullscreen[0]:
                return
            apply_zoom()
            win_wh[:] = [0, 0]
            st["shown"] = 0                              # força reler o tamanho da janela
        elif key in (ord("z"), ord("Z")):
            fullscreen[0] = not fullscreen[0]
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN if fullscreen[0] else cv2.WINDOW_NORMAL)
            if not fullscreen[0]:
                apply_zoom()
            st["shown"] = 0
        elif key == ord("["):
            qscale[0] = max(0.1, qscale[0] - 0.05)
            args.scale = max(args.scale, qscale[0]) if args.no_auto else args.scale
        elif key == ord("]"):
            qscale[0] = min(0.6, qscale[0] + 0.05)
            args.scale = max(args.scale, qscale[0])      # o ajuste manual vira o novo teto

    proc_thread = threading.Thread(target=processor, daemon=True)
    proc_thread.start()

    def bench_loop():
        """Uma linha por segundo: CSV (--bench-log) e, no modo headless, status no terminal."""
        while not stop.wait(1.0):
            now = time.perf_counter()
            avg = float(np.mean(proc_ms)) if proc_ms else 0.0
            bench.log_row(now, proc_ms=f"{avg:.1f}", escala=f"{qscale[0]:.2f}",
                          pulos=st["skipped"], repetidos=st["dup"], atrasos=st["late"])
            if args.headless:
                print(" | ".join(hud_lines(now, fg_on[0])[:3]))

    threading.Thread(target=bench_loop, daemon=True).start()

    def window_loop():
        while not stop.is_set():
            if args.seconds and time.perf_counter() - start > args.seconds:
                break
            try:
                when, frame, on = q.get(timeout=0.5)
            except queue.Empty:
                if src.ended:
                    break
                continue
            now = time.perf_counter()
            if now - when > interval:                    # muito velho: descarta em vez de acumular
                st["stale"] += 1
                continue
            if now > when + 0.004:
                st["late"] += 1
            sleep_until(when)
            key = show(frame, on)
            if key in (ord("q"), 27):
                break
            if key != 255 and key != -1:
                handle_key(key)

    try:
        if args.overlay:
            from overlay import run_overlay          # Qt no thread principal; o resto roda nos threads
            run_overlay(q, src.size, st, bench.present, hud_lines, hud_on, toggle_fg,
                        output=args.output, geometry=args.geometry, soft=args.soft,
                        managed=args.managed, sharpness=args.sharpness, seconds=args.seconds,
                        fit=args.fit)
        else:
            window_loop()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        bench.close()
        if hotkeys is not None:
            hotkeys.stop()
        with src.cond:
            src.ended = True
            src.cond.notify_all()
        proc_thread.join(3)                      # espera as threads terminarem antes de sair
        src.join(3)
        pool.shutdown(wait=True)
        if not args.headless and not args.overlay:
            cv2.destroyAllWindows()

    dur = time.perf_counter() - start
    print(f"\nDuração {dur:.1f}s | proc médio {np.mean(proc_ms) if proc_ms else 0:.0f} ms "
          f"(orçamento {interval * 1000:.0f} ms) | escala final {qscale[0]:.2f} | "
          f"atrasos {st['late']} | pulos {st['skipped']} | repetidos {st['dup']} | "
          f"velhos {st['stale']} | erros {st['errors']} | descartes {src.dropped} | "
          f"resolução {src.size}")
    print("--- Benchmark ---")
    for line in bench.summary():
        print(line)


if __name__ == "__main__":
    main()