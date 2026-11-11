#!/usr/bin/env python3
"""Framegen Control — painel LANDSCAPE com suporte a DLSS 5."""
import os
import signal
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSlider,
                               QVBoxLayout, QWidget, QRadioButton)

APP_DIR = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "framegen-gui"
LOG = CACHE / "ultima-execucao.log"
TARGETS = (30, 45, 60, 75, 90, 120, 144, 165, 240, 0)
START_MARKERS = ("Primeiro quadro", "SAIDA", "FG ON")

FSR_MODES = (
    ("fsr1",  "FSR 1 — Espacial (leve)",     "EASU + RCAS. Roda em qualquer GPU."),
    ("fsr2",  "FSR 2 — Temporal (TAA)",      "Acumulação temporal com motion vectors."),
    ("fsr3",  "FSR 3 — Temporal + FG",       "FSR 2 + frame gen. Recomendado."),
    ("dlss1", "DLSS 1 — GPU-heavy",          "TAA + bilateral 6×6 + Sobel."),
    ("dlss3", "DLSS 3 — Cinematográfico",    "TAA + Lanczos-2 + grade quente + S-curve."),
    ("dlss5", "DLSS 5 — Neural-like (α)",    "Iluminação + materiais + SR + TAA. Experimental."),
    ("fsr4",  "FSR 4 — Escala ×N (legado)",  "Multiplica o fator por --scale-factor."),
)

FLOW_OPTS = (
    ("farneback",    "Farneback",  "Melhor precisão. ~12ms a 320x180."),
    ("dis-medium",   "DIS Medium", "~4.5ms a 320x180."),
    ("dis-fast",     "DIS Fast",   "~1.6ms a 320x180. ~7× mais rápido."),
    ("dis-ultrafast","DIS Ultrafast","~0.5ms a 320x180. ~24× mais rápido."),
)

SCALE_VALUES = (0.25, 0.22, 0.20, 0.18, 0.16, 0.15, 0.13, 0.12)

# Estágios visuais do DLSS 5 (os 6 do diagrama)
DLSS5_STAGES = ("Captura", "Análise", "SR IA", "Iluminação", "Temporal", "Final")


def build_args(cfg):
    args = ["--screen", "--overlay", "--mult", str(cfg["mult"]),
            "--width", str(cfg["width"]), "--output", str(cfg["monitor"]),
            "--fit", cfg["fit"], "--fsr-mode", cfg["fsr_mode"],
            "--scale-factor", str(cfg["scale_factor"]),
            "--scale", str(cfg["scale"]), "--flow", cfg["flow"]]
    if cfg["fsr_mode"] == "dlss5":
        args += ["--dlss5-strength", str(cfg["dlss5_strength"])]
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
QLabel#section {{ color: {DIM}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#value {{ color: {ACCENT}; font-size: 26px; font-weight: 700; }}
QLabel#valueSm {{ color: {ACCENT}; font-size: 14px; font-weight: 700; }}
QLabel#pill {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 10px;
               padding: 3px 11px; color: {DIM}; font-size: 12px; }}
QLabel#pill[live="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: #0f1014; }}
QFrame#card {{ background: {CARD}; border: 1px solid #262935; border-radius: 12px; }}
QFrame#pipeline {{ background: transparent; border: none; }}
QLabel#stage {{ background: #23262f; border: 1px solid #2a2d38; border-radius: 8px;
                padding: 4px 10px; color: {DIM}; font-size: 10px; font-weight: 600; }}
QLabel#stage[active="true"] {{ background: #3daee926; border-color: {ACCENT};
                                color: {ACCENT}; }}
QLabel#arrow {{ color: {DIM}; font-size: 12px; font-weight: 400; }}
QPushButton#go {{ background: {ACCENT}; color: #0f1014; border: none; border-radius: 12px;
                  padding: 14px 24px; font-size: 15px; font-weight: 700; min-width: 260px; }}
QPushButton#go:hover {{ background: #5bbcf0; }}
QPushButton#go[mode="stop"] {{ background: #e5484d; color: #ffffff; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT}; padding: 4px; }}
QSlider::groove:horizontal {{ height: 6px; background: {EDGE}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 18px; height: 18px;
                              margin: -6px 0; border-radius: 9px; }}
QComboBox {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 8px;
             padding: 6px 10px; color: {TEXT}; min-height: 20px; }}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {CARD}; color: {TEXT};
                               selection-background-color: {ACCENT}; selection-color: #0f1014; }}
QLineEdit {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 8px;
             padding: 6px 10px; color: {TEXT}; }}
"""


def dark_palette():
    p = QPalette()
    R = QPalette.ColorRole
    for role, col in ((R.Window, BG), (R.WindowText, TEXT), (R.Base, CARD),
                      (R.AlternateBase, CARD), (R.Text, TEXT), (R.Button, CARD),
                      (R.ButtonText, TEXT), (R.Highlight, ACCENT),
                      (R.HighlightedText, "#0f1014")):
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
        self.setFixedWidth(1200)
        self.adjustSize()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(400)

    def _label(self, text, name="dim", wrap=False):
        lb = QLabel(text)
        lb.setObjectName(name)
        lb.setWordWrap(wrap)
        return lb

    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        return f

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        # ============================================================ HEADER
        head = QHBoxLayout()
        head.addWidget(self._label("Framegen Control", "title"))
        head.addSpacing(16)
        self.stage_strip_widget = self._make_pipeline_strip()
        head.addWidget(self.stage_strip_widget)
        head.addStretch()
        self.pill = self._label("Parado", "pill")
        head.addWidget(self.pill)
        root.addLayout(head)

        # ============================================================ CARDS (LINHA)
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)

        # ---- Multiplicador
        c1 = self._card()
        l1 = QVBoxLayout(c1); l1.setContentsMargins(16, 14, 16, 16); l1.setSpacing(8)
        r = QHBoxLayout()
        r.addWidget(self._label("MULTIPLICADOR", "section"))
        r.addStretch()
        self.val = self._label("2×", "value")
        r.addWidget(self.val)
        l1.addLayout(r)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(2, 10)
        self.slider.valueChanged.connect(self._refresh)
        l1.addWidget(self.slider)
        l1.addStretch()
        cards_row.addWidget(c1, 2)

        # ---- Upscaler + DLSS5 strength
        c2 = self._card()
        l2 = QVBoxLayout(c2); l2.setContentsMargins(16, 14, 16, 16); l2.setSpacing(8)
        l2.addWidget(self._label("UPSCALER", "section"))
        self.cb_fsr = QComboBox()
        for val, label, _ in FSR_MODES:
            self.cb_fsr.addItem(label, val)
        self.cb_fsr.currentIndexChanged.connect(self._on_fsr_changed)
        l2.addWidget(self.cb_fsr)
        self.fsr_hint = self._label("", "dim", True)
        self.fsr_hint.setMinimumHeight(32)
        l2.addWidget(self.fsr_hint)

        r = QHBoxLayout()
        r.addWidget(self._label("Escala fluidez (FSR4):"))
        self.rb_scale1 = QRadioButton("1x")
        self.rb_scale4 = QRadioButton("4x")
        self.rb_scale1.setChecked(True)
        self.rb_scale1.toggled.connect(self._refresh)
        self.rb_scale4.toggled.connect(self._refresh)
        r.addWidget(self.rb_scale1)
        r.addWidget(self.rb_scale4)
        r.addStretch()
        l2.addLayout(r)

        # DLSS5 strength (aparece só no modo dlss5)
        self.dlss5_row = QWidget()
        dr = QVBoxLayout(self.dlss5_row); dr.setContentsMargins(0, 4, 0, 0); dr.setSpacing(4)
        hdr = QHBoxLayout()
        hdr.addWidget(self._label("DLSS 5 — Força", "section"))
        hdr.addStretch()
        self.lbl_dlss5 = self._label("0.60", "valueSm")
        hdr.addWidget(self.lbl_dlss5)
        dr.addLayout(hdr)
        self.slider_dlss5 = QSlider(Qt.Orientation.Horizontal)
        self.slider_dlss5.setRange(0, 100)
        self.slider_dlss5.setValue(60)
        self.slider_dlss5.valueChanged.connect(self._on_dlss5_strength)
        dr.addWidget(self.slider_dlss5)
        l2.addWidget(self.dlss5_row)
        l2.addStretch()
        cards_row.addWidget(c2, 3)

        # ---- Fluxo
        c3 = self._card()
        l3 = QVBoxLayout(c3); l3.setContentsMargins(16, 14, 16, 16); l3.setSpacing(8)
        l3.addWidget(self._label("FLUXO (MOTION VECTORS)", "section"))
        hdr = QHBoxLayout()
        hdr.addWidget(self._label("Escala"))
        hdr.addStretch()
        self.lbl_scale = self._label("0.25", "valueSm")
        hdr.addWidget(self.lbl_scale)
        l3.addLayout(hdr)
        self.slider_scale = QSlider(Qt.Orientation.Horizontal)
        self.slider_scale.setRange(0, len(SCALE_VALUES) - 1)
        self.slider_scale.valueChanged.connect(self._on_scale_changed)
        l3.addWidget(self.slider_scale)
        r = QHBoxLayout()
        r.addWidget(self._label("Estimador"))
        r.addStretch()
        self.cb_flow = QComboBox()
        for val, lbl, _ in FLOW_OPTS:
            self.cb_flow.addItem(lbl, val)
        self.cb_flow.currentIndexChanged.connect(self._refresh)
        r.addWidget(self.cb_flow)
        l3.addLayout(r)
        self.flow_hint = self._label("", "dim", True)
        l3.addWidget(self.flow_hint)
        l3.addStretch()
        cards_row.addWidget(c3, 2)

        # ---- FPS Alvo
        c4 = self._card()
        l4 = QVBoxLayout(c4); l4.setContentsMargins(16, 14, 16, 16); l4.setSpacing(8)
        l4.addWidget(self._label("FPS ALVO", "section"))
        self.cb_target = QComboBox()
        for v in TARGETS:
            self.cb_target.addItem("Ilimitado" if v == 0 else f"{v} FPS", v)
        self.cb_target.currentIndexChanged.connect(self._refresh)
        l4.addWidget(self.cb_target)
        self.hint = self._label("", "dim", True)
        l4.addWidget(self.hint)
        self.cap_row = QWidget()
        cr = QHBoxLayout(self.cap_row); cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self._label("Captura"))
        cr.addStretch()
        self.cb_fps = QComboBox()
        for v in (15, 20, 24, 30, 45, 60, 90, 120):
            self.cb_fps.addItem(f"{v}", v)
        self.cb_fps.currentIndexChanged.connect(self._refresh)
        cr.addWidget(self.cb_fps)
        l4.addWidget(self.cap_row)
        l4.addStretch()
        cards_row.addWidget(c4, 2)

        root.addLayout(cards_row)

        # ============================================================ INFO
        self.info = self._label("", "dim", True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.info)

        # ============================================================ AÇÕES
        actions = QHBoxLayout()
        self.adv_btn = QPushButton("Avançado  ▸")
        self.adv_btn.setObjectName("link")
        self.adv_btn.clicked.connect(self._toggle_adv)
        actions.addWidget(self.adv_btn)
        actions.addStretch()
        self.go = QPushButton()
        self.go.setObjectName("go")
        self.go.clicked.connect(self._on_go)
        actions.addWidget(self.go)
        actions.addStretch()
        actions.addSpacing(140)  # compensa o botão da esquerda
        root.addLayout(actions)

        # ============================================================ AVANÇADO
        self.adv = self._card()
        self.adv.hide()
        al = QHBoxLayout(self.adv)
        al.setContentsMargins(16, 12, 16, 14)
        al.setSpacing(20)

        self.cb_width = QComboBox()
        self.cb_width.addItem("Automática", "auto")
        for v, t in ((720, "720px"), (960, "960px"), (1280, "1280px")):
            self.cb_width.addItem(t, v)
        self.cb_fit = QComboBox()
        for v, t in (("stretch", "Preencher"), ("auto", "Automático")):
            self.cb_fit.addItem(t, v)
        self.cb_mon = QComboBox()
        for i, s in enumerate(QApplication.screens()):
            self.cb_mon.addItem(f"{i+1}·{s.name()}", i)
        self.ed_window = QLineEdit()
        self.ed_window.setPlaceholderText("título/classe da janela")
        self.ed_window.setMinimumWidth(180)

        for text, w in (("Resolução", self.cb_width), ("Ajuste", self.cb_fit),
                        ("Monitor", self.cb_mon), ("Janela alvo", self.ed_window)):
            col = QVBoxLayout()
            col.addWidget(self._label(text, "section"))
            col.addWidget(w)
            al.addLayout(col)
        al.addStretch()
        root.addWidget(self.adv)

        # ============================================================ STATUS
        self.status = self._label("", "dim", True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        self._on_fsr_changed()

    def _make_pipeline_strip(self):
        """6 estágios do DLSS 5 (só visível quando fsr_mode == dlss5)."""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.stage_labels = []
        for i, name in enumerate(DLSS5_STAGES):
            if i:
                arrow = self._label("→", "arrow")
                lay.addWidget(arrow)
            lb = self._label(name, "stage")
            lb.setProperty("active", "true")
            self.stage_labels.append(lb)
            lay.addWidget(lb)
        w.hide()
        return w

    # -------------------------------------------------------------- handlers
    def _on_fsr_changed(self, *_):
        mode = self.cb_fsr.currentData() or "fsr1"
        is_fsr4 = mode == "fsr4"
        is_dlss5 = mode == "dlss5"
        self.rb_scale1.setEnabled(is_fsr4)
        self.rb_scale4.setEnabled(is_fsr4)
        hint = next((h for v, _, h in FSR_MODES if v == mode), "")
        self.fsr_hint.setText(hint)
        self.dlss5_row.setVisible(is_dlss5)
        self.stage_strip_widget.setVisible(is_dlss5)
        self._refresh()

    def _on_scale_changed(self, v):
        self.lbl_scale.setText(f"{SCALE_VALUES[v]:.2f}")
        self._refresh()

    def _on_dlss5_strength(self, v):
        self.lbl_dlss5.setText(f"{v / 100:.2f}")
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
                    fsr_mode=fsr_mode, scale_factor=scale_factor,
                    scale=SCALE_VALUES[self.slider_scale.value()],
                    flow=str(self.cb_flow.currentData() or "farneback"),
                    dlss5_strength=self.slider_dlss5.value() / 100.0)

    def _load(self):
        s = self.settings
        self.slider.setValue(s.value("mult", 2, type=int))
        self.ed_window.setText(s.value("window", "", type=str))
        i = self.cb_target.findData(0)
        self.cb_target.setCurrentIndex(i if i >= 0 else 0)
        for name, widget, default in (("fsr_mode", self.cb_fsr, "fsr1"),
                                      ("flow", self.cb_flow, "farneback")):
            saved = s.value(name, default, type=str)
            i = widget.findData(saved)
            if i >= 0:
                widget.setCurrentIndex(i)
        try:
            idx = SCALE_VALUES.index(float(s.value("scale", "0.25", type=str)))
        except (ValueError, TypeError):
            idx = 0
        self.slider_scale.setValue(idx)
        try:
            v = int(float(s.value("dlss5_strength", "0.6", type=str)) * 100)
            self.slider_dlss5.setValue(max(0, min(100, v)))
        except (ValueError, TypeError):
            pass

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
        self.hint.setText("Ilimitado: FPS de saída escala livremente."
                          if c["unlimited"] else f"Saída Fixa em {c['target']} FPS.")
        if c["fsr_mode"] == "dlss5":
            self.info.setText(
                f"Captura {fmt(cap)} FPS → Saída {fmt(out)} FPS · Latência ≈ {lat:.0f} ms · "
                f"DLSS 5 força {c['dlss5_strength']:.2f}")
        else:
            self.info.setText(
                f"Captura {fmt(cap)} FPS → Saída {fmt(out)} FPS · Latência ≈ {lat:.0f} ms")
        self.adjustSize()

    def _toggle_adv(self):
        show = not self.adv.isVisible()
        self.adv.setVisible(show)
        self.adv_btn.setText("Avançado  ▾" if show else "Avançado  ▸")
        QTimer.singleShot(0, self.adjustSize)

    def _set_phase(self, phase, note=""):
        self.phase = phase
        self.go.setText({"idle": "▶   Iniciar Geração de Quadros",
                         "running": "■   Parar"}[phase])
        self.pill.setText({"idle": "Parado", "running": "Ativo"}[phase])
        self.pill.setProperty("live", "true" if phase == "running" else "false")
        repolish(self.pill)
        self.status.setText(note or {"idle": "",
                                     "running": "Gerando quadros em alta fluidez"}[phase])

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