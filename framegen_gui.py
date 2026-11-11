#!/usr/bin/env python3
"""Framegen Control: painel minimalista com suporte a resolução automática."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QEvent
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget,
                               QRadioButton)

APP_DIR = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "framegen-gui"
LOG = CACHE / "ultima-execucao.log"
TARGETS = (30, 45, 60, 75, 90, 120, 144, 165, 240, 0)
START_MARKERS = ("Primeiro quadro", "SAIDA", "FG ON")

# (valor, rótulo curto, descrição para o tooltip/hint)
FSR_MODES = (
    ("fsr1",  "FSR 1 — Espacial (leve)",
     "EASU 4×4 + RCAS. Roda em qualquer GPU. É o modo mais leve."),
    ("fsr2",  "FSR 2 — Temporal (TAA)",
     "FSR 1 + acumulação temporal com motion vectors do flow. Reduz shimmer/aliasing."),
    ("fsr3",  "FSR 3 — Temporal + FG",
     "FSR 2 + frame gen (que já é ligado por padrão no overlay). Recomendado."),
    ("dlss1", "DLSS 1 — GPU-heavy",
     "Temporal + bilateral 6×6 + Sobel (144 reads/px). Alivia a CPU, pesa na GPU."),
    ("fsr4",  "FSR 4 — Escala ×N (legado)",
     "Modo antigo: multiplica o fator de quadros por --scale-factor (1x/4x)."),
)


def build_args(cfg):
    args = ["--screen", "--overlay", "--mult", str(cfg["mult"]),
            "--width", str(cfg["width"]), "--output", str(cfg["monitor"]),
            "--fit", cfg["fit"], "--fsr-mode", cfg["fsr_mode"],
            "--scale-factor", str(cfg["scale_factor"])]
    if cfg.get("window"):
        args += ["--window", cfg["window"]]
    if cfg["unlimited"]:
        args += ["--fps", str(cfg["capture_fps"]), "--target-fps", "0"]
    else:
        args += ["--target-fps", str(cfg["target"])]
    return args


def rates(cfg):
    mult = cfg["mult"]
    if cfg["fsr_mode"] == "fsr4" and cfg["scale_factor"] > 1:
        mult *= cfg["scale_factor"]
    capture = float(cfg["capture_fps"]) if cfg["unlimited"] else (cfg["target"] / mult if mult > 0 else 30.0)
    interval = 1.0 / capture if capture > 0 else 0.033
    return capture, capture * mult, (2 * interval - interval / mult) * 1000


def fmt(x):
    return f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}"


def find_folder(saved):
    for c in (saved, APP_DIR, APP_DIR / "framegen", Path.cwd()):
        if c and (Path(c) / "live.py").is_file():
            return Path(c)
    return None


class Runner:
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

    def terminate(self):
        if self.alive():
            os.kill(self.proc.pid, signal.SIGTERM)

    def finish(self):
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
QPushButton#go {{ background: {ACCENT}; color: #0f1014; border: none; border-radius: 12px;
                  padding: 15px; font-size: 15px; font-weight: 700; }}
QPushButton#go:hover {{ background: #5bbcf0; }}
QPushButton#go[mode="stop"] {{ background: #e5484d; color: #ffffff; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT}; padding: 4px; }}
QSlider::groove:horizontal {{ height: 6px; background: {EDGE}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 18px; height: 18px; margin: -6px 0; border-radius: 9px; }}
QComboBox {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 8px;
             padding: 6px 10px; color: {TEXT}; min-height: 20px; }}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {CARD}; color: {TEXT};
                               selection-background-color: {ACCENT}; selection-color: #0f1014; }}
"""


def dark_palette():
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    for role, col in ((R.Window, BG), (R.WindowText, TEXT), (R.Base, CARD), (R.AlternateBase, CARD),
                      (R.Text, TEXT), (R.Button, CARD), (R.ButtonText, TEXT),
                      (R.Highlight, ACCENT), (R.HighlightedText, "#0f1014")):
        p.setColor(role, QColor(col))
    return p


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


class Panel(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Framegen Control")
        icon = APP_DIR / "framegen.svg"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.settings = QSettings("framegen", "gui")
        self.runner = Runner()
        self.folder = find_folder(self.settings.value("folder", "", type=str))
        self.phase = "idle"
        self._build()
        self._load()
        self._refresh()
        self.setFixedWidth(400)
        self.adjustSize()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(400)

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
        head.addWidget(self._label("Framegen Control", "title"))
        head.addStretch()
        self.pill = self._label("Parado", "pill")
        head.addWidget(self.pill)
        root.addLayout(head)

        # ---------------- multiplicador
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
        root.addWidget(card)

        # ---------------- FSR / DLSS (COMBO com as 5 opções)
        card_fsr = QFrame(); card_fsr.setObjectName("card")
        cf_l = QVBoxLayout(card_fsr); cf_l.setContentsMargins(16, 14, 16, 16); cf_l.setSpacing(10)
        cf_l.addWidget(self._label("UPSCALER (FSR 1/2/3/4 e DLSS 1)", "section"))

        self.cb_fsr = QComboBox()
        for val, label, _tip in FSR_MODES:
            self.cb_fsr.addItem(label, val)
        self.cb_fsr.currentIndexChanged.connect(self._on_fsr_changed)
        cf_l.addWidget(self.cb_fsr)

        self.fsr_hint = self._label("", "dim", True)
        cf_l.addWidget(self.fsr_hint)

        # Escala de fluidez: SÓ existe no modo FSR 4 (legado).
        scale_row = QHBoxLayout()
        scale_row.addWidget(self._label("Escala de fluidez (FSR 4):"))
        self.rb_scale1 = QRadioButton("1x (padrão)")
        self.rb_scale4 = QRadioButton("4x")
        self.rb_scale1.setChecked(True)
        self.rb_scale1.toggled.connect(self._refresh)
        self.rb_scale4.toggled.connect(self._refresh)
        scale_row.addWidget(self.rb_scale1)
        scale_row.addWidget(self.rb_scale4)
        cf_l.addLayout(scale_row)
        root.addWidget(card_fsr)

        # ---------------- FPS alvo
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
        root.addWidget(card2)

        self.info = self._label("", "dim", True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.info)

        self.adv_btn = QPushButton("Avançado  ▸")
        self.adv_btn.setObjectName("link")
        self.adv_btn.clicked.connect(self._toggle_adv)
        root.addWidget(self.adv_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        # ---------------- avançado
        self.adv = QFrame(); self.adv.setObjectName("card"); self.adv.hide()
        al = QVBoxLayout(self.adv); al.setContentsMargins(16, 12, 16, 14); al.setSpacing(10)

        self.cb_width = QComboBox()
        self.cb_width.addItem("Automática (Janela)", "auto")
        for v, t in ((720, "720 px"), (960, "960 px"), (1280, "1280 px")):
            self.cb_width.addItem(t, v)

        self.cb_fit = QComboBox()
        for v, t in (("stretch", "Preencher"), ("auto", "Automático")):
            self.cb_fit.addItem(t, v)
        self.cb_mon = QComboBox()
        for i, s in enumerate(QApplication.screens()):
            self.cb_mon.addItem(f"{i + 1} · {s.name()}", i)
        self.ed_window = QLineEdit()
        self.ed_window.setPlaceholderText("opcional: parte do título ou da classe")
        self.ed_window.setToolTip("Resolução automática cobre a janela do app. Se o painel escolher "
                                  "a janela errada, escreva aqui parte do título ou da classe dela.")
        self.ed_window.setMinimumWidth(190)
        for text, w in (("Resolução", self.cb_width), ("Janela alvo", self.ed_window),
                        ("Monitor", self.cb_mon), ("Ajuste", self.cb_fit)):
            r = QHBoxLayout(); r.addWidget(self._label(text)); r.addStretch(); r.addWidget(w)
            al.addLayout(r)
        root.addWidget(self.adv)

        self.go = QPushButton()
        self.go.setObjectName("go")
        self.go.clicked.connect(self._on_go)
        root.addWidget(self.go)

        self.status = self._label("", "dim", True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        # Todos os widgets existem: agora é seguro consultar _cfg() via _refresh().
        self._on_fsr_changed()

    # ------------------------------------------------------------------ FSR/DLSS
    def _on_fsr_changed(self, *_):
        """Chamado quando o combo de FSR muda. Habilita a escala só no FSR 4 e atualiza o hint."""
        mode = self.cb_fsr.currentData() or "fsr1"
        is_fsr4 = (mode == "fsr4")
        self.rb_scale1.setEnabled(is_fsr4)
        self.rb_scale4.setEnabled(is_fsr4)
        hint = next((h for v, _, h in FSR_MODES if v == mode), "")
        self.fsr_hint.setText(hint)
        self._refresh()

    def _cfg(self):
        target = int(self.cb_target.currentData() or 0)
        fsr_mode = self.cb_fsr.currentData() or "fsr1"
        scale_factor = 4 if (fsr_mode == "fsr4" and self.rb_scale4.isChecked()) else 1
        width_data = self.cb_width.currentData()
        width = "auto" if width_data == "auto" else int(width_data)
        return dict(mult=self.slider.value(), unlimited=target == 0, target=target,
                    capture_fps=int(self.cb_fps.currentData()),
                    width=width,
                    monitor=int(self.cb_mon.currentData() or 0),
                    fit=str(self.cb_fit.currentData() or "stretch"),
                    window=self.ed_window.text().strip(),
                    fsr_mode=fsr_mode, scale_factor=scale_factor)

    def _load(self):
        s = self.settings
        self.slider.setValue(s.value("mult", 2, type=int))
        self.ed_window.setText(s.value("window", "", type=str))
        target_default = 0
        i = self.cb_target.findData(target_default)
        self.cb_target.setCurrentIndex(i if i >= 0 else 0)
        saved_fsr = s.value("fsr_mode", "fsr1", type=str)
        i = self.cb_fsr.findData(saved_fsr)
        if i >= 0:
            self.cb_fsr.setCurrentIndex(i)

    def _save(self):
        c = self._cfg()
        for k, v in c.items():
            self.settings.setValue(k, str(v))
        self.settings.setValue("folder", str(self.folder or ""))

    def _refresh(self, *_):
        c = self._cfg()
        cap, out, lat = rates(c)
        self.val.setText(f"{c['mult']}×")
        self.cap_row.setVisible(c["unlimited"])
        self.hint.setText("Modo Ilimitado Ativo: O FPS de saída escala livremente com o FSR 4." if c["unlimited"]
                          else f"Saída Fixa em {c['target']} FPS.")
        self.info.setText(f"Captura {fmt(cap)} FPS  →  Saída Efetiva {fmt(out)} FPS  ·  Latência ≈ {lat:.0f} ms")
        self.adjustSize()

    def _toggle_adv(self):
        show = not self.adv.isVisible()
        self.adv.setVisible(show)
        self.adv_btn.setText("Avançado  ▾" if show else "Avançado  ▸")
        QTimer.singleShot(0, self.adjustSize)

    def _set_phase(self, phase, note=""):
        self.phase = phase
        self.go.setText({"idle": "▶   Iniciar Geração de Quadros", "running": "■   Parar"}[phase])
        self.pill.setText({"idle": "Parado", "running": "Ativo"}[phase])
        self.pill.setProperty("live", "true" if phase == "running" else "false")
        repolish(self.pill)
        self.status.setText(note or {"idle": "", "running": "Gerando quadros em alta fluidez"}[phase])

    def _on_go(self):
        if self.phase == "running":
            self.runner.terminate()
            self._set_phase("idle")
            return
        if not self.folder:
            self.folder = find_folder(Path.cwd())
            if not self.folder:
                return
        self._save()
        self.runner.start(self.folder, build_args(self._cfg()))
        self._set_phase("running")

    def _tick(self):
        r = self.runner
        if r.proc is None:
            return
        r.read_log()
        if not r.alive():
            self._set_phase("idle")

    def closeEvent(self, ev):
        if self.runner.alive():
            self.runner.terminate()
            self.runner.finish()
        self._save()
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(QSS)
    w = Panel()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()