#!/usr/bin/env python3
"""
Visualizador de Frame Generation em tempo real (CPU).

Fontes:
  --video arquivo.mp4     reproduz o vídeo em tempo real (funciona em qualquer sessão)
  --screen                captura a tela (Wayland: portal + PipeWire; X11: mss)

Teclas na janela:  F = liga/desliga a geração de quadros   Q/Esc = sair

Exemplos:
  python live.py --video demo.mp4
  python live.py --video gameplay.mp4 --width 960 --scale 0.25
  python live.py --screen --fps 30              # Wayland: escolha a tela/janela no diálogo
  python live.py --screen --x11 --region 0,0,1280,720   # força mss (só X11)
"""
import argparse
import os

# O OpenCV traz Qt sem plugin Wayland: usa XWayland para a janela e evita avisos.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")
import collections
import queue
import threading
import time

import cv2
import numpy as np

from concurrent.futures import ThreadPoolExecutor

from framegen import interpolate, estimate_flow


# ------------------------------------------------------------ fontes
class Source(threading.Thread):
    """Thread de captura. Guarda só o quadro mais recente: se o processamento
    atrasa, quadros antigos são descartados (e contados) em vez de acumular latência."""

    def __init__(self, args):
        super().__init__(daemon=True)
        self.args = args
        self.cond = threading.Condition()
        self.latest = None      # (frame, timestamp)
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

    def _publish(self, f):
        with self.cond:
            if not self._consumed:
                self.dropped += 1
            self.latest = (f, time.perf_counter())
            self.id += 1
            self._consumed = False
            self.size = f.shape[1::-1]
            self.cond.notify_all()
        self.ready.set()

    def get(self, last_id, timeout=2.0):
        with self.cond:
            self.cond.wait_for(lambda: self.id != last_id or self.ended, timeout)
            if self.id == last_id:
                return None, None, last_id
            self._consumed = True
            f, t = self.latest
            return f, t, self.id

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
        return os.environ.get("XDG_SESSION_TYPE") == "wayland" and not self.args.x11

    def _run(self):
        period = 1.0 / self.fps
        nxt = time.perf_counter()
        if self.args.video:
            cap = cv2.VideoCapture(self.args.video)
            while not self.ended:
                ok, f = cap.read()
                if not ok:                       # fim: volta ao começo
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                nxt += period
                time.sleep(max(0.0, nxt - time.perf_counter()))
                self._publish(self._resize(f))
        elif self._use_portal():
            import struct
            import subprocess
            import sys
            here = os.path.dirname(os.path.abspath(__file__))
            proc = subprocess.Popen(
                [sys.executable, os.path.join(here, "capture_wayland.py"),
                 "--width", str(self.args.width), "--fps", str(self.fps)],
                stdout=subprocess.PIPE)

            def read_exact(n):
                buf = proc.stdout.read(n)
                if buf is None or len(buf) < n:
                    raise RuntimeError("o processo de captura terminou (veja as mensagens acima)")
                return buf

            try:
                while not self.ended:
                    w, h = struct.unpack("<II", read_exact(8))
                    f = np.frombuffer(read_exact(w * h * 3), np.uint8).reshape(h, w, 3).copy()
                    self._publish(f)
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


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--video")
    g.add_argument("--screen", action="store_true")
    p.add_argument("--region", help="x,y,largura,altura (use com --screen)")
    p.add_argument("--x11", action="store_true", help="força o mss mesmo no Wayland")
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--fps", type=float, default=30.0, help="FPS de captura (--screen)")
    p.add_argument("--width", type=int, default=960, help="reduz a largura antes de processar")
    p.add_argument("--scale", type=float, default=0.25, help="escala do cálculo do flow")
    p.add_argument("--headless", action="store_true", help="sem janela (só estatísticas)")
    p.add_argument("--seconds", type=float, default=0, help="duração (0 = até fechar)")
    args = p.parse_args()
    if args.headless and not args.seconds:
        args.seconds = 8

    src = Source(args)
    src.start()
    src.ready.wait(180)
    interval = 1.0 / src.fps

    if not args.headless:
        cv2.namedWindow("framegen", cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL)
        if src.size:
            cv2.resizeWindow("framegen", *src.size)

    fg_on = [True]
    shown = collections.deque(maxlen=240)     # timestamps das exibições
    proc_ms = collections.deque(maxlen=60)
    late = 0
    n_shown = 0
    pool = ThreadPoolExecutor(2)
    q = queue.Queue()                         # fila de saída: (hora_de_exibir, quadro, fg)
    stop = threading.Event()
    start = time.perf_counter()

    def processor():
        """Thread de processamento: gera o quadro intermediário e agenda a exibição.
        Latência fixa de 1.5 quadros de origem: mid em +1.0I, cur em +1.5I."""
        last_id = 0
        prev = None
        while prev is None and not stop.is_set():
            prev, _, last_id = src.get(last_id)
        while not stop.is_set():
            cur, t_cap, last_id = src.get(last_id)
            if cur is None:
                continue
            if fg_on[0]:
                t0 = time.perf_counter()
                f_fw = pool.submit(estimate_flow, prev, cur, args.scale)
                f_bw = pool.submit(estimate_flow, cur, prev, args.scale)
                mid, _ = interpolate(prev, cur, 0.5, f_fw.result(), f_bw.result(),
                                     scale=args.scale)
                proc_ms.append((time.perf_counter() - t0) * 1000)
                q.put((t_cap + interval, mid, True))
            q.put((t_cap + 1.5 * interval, cur, fg_on[0]))
            prev = cur

    def show(frame, label_on):
        nonlocal n_shown
        now = time.perf_counter()
        n_shown += 1
        shown.append(now)
        out_fps = sum(1 for t in shown if now - t <= 1.0)
        if not args.headless:
            f = frame.copy()
            txt = (f"FG {'ON ' if label_on else 'OFF'} | saida {out_fps} fps | fonte {src.fps:.0f} fps"
                   f" | flow {np.mean(proc_ms) if proc_ms else 0:.0f} ms | atrasos {late} | descartes {src.dropped}")
            cv2.putText(f, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
            cv2.putText(f, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
            cv2.imshow("framegen", f)
            return cv2.waitKey(1) & 0xFF
        return -1

    threading.Thread(target=processor, daemon=True).start()

    try:
        while True:
            if args.seconds and time.perf_counter() - start > args.seconds:
                break
            try:
                when, frame, on = q.get(timeout=0.5)
            except queue.Empty:
                if src.ended:
                    break
                continue
            if time.perf_counter() > when + 0.004:
                late += 1
            sleep_until(when)
            key = show(frame, on)
            if key in (ord("q"), 27):
                break
            if key in (ord("f"), ord("F")):
                fg_on[0] = not fg_on[0]
    finally:
        stop.set()
        src.ended = True
        if not args.headless:
            cv2.destroyAllWindows()

    dur = time.perf_counter() - start
    print(f"\nDuração {dur:.1f}s | exibições {n_shown} | "
          f"saída ~{n_shown / dur:.0f} fps (fonte {src.fps:.0f}) | "
          f"flow médio {np.mean(proc_ms) if proc_ms else 0:.0f} ms (orçamento {interval * 1000:.0f} ms) | "
          f"atrasos {late} | descartes {src.dropped} | resolução {src.size}")


if __name__ == "__main__":
    main()