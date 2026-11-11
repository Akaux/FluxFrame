#!/usr/bin/env python3
"""
Resumo do CSV de fps gravado pela camada (FLUXFRAME_LOG_FILE) ou pelo live.py (--bench-log).

  python fps_report.py ~/fluxframe-fps.csv
  python fps_report.py antes.csv depois.csv        # compara dois registros

Colunas: t,jogo_fps,saida_fps,low1_fps,fg_on
"""
import csv
import statistics as st
import sys


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({k: (float(v) if v != "" else None) for k, v in r.items()})
            except ValueError:
                continue
    return rows


def col(rows, name):
    return [r[name] for r in rows if r.get(name) is not None]


def summarize(path):
    rows = load(path)
    if len(rows) < 2:
        return None
    jogo, saida, low = col(rows, "jogo_fps"), col(rows, "saida_fps"), col(rows, "low1_fps")
    s = {"arquivo": path, "duracao": rows[-1]["t"] - rows[0]["t"] + 1, "amostras": len(rows),
         "jogo_med": st.mean(jogo), "saida_med": st.mean(saida), "saida_min": min(saida),
         "saida_max": max(saida), "saida_desvio": st.pstdev(saida),
         "low1_med": st.mean(low) if low else 0.0, "low1_pior": min(low) if low else 0.0}
    s["ganho"] = (s["saida_med"] / s["jogo_med"] - 1) * 100 if s["jogo_med"] else 0.0
    return s


def show(s):
    print(f"\n{s['arquivo']}  ({s['amostras']} s de registro)")
    print(f"  Jogo (real):   média {s['jogo_med']:.1f} fps")
    print(f"  Saída:         média {s['saida_med']:.1f} | mín {s['saida_min']:.0f} | máx "
          f"{s['saida_max']:.0f} | desvio {s['saida_desvio']:.1f}")
    print(f"  1% low:        média {s['low1_med']:.0f} | pior {s['low1_pior']:.0f} fps")
    print(f"  Ganho sobre o jogo real: {s['ganho']:+.0f}%")


def main():
    paths = sys.argv[1:]
    if not paths:
        raise SystemExit(__doc__)
    results = []
    for p in paths:
        try:
            s = summarize(p)
        except OSError as e:
            raise SystemExit(f"Não consegui abrir {p}: {e}")
        if s is None:
            print(f"\n{p}: poucos dados (precisa de pelo menos 2 s de registro).")
            continue
        show(s)
        results.append(s)
    if len(results) == 2:
        a, b = results
        print(f"\nComparação (2º contra 1º): saída {b['saida_med'] / a['saida_med'] * 100 - 100:+.0f}% "
              f"| 1% low {b['low1_med'] - a['low1_med']:+.0f} fps")


if __name__ == "__main__":
    main()
