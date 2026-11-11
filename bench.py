"""
Benchmark em tempo real do framegen: fps REAL do jogo x fps efetivamente apresentado.

Três números que importam:
  JOGO   quadros únicos que o jogo entrega (contador do PipeWire; é o que você teria sem FG)
  SAIDA  quadros únicos apresentados por segundo pelo overlay (originais + gerados).
         Quadro repetido não conta: se o jogo não entregou nada novo, não há o que mostrar.
  1% low o pior caso de fluidez: fps equivalente ao intervalo mais longo entre 99% dos quadros.
         É ele que denuncia a "variação" de fps que a média esconde.

Também guarda a média de SAIDA separada por estado do FG (ligado/desligado). Alternar com
Ctrl+Alt+F durante o jogo dá o ganho medido, lado a lado, na mesma cena.
"""
import collections
import threading
import time

import numpy as np


class Bench:
    WINDOW = 1.0          # janela do fps instantâneo, em segundos

    def __init__(self, log_path=None):
        self.lock = threading.Lock()
        self.game = collections.deque(maxlen=900)    # (t, contador acumulado de quadros do jogo)
        self.pres = collections.deque(maxlen=900)    # instantes de apresentação (quadros únicos)
        self.count = {True: 0, False: 0}             # apresentações por estado do FG
        self.secs = {True: 0.0, False: 0.0}          # tempo gasto em cada estado
        self.state, self.since = True, None
        self.first = None                            # (t, contador) do primeiro quadro do jogo
        self.log_path = log_path
        self._log = None
        self._log_t0 = None

    # ------------------------------------------------------------ entradas
    def on_source(self, total, now=None):
        """Chamado a cada quadro publicado pela captura. `total` = quadros reais acumulados."""
        now = time.perf_counter() if now is None else now
        with self.lock:
            if self.first is None:
                self.first = (now, total)
                self.since = now
            self.game.append((now, total))

    def present(self, now, fg_on):
        """Chamado quando um quadro único vai para a tela."""
        with self.lock:
            self.pres.append(now)
            self.count[bool(fg_on)] += 1

    def set_fg(self, on, now=None):
        now = time.perf_counter() if now is None else now
        with self.lock:
            if self.since is not None:
                self.secs[self.state] += now - self.since
                self.since = now
            self.state = bool(on)

    # ------------------------------------------------------------ leituras
    def game_fps(self, now):
        with self.lock:
            return self._game_fps(now)

    def _game_fps(self, now):
        old = None
        for t, n in self.game:
            if t >= now - self.WINDOW:
                old = (t, n)
                break
        if old is None or not self.game:
            return 0.0
        t1, n1 = self.game[-1]
        return (n1 - old[1]) / (t1 - old[0]) if t1 - old[0] > 0.05 else 0.0

    def game_avg(self):
        with self.lock:
            if self.first is None or len(self.game) < 2:
                return 0.0
            t1, n1 = self.game[-1]
            return (n1 - self.first[1]) / max(t1 - self.first[0], 1e-6)

    def out_fps(self, now):
        with self.lock:
            return sum(1 for t in self.pres if now - t <= self.WINDOW)

    def pacing(self, now, span=3.0):
        """(1% low em fps, frametime médio em ms, pior frametime em ms) dos últimos `span` s."""
        with self.lock:
            ts = [t for t in self.pres if now - t <= span]
        if len(ts) < 12:
            return None
        dt = np.diff(ts)
        return 1.0 / np.percentile(dt, 99), float(dt.mean() * 1000), float(dt.max() * 1000)

    def state_avg(self, on, now=None):
        """Média de SAIDA (fps) enquanto o FG esteve no estado `on`; None se ainda sem dados."""
        now = time.perf_counter() if now is None else now
        with self.lock:
            secs = self.secs[on] + ((now - self.since) if self.state == on and self.since else 0.0)
            return self.count[on] / secs if secs >= 2.0 else None

    # ------------------------------------------------------------ registro
    def log_row(self, now, **extra):
        """Uma linha por chamada em CSV (se --bench-log foi dado)."""
        if not self.log_path:
            return
        if self._log is None:
            self._log = open(self.log_path, "w", buffering=1)
            self._log_t0 = now
            self._log.write("t,jogo_fps,saida_fps,low1_fps,fg_on," + ",".join(extra) + "\n")
        p = self.pacing(now)
        row = [f"{now - self._log_t0:.1f}", f"{self.game_fps(now):.1f}", f"{self.out_fps(now)}",
               f"{p[0]:.1f}" if p else "", str(int(self.state))] + [str(v) for v in extra.values()]
        self._log.write(",".join(row) + "\n")

    def close(self):
        if self._log:
            self._log.close()
            self._log = None

    def summary(self, now=None):
        now = time.perf_counter() if now is None else now
        game = self.game_avg()
        on, off = self.state_avg(True, now), self.state_avg(False, now)
        lines = [f"Jogo (real):  média {game:.1f} fps"]
        if on is not None:
            lines.append(f"Saída FG ON:  média {on:.1f} fps"
                         + (f"  ({(on / game - 1) * 100:+.0f}% sobre o jogo real)" if game else ""))
        if off is not None:
            lines.append(f"Saída FG OFF: média {off:.1f} fps")
        if on is not None and off:
            lines.append(f"Ganho medido (ON x OFF): {(on / off - 1) * 100:+.0f}%")
        p = self.pacing(now)
        if p:
            lines.append(f"Fluidez (últimos 3 s): 1% low {p[0]:.0f} fps | frametime médio "
                         f"{p[1]:.1f} ms | pior {p[2]:.1f} ms")
        return lines
