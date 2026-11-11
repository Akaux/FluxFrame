#!/usr/bin/env python3
"""
Visualizador de Frame Generation em tempo real (CPU/GPU).
"""
import argparse
import collections
import os
import queue
import signal
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts")

from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from bench import Bench
from framegen import (FLOW_METHODS, GenRef, flow_from_gray, interpolate, set_flow_method,
                      small_gray)

WIN = "framegen"


def letterbox(img, w, h):
    ih, iw = img.shape[:2]
    if (iw, ih) == (w, h):
        return img
    if abs((iw / ih) / (w / h) - 1.0) < 0.03:
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA if w < iw else cv2.INTER_LINEAR)
    s = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * s)), max(1, int(ih * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    canvas = np.zeros((h, w, 3), np.uint8)
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = r
    return canvas


class Source(threading.Thread):
    def __init__(self, args, bench=None):
        super().__init__(daemon=True)
        self.args = args
        self.bench = bench
        self._uniq = 0
        self._thumb = None
        self.cond = threading.Condition()
        self.latest = None
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
        except Exception as e:
            print(f"\nErro na captura: {e}")
            with self.cond:
                self.ended = True
                self.cond.notify_all()
            self.ready.set()

    def _use_portal(self):
        if self.args.overlay:
            return True
        return os.environ.get("XDG_SESSION_TYPE") == "wayland" and not self.args.x11

    def _run(self):
        period = 1.0 / self.fps
        nxt = time.perf_counter()
        if self.args.video:
            cap = cv2.VideoCapture(self.args.video)
            while not self.ended:
                ok, f = cap.read()
                if not ok:
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
                    raise RuntimeError("o processo de captura terminou")
                return buf

            try:
                while not self.ended:
                    w, h, seq = struct.unpack("<IIQ", read_exact(16))
                    f = np.frombuffer(read_exact(w * h * 3), np.uint8).reshape(h, w, 3)
                    self._publish(f, seq)
            finally:
                proc.terminate()
        else:
            import mss
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


def sleep_until(t):
    d = t - time.perf_counter()
    if d > 0.002:
        time.sleep(d - 0.001)
    while time.perf_counter() < t:
        pass


class GlobalHotkeys:
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
        last_scan = -1e9
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - last_scan >= (3.0 if devices else 1.0):
                    last_scan = now
                    for path in evdev.list_devices():
                        if path in devices:
                            continue
                        try:
                            dev = evdev.InputDevice(path)
                            keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
                            if not ({ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT,
                                     ecodes.KEY_F, ecodes.KEY_H, ecodes.KEY_N} <= keys):
                                dev.close()
                                continue
                            devices[path] = dev
                            pressed[path] = set()
                        except (PermissionError, OSError):
                            continue

                if not devices:
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
                        devices.pop(dev.path, None)
                        pressed.pop(dev.path, None)
                        continue

                    held = pressed[dev.path]
                    for event in events:
                        if event.type != ecodes.EV_KEY:
                            continue
                        if event.value == 0:
                            held.discard(event.code)
                            continue
                        if event.value != 1:
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
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--video")
    g.add_argument("--screen", action="store_true")
    p.add_argument("--region")
    p.add_argument("--x11", action="store_true")
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--fps", type=float, default=None)
    p.add_argument("--target-fps", type=float, default=0.0)
    p.add_argument("--mult", type=int, choices=range(2, 11), metavar="2..10", default=None)
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--scale", type=float, default=0.25)
    p.add_argument("--no-auto", action="store_true")
    p.add_argument("--window-scale", type=float, default=1.0)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--overlay", action="store_true")
    p.add_argument("--output", type=int, default=0)
    p.add_argument("--geometry")
    p.add_argument("--sharpness", type=float, default=0.2)
    p.add_argument("--fit", choices=("auto", "fit", "stretch", "crop"), default="auto")
    p.add_argument("--soft", action="store_true")
    p.add_argument("--managed", action="store_true")
    p.add_argument("--no-global-hotkeys", action="store_true")
    p.add_argument("--seconds", type=float, default=0)
    p.add_argument("--flow", choices=FLOW_METHODS, default=None)
    p.add_argument("--cv-threads", type=int, default=2)
    p.add_argument("--nice", type=int, default=5)
    p.add_argument("--single-flow", action="store_true")
    p.add_argument("--cpu-interp", action="store_true")
    p.add_argument("--bench-log", metavar="ARQ.csv")
    p.add_argument("--fsr-mode", choices=("fsr1", "fsr4"), default="fsr1")
    p.add_argument("--scale-factor", type=int, default=1)
    args = p.parse_args()

    fixed_target = args.target_fps > 0
    if args.fps is None:
        args.fps = (args.target_fps / args.mult) if (fixed_target and args.mult) else 30.0

    if args.flow:
        set_flow_method(args.flow)
    if args.cv_threads > 0:
        cv2.setNumThreads(args.cv_threads)
    if args.nice > 0:
        try:
            os.nice(args.nice)
        except OSError:
            pass

    bench = Bench(args.bench_log)
    src = Source(args, bench)
    src.start()
    src.ready.wait(180)
    if src.size is None:
        return
    
    interval = 1.0 / src.fps
    factor = args.mult or max(1, round(args.target_fps / src.fps) if fixed_target else 2)
    if args.fsr_mode == "fsr4" and args.scale_factor > 1:
        factor *= args.scale_factor

    L = 2 * interval - interval / factor
    canvas_w, canvas_h = src.size

    fg_on = [True]
    hud_on = [True]
    qscale = [args.scale]
    proc_ms = collections.deque(maxlen=60)
    st = dict(late=0, skipped=0, stale=0, errors=0, shown=0, dup=0)
    gpu_interp = [bool(args.overlay and not args.soft and not args.cpu_interp)]
    pool = ThreadPoolExecutor(1 if args.single_flow else 2)
    q = queue.Queue()
    stop = threading.Event()
    start = time.perf_counter()

    def toggle_fg():
        fg_on[0] = not fg_on[0]
        bench.set_fg(fg_on[0])

    def toggle_hud():
        hud_on[0] = not hud_on[0]

    def request_quit():
        stop.set()
        with src.cond:
            src.ended = True
            src.cond.notify_all()
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
        last_id = 0
        prev = prev_uid = prev_t = None
        while prev is None and not stop.is_set():
            prev, prev_t, last_id, prev_uid = src.get(last_id)
        if prev is not None:
            prev = letterbox(prev, canvas_w, canvas_h)
        pg = None
        adapt_n = 0
        carry = 0.0
        while not stop.is_set():
            cur, t_cap, last_id, uid = src.get(last_id)
            if cur is None:
                continue
            if uid == prev_uid:
                st["dup"] += 1
                continue
            cur = letterbox(cur, canvas_w, canvas_h)
            gap = t_cap - prev_t
            n = int(round(gap / interval * factor))
            want = n
            if fixed_target:
                want = gap * args.target_fps + carry
                n = max(1, int(round(want)))
            eligible = fg_on[0] and factor > 1 and gap <= 3.2 * interval
            carry = max(-1.0, min(1.0, want - n)) if (fixed_target and eligible) else 0.0
            if eligible and n > 1:
                try:
                    t0 = time.perf_counter()
                    sc = qscale[0]
                    if pg is None or pg[1] != sc:
                        pg = (small_gray(prev, sc), sc)
                    gc = small_gray(cur, sc)
                    use_gpu = gpu_interp[0]
                    size = None if use_gpu else (cur.shape[1], cur.shape[0])
                    f_fw = pool.submit(flow_from_gray, pg[0], gc, sc, size)
                    if args.single_flow:
                        fw = f_fw.result()
                        bw = -fw
                    else:
                        f_bw = pool.submit(flow_from_gray, gc, pg[0], sc, size)
                        fw, bw = f_fw.result(), f_bw.result()
                    for k in range(1, n):
                        when = prev_t + L + k * gap / n
                        if time.perf_counter() > when + 0.002:
                            st["skipped"] += 1
                            continue
                        if use_gpu:
                            q.put((when, GenRef(prev, cur, fw, bw, k / n), True))
                        else:
                            mid, _ = interpolate(prev, cur, k / n, fw, bw, scale=sc)
                            q.put((when, mid, True))
                    pg = (gc, sc)
                    proc_ms.append((time.perf_counter() - t0) * 1000)
                except Exception as e:
                    pg = None
                    st["errors"] += 1
            else:
                pg = None
            q.put((t_cap + L, cur, fg_on[0]))
            prev, prev_uid, prev_t = cur, uid, t_cap

    proc_thread = threading.Thread(target=processor, daemon=True)
    proc_thread.start()

    try:
        if args.overlay:
            from overlay import run_overlay
            run_overlay(q, src.size, st, bench.present, lambda n, l: [
                f"JOGO {bench.game_fps(n):3.0f} fps | SAIDA REAL {bench.out_fps(n):3.0f} fps [UNLOCKED]",
                f"Frame Generation: {'ON (x' + str(factor) + ')' if l else 'OFF'} | FSR: {args.fsr_mode.upper()}"
            ], hud_on, toggle_fg,
                        output=args.output, geometry=args.geometry, soft=args.soft,
                        managed=args.managed, sharpness=args.sharpness, seconds=args.seconds,
                        fit=args.fit, gpu_interp=gpu_interp)
        else:
            while not stop.is_set():
                time.sleep(1.0)
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
        proc_thread.join(3)
        src.join(3)
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()