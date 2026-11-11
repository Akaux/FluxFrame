"""
Captura de tela no Wayland: xdg-desktop-portal (ScreenCast) + PipeWire + GStreamer.

O portal mostra um diálogo do sistema para você escolher uma tela ou janela
(por isso não há --region neste modo). Escolher só a janela do jogo/vídeo evita
que o visualizador capture a si mesmo.
"""
import itertools
import sys
import threading

import numpy as np

AJUDA = (
    "Dependências da captura no Wayland ausentes. Instale:\n"
    "  sudo pacman -S python-gobject gst-plugin-pipewire gst-plugins-base gst-plugins-good\n"
    "  sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg\n"
    "e confirme o portal do seu ambiente: xdg-desktop-portal-kde, -gnome, -hyprland ou -wlr."
)


class PortalCapture:
    def __init__(self, width=960, timeout=120, overlay=False):
        self.width = width
        self.timeout = timeout
        self.overlay = overlay
        self._frame = None
        self._seq = 0
        self._lock = threading.Condition()
        self._n = itertools.count(1)
        self.pipeline = None

    # ---------------------------------------------------------- portal
    def _request(self, method, build, options):
        GLib, Gio = self._GLib, self._Gio
        token = f"fgreq{next(self._n)}"
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        ev, out = threading.Event(), {}

        def on_resp(conn, sender_name, obj, iface, sig, params, *a):
            out["code"], out["results"] = params.unpack()
            ev.set()

        sid = self.bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", path, None, Gio.DBusSignalFlags.NONE, on_resp)
        try:
            self.portal.call_sync(method, build(options), Gio.DBusCallFlags.NONE, -1, None)
            if not ev.wait(self.timeout):
                raise RuntimeError(f"Tempo esgotado esperando o portal ({method})")
        finally:
            self.bus.signal_unsubscribe(sid)
        if out["code"] != 0:
            raise RuntimeError(f"O portal recusou ou você cancelou ({method})")
        return out["results"]

    def start(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gio, GLib, Gst
        except (ImportError, ValueError) as e:
            raise RuntimeError(f"{e}\n{AJUDA}")
        self._Gio, self._GLib, self._Gst = Gio, GLib, Gst
        Gst.init(None)

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
        self.portal = Gio.DBusProxy.new_sync(
            self.bus, Gio.DBusProxyFlags.NONE, None,
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast", None)

        print("Escolha a tela ou janela no diálogo do sistema...", file=sys.stderr)
        res = self._request(
            "CreateSession", lambda o: GLib.Variant("(a{sv})", (o,)),
            {"session_handle_token": GLib.Variant("s", "framegen")})
        session = res["session_handle"]

        self._request(
            "SelectSources", lambda o: GLib.Variant("(oa{sv})", (session, o)),
            {"types": GLib.Variant("u", 2 if self.overlay else 3),
             "multiple": GLib.Variant("b", False),
             "cursor_mode": GLib.Variant("u", 1 if self.overlay else 2)})
        res = self._request(
            "Start", lambda o: GLib.Variant("(osa{sv})", (session, "", o)), {})
        node = res["streams"][0][0]

        result, fds = self.portal.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (session, {})),
            Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fds.get(result.unpack()[0])

        # Se width <= 0, não força escala no GStreamer (deixa o tamanho nativo).
        pipeline_str = f"pipewiresrc fd={fd} path={node} do-timestamp=true ! "
        if self.width > 0:
            pipeline_str += f"videoscale ! videoconvert ! video/x-raw,format=BGR,width={self.width} ! "
        else:
            pipeline_str += "videoconvert ! video/x-raw,format=BGR ! "
        pipeline_str += "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        
        self.pipeline = Gst.parse_launch(pipeline_str)
        self.pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        self.pipeline.set_state(Gst.State.PLAYING)

    # ---------------------------------------------------------- quadros
    def _on_sample(self, sink):
        Gst = self._Gst
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = np.frombuffer(info.data, np.uint8)
                stride = data.size // h
                frame = data.reshape(h, stride)[:, : w * 3].reshape(h, w, 3).copy()
            finally:
                buf.unmap(info)
            with self._lock:
                self._frame = frame
                self._seq += 1
                self._lock.notify_all()
        return Gst.FlowReturn.OK

    def latest(self):
        with self._lock:
            return self._frame

    def latest_with_seq(self):
        with self._lock:
            return self._frame, self._seq

    def wait_new(self, last_seq, timeout=1.0):
        with self._lock:
            self._lock.wait_for(lambda: self._seq != last_seq and self._frame is not None, timeout)
            if self._seq == last_seq or self._frame is None:
                return None, last_seq
            return self._frame, self._seq

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(self._Gst.State.NULL)


if __name__ == "__main__":
    import argparse
    import struct
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--fps", type=float, default=0.0)
    ap.add_argument("--overlay", action="store_true")
    a = ap.parse_args()
    cap = PortalCapture(a.width, overlay=a.overlay)
    try:
        cap.start()
    except RuntimeError as e:
        print(f"Erro: {e}", file=sys.stderr)
        sys.exit(1)

    out = sys.stdout.buffer
    disable_throttle = (a.fps <= 0 or a.fps >= 1000)
    period = 1.0 / a.fps if not disable_throttle else 0.0
    last_seq, last_sent, first = -1, 0.0, True
    
    try:
        while True:
            f, seq = cap.wait_new(last_seq, 1.0)
            if f is None:
                continue
            if not disable_throttle:
                wait = last_sent + 0.9 * period - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                    f, seq = cap.latest_with_seq()
            last_seq = seq
            if first:
                first = False
                print(f"Primeiro quadro: {f.shape[1]}x{f.shape[0]}, brilho médio {f.mean():.1f}"
                      " (0 = preto)", file=sys.stderr)
            out.write(struct.pack("<IIQ", f.shape[1], f.shape[0], seq))
            out.write(memoryview(f))
            out.flush()
            last_sent = time.perf_counter()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        cap.stop()