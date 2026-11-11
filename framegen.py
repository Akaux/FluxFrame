#!/usr/bin/env python3
"""
Protótipo offline de Frame Generation (Etapas 1 e 2).

Modos:
  demo    gera um vídeo sintético com movimento rápido (para testar sem baixar nada)
  eval    compara mistura simples x interpolação com movimento (PSNR contra o quadro real)
  upscale gera um novo vídeo com mais FPS (fator 2x, 3x, 4x...)

Exemplos:
  python framegen.py demo demo.mp4
  python framegen.py eval demo.mp4
  python framegen.py upscale meu_video.mp4 saida.mp4 --factor 2
"""
import argparse
import threading
import time

import cv2
import numpy as np


# ---------------------------------------------------------------- movimento
def small_gray(img, scale=0.5):
    """Quadro em tons de cinza na resolução do cálculo do flow (reaproveitável entre pares)."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if scale != 1.0:
        h, w = g.shape
        size = (max(8, int(w * scale)), max(8, int(h * scale)))
        g = cv2.resize(g, size, interpolation=cv2.INTER_AREA)
    return g


FLOW_METHODS = ("dis-ultrafast", "dis-fast", "dis-medium", "farneback")
_flow_method = "farneback"        # padrão: melhor qualidade medida (DIS é opcional via --flow)
_tls = threading.local()          # DISOpticalFlow não é thread-safe: uma instância por thread


def set_flow_method(name):
    """Escolhe o estimador de movimento. DIS é ~7x mais rápido que Farneback (a 320x180:
    0,5 / 1,6 / 4,5 ms contra ~12 ms), mas na cena de teste perdeu 6-7 dB de PSNR; sem DIS no OpenCV instalado, cai para Farneback."""
    global _flow_method
    if name not in FLOW_METHODS:
        raise ValueError(f"método de flow inválido: {name}")
    if name.startswith("dis") and not hasattr(cv2, "DISOpticalFlow"):
        print("OpenCV sem DISOpticalFlow; usando Farneback.")
        name = "farneback"
    _flow_method = name
    return name


def get_flow_method():
    return _flow_method


def _dis_instance(name):
    cached = getattr(_tls, "dis", None)
    if cached is None or cached[0] != name:
        preset = {"dis-ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
                  "dis-fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
                  "dis-medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM}[name]
        cached = (name, cv2.DISOpticalFlow.create(preset))
        _tls.dis = cached
    return cached[1]


def _raw_flow(ga, gb):
    if _flow_method == "farneback":
        return cv2.calcOpticalFlowFarneback(
            ga, gb, None,
            pyr_scale=0.5, levels=5, winsize=11, iterations=3,
            poly_n=5, poly_sigma=1.1, flags=0)
    return _dis_instance(_flow_method).calc(ga, gb, None)


def flow_from_gray(ga, gb, scale=0.5, full_size=None):
    """Fluxo óptico entre dois cinzas já reduzidos (DIS por padrão, ou Farneback).
    O resultado vem em PIXELS DO QUADRO FINAL.
    full_size=(w, h) reescala o campo para tela cheia; None devolve o campo pequeno
    (a GPU amostra com filtro bilinear, então não precisa do resize)."""
    flow = _raw_flow(ga, gb)
    # Zona morta (em baixa resolução, barato): movimentos < ~0.5px do quadro final
    # são ruído do estimador; aplicá-los só borra a imagem.
    fx, fy = cv2.split(flow)
    mag = cv2.magnitude(fx, fy)
    gain = np.clip((mag - 0.4 * scale) / (0.6 * scale), 0.0, 1.0)
    flow = flow * (gain[..., None] / scale)          # já converte para pixels do quadro final
    if full_size is not None and scale != 1.0:
        flow = cv2.resize(flow, full_size, interpolation=cv2.INTER_LINEAR)
    return flow


def estimate_flow(a, b, scale=0.5):
    """Farneback: deslocamento (dx, dy) de cada pixel de `a` até `b`.
    Calculado em resolução reduzida (scale) por velocidade e reescalado depois."""
    h, w = a.shape[:2]
    return flow_from_gray(small_gray(a, scale), small_gray(b, scale), scale, (w, h))


# ----------------------------------------------------------------- warping
_GRIDS = {}


def _grid(h, w):
    """Grade de coordenadas (x, y) intercalada em 2 canais float32: permite montar o mapa
    do remap com UMA operação (scaleAdd) em vez de split + 2 scaleAdd + 2 mapas."""
    g = _GRIDS.get((h, w))
    if g is None:
        if len(_GRIDS) > 4:
            _GRIDS.clear()
        gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        g = cv2.merge([gx, gy])
        _GRIDS[(h, w)] = g
    return g


def warp(img, flow, t):
    """Reposiciona pixels: o pixel q da saída busca sua cor em q - t*flow(q).
    (backward warping: evita buracos, ao contrário de empurrar pixels para frente)."""
    h, w = flow.shape[:2]
    maps = cv2.scaleAdd(flow, -t, _grid(h, w))        # grade - t*flow
    return cv2.remap(img, maps, None, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


# ------------------------------------------------------------ interpolação
def blend(a, b, t):
    """Baseline: mistura simples (gera fantasmas)."""
    return cv2.addWeighted(a, 1.0 - t, b, t, 0)


def interpolate(a, b, t, flow_fw=None, flow_bw=None, occ_thresh=18.0, scale=0.5):
    """Gera o quadro na posição temporal t (0 = a, 1 = b).

    1. flow A->B (forward) e B->A (backward)
    2. warp de A por t e de B por (1-t)
    3. onde os dois warps discordam, o pixel provavelmente é oclusão/erro:
       usa só o quadro temporalmente mais próximo
    Retorna (quadro, máscara_de_oclusão uint8 0..255).
    """
    if flow_fw is None:
        flow_fw = estimate_flow(a, b, scale)
    if flow_bw is None:
        flow_bw = estimate_flow(b, a, scale)

    wa = warp(a, flow_fw, t)
    wb = warp(b, flow_bw, 1.0 - t)
    mixed = cv2.addWeighted(wa, 1.0 - t, wb, t, 0)

    # Oclusão calculada em 1/4 da resolução (o mapa é suave de qualquer jeito)
    h, w = wa.shape[:2]
    small = (max(1, w // 4), max(1, h // 4))
    da = cv2.resize(wa, small, interpolation=cv2.INTER_AREA)
    db = cv2.resize(wb, small, interpolation=cv2.INTER_AREA)
    diff = cv2.absdiff(da, db).astype(np.float32).mean(axis=2)
    diff = cv2.GaussianBlur(diff, (0, 0), 0.8)
    occ = np.clip((diff - occ_thresh) / occ_thresh, 0.0, 1.0)
    mask = cv2.resize((occ * 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_LINEAR)
    if not mask.any():
        return mixed, mask

    nearest = wa if t < 0.5 else wb
    m3 = cv2.merge([mask, mask, mask])
    out = cv2.add(cv2.multiply(mixed, cv2.bitwise_not(m3), scale=1 / 255.0),
                  cv2.multiply(nearest, m3, scale=1 / 255.0))
    return out, mask


class GenRef:
    """Quadro gerado "pendente": guarda só as referências (quadros e fluxos). No overlay, a GPU
    monta a imagem com o shader de interpolação (zero CPU de warp/mistura). render() é o caminho
    de reserva na CPU, usado se o shader não estiver disponível."""
    __slots__ = ("prev", "cur", "fw", "bw", "t", "shape")

    def __init__(self, prev, cur, fw, bw, t):
        self.prev, self.cur, self.fw, self.bw, self.t = prev, cur, fw, bw, t
        self.shape = cur.shape

    def render(self):
        h, w = self.cur.shape[:2]
        fw, bw = self.fw, self.bw
        if fw.shape[:2] != (h, w):                 # fluxo pequeno (feito para a GPU): amplia
            fw = cv2.resize(fw, (w, h), interpolation=cv2.INTER_LINEAR)
            bw = cv2.resize(bw, (w, h), interpolation=cv2.INTER_LINEAR)
        return interpolate(self.prev, self.cur, self.t, fw, bw)[0]


def generate_between(a, b, n_new, **kw):
    """Gera n_new quadros igualmente espaçados entre a e b."""
    flow_fw = estimate_flow(a, b, kw.get("scale", 0.5))
    flow_bw = estimate_flow(b, a, kw.get("scale", 0.5))
    return [interpolate(a, b, k / (n_new + 1), flow_fw, flow_bw, **kw)[0]
            for k in range(1, n_new + 1)]


# -------------------------------------------------------------------- I/O
def read_frames(path, max_frames=None):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"Não consegui abrir {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = []
    while True:
        ok, f = cap.read()
        if not ok or (max_frames and len(frames) >= max_frames):
            break
        frames.append(f)
    cap.release()
    return frames, fps


def write_video(path, frames, fps):
    h, w = frames[0].shape[:2]
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in frames:
        out.write(f)
    out.release()


def psnr(x, y):
    mse = np.mean((x.astype(np.float32) - y.astype(np.float32)) ** 2)
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


# ------------------------------------------------------------------ modos
def cmd_demo(args):
    """Cena sintética: fundo texturizado + objetos rápidos em direções diferentes."""
    rng = np.random.default_rng(1)
    w, h, n = 640, 360, 60
    bg = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), dtype=np.uint8), (0, 0), 6)
    bg = cv2.normalize(bg, None, 40, 200, cv2.NORM_MINMAX)
    frames = []
    for i in range(n):
        f = bg.copy()
        x = int(40 + i * 9) % (w - 80)
        cv2.rectangle(f, (x, 80), (x + 70, 150), (30, 30, 230), -1)
        cv2.circle(f, (w - 60 - (i * 6) % (w - 120), 250), 35, (40, 220, 40), -1)
        cv2.putText(f, "FRAME", (x + 5, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2)
        frames.append(f)
    write_video(args.output, frames, 30)
    print(f"Vídeo de teste salvo em {args.output} ({n} quadros)")


def cmd_eval(args):
    """Pega os quadros i e i+2, gera o do meio e compara com o i+1 real."""
    frames, _ = read_frames(args.input, args.max_frames)
    scores_b, scores_m, times = [], [], []
    best = None
    for i in range(0, len(frames) - 2, args.step):
        a, gt, b = frames[i], frames[i + 1], frames[i + 2]
        t0 = time.perf_counter()
        mid, occ = interpolate(a, b, 0.5, scale=args.scale)
        times.append(time.perf_counter() - t0)
        sb, sm = psnr(blend(a, b, 0.5), gt), psnr(mid, gt)
        scores_b.append(sb)
        scores_m.append(sm)
        if best is None or sm - sb > best[0]:
            best = (sm - sb, a, b, gt, mid, occ)
    print(f"Pares avaliados:        {len(scores_b)}")
    print(f"PSNR mistura simples:   {np.mean(scores_b):.2f} dB")
    print(f"PSNR com movimento:     {np.mean(scores_m):.2f} dB")
    print(f"Ganho médio:            {np.mean(scores_m) - np.mean(scores_b):+.2f} dB")
    print(f"Tempo por quadro (CPU): {np.mean(times) * 1000:.0f} ms")
    _, a, b, gt, mid, occ = best
    occ_img = cv2.cvtColor(occ, cv2.COLOR_GRAY2BGR)
    row1 = np.hstack([blend(a, b, 0.5), mid])
    row2 = np.hstack([gt, occ_img])
    cv2.imwrite("comparacao.png", np.vstack([row1, row2]))
    print("Salvo comparacao.png  [mistura | movimento] / [real | oclusão]")


def cmd_upscale(args):
    frames, fps = read_frames(args.input, args.max_frames)
    n_new = args.factor - 1
    out = []
    t0 = time.perf_counter()
    for i in range(len(frames) - 1):
        out.append(frames[i])
        out.extend(generate_between(frames[i], frames[i + 1], n_new, scale=args.scale))
        if i % 20 == 0:
            print(f"  {i}/{len(frames) - 1}", end="\r")
    out.append(frames[-1])
    write_video(args.output, out, fps * args.factor)
    print(f"\n{len(frames)} -> {len(out)} quadros em {time.perf_counter() - t0:.1f}s "
          f"({fps:.0f} -> {fps * args.factor:.0f} FPS) | {args.output}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo"); d.add_argument("output"); d.set_defaults(fn=cmd_demo)

    e = sub.add_parser("eval"); e.add_argument("input")
    e.add_argument("--step", type=int, default=1)
    e.add_argument("--scale", type=float, default=0.5)
    e.add_argument("--max-frames", type=int, default=300)
    e.add_argument("--flow", choices=FLOW_METHODS, default=None)
    e.set_defaults(fn=cmd_eval)

    u = sub.add_parser("upscale"); u.add_argument("input"); u.add_argument("output")
    u.add_argument("--factor", type=int, default=2)
    u.add_argument("--scale", type=float, default=0.5)
    u.add_argument("--max-frames", type=int, default=None)
    u.add_argument("--flow", choices=FLOW_METHODS, default=None)
    u.set_defaults(fn=cmd_upscale)

    args = p.parse_args()
    if getattr(args, "flow", None):
        set_flow_method(args.flow)
    args.fn(args)


if __name__ == "__main__":
    main()