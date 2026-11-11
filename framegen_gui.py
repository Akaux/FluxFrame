#!/usr/bin/env python3
"""Framegen Control: painel minimalista (tema escuro) para o live.py no KDE Plasma / Arch.

Um botão inicia e para a geração de quadros. O multiplicador (2x a 10x) e o FPS alvo
(60 ou ilimitado) são traduzidos para as opções --mult / --fps / --target-fps do live.py.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget)

APP_DIR = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "framegen-gui"
LOG = CACHE / "ultima-execucao.log"
TARGETS = (30, 45, 60, 75, 90, 120, 144, 165, 240, 0)    # 0 = ilimitado; padrão 60
START_MARKERS = ("Primeiro quadro", "SAIDA", "FG ON")     # linhas que o live.py imprime ao rodar


# ------------------------------------------------------------------ lógica
def build_args(cfg):
    """Opções do live.py para a configuração do painel."""
    args = ["--screen", "--overlay", "--mult", str(cfg["mult"]),
            "--width", str(cfg["width"]), "--output", str(cfg["monitor"]),
            "--fit", cfg["fit"]]
    if cfg["unlimited"]:
        args += ["--fps", str(cfg["capture_fps"])]          # saída = fps de captura x mult
    else:
        args += ["--target-fps", str(cfg["target"])]        # captura = alvo / mult
    return args


def rates(cfg):
    """(fps de captura, fps de saída, latência em ms). Latência: L = 2*I - I/f (README)."""
    mult = cfg["mult"]
    capture = float(cfg["capture_fps"]) if cfg["unlimited"] else cfg["target"] / mult
    interval = 1.0 / capture
    return capture, capture * mult, (2 * interval - interval / mult) * 1000


def fmt(x):
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def find_folder(saved):
    for c in (saved, APP_DIR, APP_DIR / "framegen", Path.cwd()):
        if c and (Path(c) / "live.py").is_file():
            return Path(c)
    return None


class Runner:
    """Executa o live.py em uma sessão própria (grupo de processos) e acompanha o log."""

    def __init__(self):
        self.proc = None
        self.logf = None
        self.pos = 0
        self.tail = ""

    def start(self, folder, args):
        venv_py = folder / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        CACHE.mkdir(parents=True, exist_ok=True)
        self.logf = open(LOG, "w")
        self.pos, self.tail = 0, ""
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [py, "-u", str(folder / "live.py"), *args], cwd=folder, env=env,
            stdin=subprocess.DEVNULL, stdout=self.logf, stderr=subprocess.STDOUT,
            start_new_session=True)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def read_log(self):
        try:
            with open(LOG, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return
        self.tail = (self.tail + data.decode("utf-8", "replace"))[-6000:]

    def started_ok(self):
        return any(m in self.tail for m in START_MARKERS)

    def last_error(self):
        lines = [l.strip() for l in self.tail.splitlines() if l.strip()]
        for l in reversed(lines):
            if l.startswith(("Erro", "Traceback")) or "Error" in l or "ModuleNotFound" in l:
                return l
        return ""

    def toggle_fg(self):
        if self.alive():
            os.kill(self.proc.pid, signal.SIGUSR1)

    def terminate(self):
        if self.alive():
            os.kill(self.proc.pid, signal.SIGTERM)

    def kill(self):
        self._group(signal.SIGKILL)

    def finish(self):
        """Chamado quando o processo principal terminou: limpa filhos soltos e devolve o código."""
        code = self.proc.poll() if self.proc else None
        self._group(signal.SIGTERM)
        if self.logf:
            self.logf.close()
            self.logf = None
        self.proc = None
        return code

    def _group(self, sig):
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


# ------------------------------------------------------------------- tema
ACCENT, BG, CARD, EDGE, TEXT, DIM = "#3daee9", "#15161b", "#1d1f27", "#2a2d38", "#e6e8ee", "#8b90a0"

QSS = f"""
QWidget#root {{ background: {BG}; }}
QLabel#title {{ font-size: 20px; font-weight: 700; }}
QLabel#dim {{ color: {DIM}; }}
QLabel#section {{ color: {DIM}; font-size: 11px; font-weight: 600; }}
QLabel#value {{ color: {ACCENT}; font-size: 24px; font-weight: 700; }}
QLabel#pill {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 10px;
               padding: 3px 11px; color: {DIM}; font-size: 12px; }}
QLabel#pill[live="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: #0f1014; }}
QFrame#card {{ background: {CARD}; border: 1px solid #262935; border-radius: 12px; }}
QFrame#segbar {{ background: {BG}; border: 1px solid #262935; border-radius: 10px; }}
QPushButton#seg {{ background: transparent; border: none; border-radius: 8px;
                   padding: 8px 0; color: {DIM}; font-weight: 600; }}
QPushButton#seg:hover:!checked {{ background: #262935; }}
QPushButton#seg:checked {{ background: {ACCENT}; color: #0f1014; }}
QPushButton#go {{ background: {ACCENT}; color: #0f1014; border: none; border-radius: 12px;
                  padding: 15px; font-size: 15px; font-weight: 700; }}
QPushButton#go:hover {{ background: #5bbcf0; }}
QPushButton#go[mode="stop"] {{ background: #e5484d; color: #ffffff; }}
QPushButton#go[mode="stop"]:hover {{ background: #ee6a6e; }}
QPushButton#go:disabled {{ background: {EDGE}; color: #6b7080; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT}; padding: 4px; }}
QPushButton#link:hover {{ color: #5bbcf0; }}
QSlider::groove:horizontal {{ height: 6px; background: {EDGE}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 18px; height: 18px;
                              margin: -6px 0; border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::sub-page:horizontal:disabled {{ background: #3a4a55; }}
QSlider::handle:horizontal:disabled {{ background: #5a5f6e; }}
"""


def dark_palette():
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    for role, col in ((R.Window, BG), (R.WindowText, TEXT), (R.Base, CARD), (R.AlternateBase, CARD),
                      (R.Text, TEXT), (R.Button, CARD), (R.ButtonText, TEXT),
                      (R.Highlight, ACCENT), (R.HighlightedText, "#0f1014"),
                      (R.ToolTipBase, CARD), (R.ToolTipText, TEXT), (R.PlaceholderText, "#6b7080")):
        p.setColor(role, QColor(col))
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(G.Disabled, role, QColor("#5a5f6e"))
    return p


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


# ------------------------------------------------------------------ painel
class Panel(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Framegen")
        icon = APP_DIR / "framegen.svg"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.settings = QSettings("framegen", "gui")
        self.runner = Runner()
        self.folder = find_folder(self.settings.value("folder", "", type=str))
        self.phase, self.user_stop, self.stop_deadline = "idle", False, 0.0
        self._build()
        self._load()
        self._refresh()
        self.setFixedWidth(400)
        self.adjustSize()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(400)

    # ---------------------------------------------------------- interface
    def _label(self, text, name="dim", wrap=False):
        lb = QLabel(text)
        lb.setObjectName(name)
        lb.setWordWrap(wrap)
        return lb

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(self._label("Framegen", "title"))
        head.addStretch()
        self.pill = self._label("Parado", "pill")
        head.addWidget(self.pill)
        root.addLayout(head)

        # Multiplicador
        self.box = QWidget()                      # tudo que trava enquanto roda
        bl = QVBoxLayout(self.box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(14)
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(16, 14, 16, 16); cl.setSpacing(10)
        row = QHBoxLayout()
        row.addWidget(self._label("MULTIPLICADOR", "section"))
        row.addStretch()
        self.val = self._label("2×", "value")
        row.addWidget(self.val)
        cl.addLayout(row)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(2, 10)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(self._refresh)
        cl.addWidget(self.slider)
        bl.addWidget(card)

        # FPS alvo
        card2 = QFrame(); card2.setObjectName("card")
        c2 = QVBoxLayout(card2); c2.setContentsMargins(16, 14, 16, 16); c2.setSpacing(10)
        c2.addWidget(self._label("FPS ALVO", "section"))
        self.cb_target = QComboBox()
        for v in TARGETS:
            self.cb_target.addItem("Ilimitado" if v == 0 else f"{v} FPS", v)
        self.cb_target.currentIndexChanged.connect(self._refresh)
        c2.addWidget(self.cb_target)
        self.hint = self._label("", "dim", True)
        c2.addWidget(self.hint)
        self.cap_row = QWidget()
        cr = QHBoxLayout(self.cap_row); cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self._label("FPS do jogo (captura)"))
        cr.addStretch()
        self.cb_fps = QComboBox()
        for v in (15, 20, 24, 30, 45, 60, 90, 120):
            self.cb_fps.addItem(f"{v} fps", v)
        self.cb_fps.currentIndexChanged.connect(self._refresh)
        cr.addWidget(self.cb_fps)
        c2.addWidget(self.cap_row)
        bl.addWidget(card2)
        root.addWidget(self.box)

        self.info = self._label("", "dim", True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.info)

        # Avançado
        self.adv_btn = QPushButton("Avançado  ▸")
        self.adv_btn.setObjectName("link")
        self.adv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_btn.clicked.connect(self._toggle_adv)
        root.addWidget(self.adv_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self.adv = QFrame(); self.adv.setObjectName("card"); self.adv.hide()
        al = QVBoxLayout(self.adv); al.setContentsMargins(16, 12, 16, 14); al.setSpacing(10)
        self.cb_width = QComboBox()
        for v, t in ((720, "720 px · leve"), (960, "960 px · padrão"),
                     (1280, "1280 px · nítido"), (1600, "1600 px · pesado")):
            self.cb_width.addItem(t, v)
        self.cb_fit = QComboBox()
        for v, t in (("stretch", "Preencher a tela · sem bordas"),
                     ("crop", "Preencher cortando as bordas"),
                     ("auto", "Automático"),
                     ("fit", "Manter proporção · com barras")):
            self.cb_fit.addItem(t, v)
        self.cb_mon = QComboBox()
        for i, s in enumerate(QApplication.screens()):
            self.cb_mon.addItem(f"{i + 1} · {s.name()}", i)
        for text, w in (("Resolução de processamento", self.cb_width), ("Monitor do overlay", self.cb_mon),
                        ("Preenchimento da tela", self.cb_fit)):
            r = QHBoxLayout(); r.addWidget(self._label(text)); r.addStretch(); r.addWidget(w)
            al.addLayout(r)
        self.folder_lb = self._label("", "dim", True)
        pick = QPushButton("Alterar pasta do framegen…")
        pick.setObjectName("link"); pick.clicked.connect(self._pick_folder)
        al.addWidget(self.folder_lb)
        al.addWidget(pick, alignment=Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.adv)

        self.go = QPushButton()
        self.go.setObjectName("go")
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.clicked.connect(self._on_go)
        root.addWidget(self.go)
        self.status = self._label("", "dim", True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)
        self.toggle = QPushButton("Alternar geração ON/OFF sem fechar")
        self.toggle.setObjectName("link")
        self.toggle.clicked.connect(self.runner.toggle_fg)
        self.toggle.hide()
        root.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignCenter)

    # ---------------------------------------------------------- estado
    def _cfg(self):
        target = int(self.cb_target.currentData() or 0)
        return dict(mult=self.slider.value(), unlimited=target == 0, target=target,
                    capture_fps=int(self.cb_fps.currentData()),
                    width=int(self.cb_width.currentData()),
                    monitor=int(self.cb_mon.currentData() or 0),
                    fit=str(self.cb_fit.currentData() or "stretch"))

    def _load(self):
        s = self.settings
        self.slider.setValue(s.value("mult", 2, type=int))
        i = self.cb_target.findData(s.value("target", 60, type=int))
        self.cb_target.setCurrentIndex(i if i >= 0 else self.cb_target.findData(60))
        for cb, key, default in ((self.cb_fps, "capture_fps", 30), (self.cb_width, "width", 960),
                                 (self.cb_mon, "monitor", 0)):
            i = cb.findData(s.value(key, default, type=int))
            cb.setCurrentIndex(i if i >= 0 else 0)
        i = self.cb_fit.findData(s.value("fit", "stretch", type=str))
        self.cb_fit.setCurrentIndex(i if i >= 0 else 0)

    def _save(self):
        c = self._cfg()
        for k, v in c.items():
            self.settings.setValue(k, v)
        self.settings.setValue("folder", str(self.folder or ""))

    def _refresh(self, *_):
        c = self._cfg()
        cap, out, lat = rates(c)
        self.val.setText(f"{c['mult']}×")
        self.cap_row.setVisible(c["unlimited"])
        self.hint.setText("Sem teto: saída = fps do jogo × multiplicador." if c["unlimited"]
                          else f"Saída fixa em {c['target']} fps: captura = {c['target']} ÷ multiplicador.")
        warn = "\nCaptura muito baixa: aumente o alvo ou reduza o multiplicador." if cap < 10 else ""
        screens = QApplication.screens()
        hz = screens[min(c["monitor"], len(screens) - 1)].refreshRate() if screens else 0
        if hz and out > hz + 1:
            warn += f"\nSeu monitor tem {hz:.0f} Hz: acima disso o alvo não aparece na tela."
        self.info.setText(f"Captura {fmt(cap)} fps  →  saída {fmt(out)} fps  ·  latência ≈ {lat:.0f} ms{warn}")
        self.folder_lb.setText(f"Pasta: {self.folder}" if self.folder
                               else "Pasta do framegen não encontrada (onde está o live.py).")
        if self.phase == "idle" and not self.status.text():
            self._set_phase("idle")
        self.adjustSize()

    def _toggle_adv(self):
        show = not self.adv.isVisible()
        self.adv.setVisible(show)
        self.adv_btn.setText("Avançado  ▾" if show else "Avançado  ▸")
        QTimer.singleShot(0, self.adjustSize)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta com o live.py", str(self.folder or Path.home()))
        if not d:
            return
        if (Path(d) / "live.py").is_file():
            self.folder = Path(d)
            self._save()
        else:
            self._set_phase("idle", "Essa pasta não tem o live.py.")
        self._refresh()

    def _set_phase(self, phase, note=""):
        self.phase = phase
        running = phase in ("starting", "running", "stopping")
        self.box.setEnabled(not running)
        self.adv_btn.setEnabled(not running)
        self.adv.setEnabled(not running)
        self.toggle.setVisible(phase == "running")
        self.go.setEnabled(phase != "stopping")
        self.go.setText({"idle": "▶   Iniciar geração de quadros", "starting": "■   Cancelar",
                         "running": "■   Parar", "stopping": "Encerrando…"}[phase])
        self.go.setProperty("mode", "stop" if phase in ("starting", "running") else "start")
        repolish(self.go)
        self.pill.setText({"idle": "Parado", "starting": "Iniciando", "running": "Ativo",
                           "stopping": "Encerrando"}[phase])
        self.pill.setProperty("live", "true" if phase == "running" else "false")
        repolish(self.pill)
        self.status.setText(note or {
            "idle": "Os ajustes valem a partir do próximo início.",
            "starting": "Escolha a janela do jogo no diálogo do sistema…",
            "running": "Gerando quadros  ·  Ctrl+Alt+F liga/desliga  ·  Ctrl+Alt+N fecha",
            "stopping": ""}[phase])

    # ---------------------------------------------------------- ações
    def _on_go(self):
        if self.phase in ("starting", "running"):
            self.user_stop, self.stop_deadline = True, time.monotonic() + 5
            self.runner.terminate()
            self._set_phase("stopping")
            return
        if not self.folder:
            self._pick_folder()
            if not self.folder:
                return
        self._save()
        try:
            self.runner.start(self.folder, build_args(self._cfg()))
        except OSError as e:
            self._set_phase("idle", f"Não foi possível iniciar: {e}")
            return
        self.user_stop = False
        self._set_phase("starting")

    def _tick(self):
        r = self.runner
        if r.proc is None:
            return
        r.read_log()
        if r.alive():
            if self.phase == "stopping" and time.monotonic() > self.stop_deadline:
                r.kill()
            elif self.phase == "starting" and r.started_ok():
                self._set_phase("running")
            return
        err = r.last_error()
        code = r.finish()
        note = ""
        if not self.user_stop:
            note = err or ("" if code in (0, None) else f"O framegen encerrou (código {code}).")
        self._set_phase("idle", note)
        self.status.setToolTip(f"Log completo: {LOG}")

    def closeEvent(self, ev):
        r = self.runner
        if r.alive():
            r.terminate()
            end = time.monotonic() + 3
            while r.alive() and time.monotonic() < end:
                time.sleep(0.05)
            r.kill()
            r.finish()
        self._save()
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Framegen")
    app.setDesktopFileName("framegen")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(QSS)
    w = Panel()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
