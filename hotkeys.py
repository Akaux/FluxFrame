#!/usr/bin/env python3
"""
Atalhos globais do overlay. Roda como processo filho do live.py e manda sinais ao pai.

  Ctrl+Alt+N  fecha o framegen (SIGTERM)
  Ctrl+Alt+F  liga/desliga a geração de quadros (SIGUSR1)
  Ctrl+Alt+H  mostra/esconde o HUD (SIGUSR2)

Uma janela click-through nunca recebe teclado, e o Wayland não deixa um app comum capturar
teclas globais. Dois caminhos, nesta ordem:
  1. evdev: lê /dev/input direto. Funciona em qualquer compositor, mesmo com o jogo em foco.
     Precisa do pacote python-evdev e do seu usuário no grupo 'input':
         sudo pacman -S python-evdev   (ou: pip install evdev)
         sudo usermod -aG input $USER  (e reiniciar a sessão)
  2. Portal GlobalShortcuts (KDE Plasma 6, GNOME 48+, Hyprland), via D-Bus.
Se nenhum funcionar, ligue um atalho do seu ambiente a:  pkill -TERM -f live.py
"""
import os
import signal
import sys
import threading
import time

PARENT = os.getppid()
# id -> (tecla, sinal enviado ao live.py)
ACTIONS = {
    "quit": ("n", signal.SIGTERM),
    "fg": ("f", signal.SIGUSR1),
    "hud": ("h", signal.SIGUSR2),
}
DESC = {"quit": "Fechar o framegen", "fg": "Ligar/desligar a geração de quadros",
        "hud": "Mostrar/esconder o HUD"}


def fire(action):
    try:
        os.kill(PARENT, ACTIONS[action][1])
    except ProcessLookupError:
        sys.exit(0)


def parent_alive():
    return os.getppid() == PARENT


# ------------------------------------------------------------------ evdev
def run_evdev():
    import selectors

    import evdev
    from evdev import ecodes as e

    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        keys = d.capabilities().get(e.EV_KEY, [])
        if e.KEY_N in keys and e.KEY_LEFTCTRL in keys:        # é um teclado de verdade
            devs.append(d)
    if not devs:
        raise RuntimeError("nenhum teclado legível em /dev/input (usuário no grupo 'input'?)")

    keymap = {getattr(e, f"KEY_{k.upper()}"): a for a, (k, _) in ACTIONS.items()}
    ctrl, alt = {e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL}, {e.KEY_LEFTALT, e.KEY_RIGHTALT}
    sel = selectors.DefaultSelector()
    for d in devs:
        sel.register(d, selectors.EVENT_READ)
    print(f"hotkeys: evdev em {len(devs)} teclado(s). Ctrl+Alt+N fecha.", file=sys.stderr)

    held = set()
    while parent_alive():
        for key, _ in sel.select(timeout=1.0):
            dev = key.fileobj
            try:
                events = list(dev.read())
            except OSError:                                    # teclado desconectado
                sel.unregister(dev)
                continue
            for ev in events:
                if ev.type != e.EV_KEY:
                    continue
                if ev.value:
                    held.add(ev.code)
                else:
                    held.discard(ev.code)
                if (ev.value == 1 and ev.code in keymap
                        and held & ctrl and held & alt):
                    fire(keymap[ev.code])


# ----------------------------------------------------------------- portal
def run_portal():
    import itertools

    import gi
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
    portal = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None,
        "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.GlobalShortcuts", None)
    counter = itertools.count(1)

    def request(method, build, options):
        token = f"fghk{next(counter)}"
        options = dict(options, handle_token=GLib.Variant("s", token))
        sender = bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        ev, out = threading.Event(), {}

        def on_resp(conn, sender_name, obj, iface, sig, params, *a):
            out["code"], out["results"] = params.unpack()
            ev.set()

        sid = bus.signal_subscribe("org.freedesktop.portal.Desktop",
                                   "org.freedesktop.portal.Request", "Response", path, None,
                                   Gio.DBusSignalFlags.NONE, on_resp)
        try:
            portal.call_sync(method, build(options), Gio.DBusCallFlags.NONE, -1, None)
            if not ev.wait(60):
                raise RuntimeError(f"tempo esgotado no portal ({method})")
        finally:
            bus.signal_unsubscribe(sid)
        if out["code"] != 0:
            raise RuntimeError(f"o portal recusou ({method})")
        return out["results"]

    res = request("CreateSession", lambda o: GLib.Variant("(a{sv})", (o,)),
                  {"session_handle_token": GLib.Variant("s", "framegen_hotkeys")})
    session = res["session_handle"]

    def on_activated(conn, sender, path, iface, sig, params, *a):
        _, shortcut_id, _, _ = params.unpack()
        if shortcut_id in ACTIONS:
            fire(shortcut_id)

    bus.signal_subscribe("org.freedesktop.portal.Desktop",
                         "org.freedesktop.portal.GlobalShortcuts", "Activated",
                         "/org/freedesktop/portal/desktop", None,
                         Gio.DBusSignalFlags.NONE, on_activated)
    shortcuts = [(a, {"description": GLib.Variant("s", DESC[a]),
                      "preferred_trigger": GLib.Variant("s", f"CTRL+ALT+{k}")})
                 for a, (k, _) in ACTIONS.items()]
    request("BindShortcuts",
            lambda o: GLib.Variant("(oa(sa{sv})sa{sv})", (session, shortcuts, "", o)), {})
    print("hotkeys: portal GlobalShortcuts. Se o atalho não funcionar, confira "
          "Configurações > Atalhos.", file=sys.stderr)
    while parent_alive():
        time.sleep(1.0)


def main():
    erros = []
    for nome, fn in (("evdev", run_evdev), ("portal", run_portal)):
        try:
            return fn()
        except Exception as ex:
            erros.append(f"{nome}: {ex}")
    print("hotkeys: atalhos globais indisponíveis:\n  " + "\n  ".join(erros) +
          "\n  Use um atalho do sistema com:  pkill -TERM -f live.py", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
