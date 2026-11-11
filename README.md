# Framegen — Frame Generation por software para Linux

Geração de quadros em tempo real que roda **por cima** de qualquer jogo ou programa, sem
modificá-lo. O programa captura a janela, calcula o movimento entre dois quadros, inventa os quadros
que faltam e mostra tudo num overlay **click-through** (mouse e teclado atravessam a janela, então
você continua jogando normalmente). Um shader inspirado no FSR 1 amplia e afia a imagem na GPU.

```
janela do jogo ──(portal + PipeWire)──► captura ──► fluxo óptico ──► interpolação ──► overlay (GPU)
                                         (processo   (CPU, OpenCV)   (CPU, OpenCV)    upscale + nitidez
                                          filho)                                      click-through
```

> **Importante:** é frame generation *por software, na CPU*. Ele consome CPU que o jogo também
> usa, e acrescenta latência (ver [Latência](#latência)). Os números reais da sua máquina estão no
> [benchmark](#benchmark-e-hud).

---

## Índice

1. [Arquivos](#arquivos)
2. [Requisitos e instalação](#requisitos-e-instalação)
3. [Uso rápido](#uso-rápido)
4. [Multiplicador 2x a 10x](#multiplicador-2x-a-10x)
5. [Controles](#controles)
6. [Benchmark e HUD](#benchmark-e-hud)
7. [Referência de opções](#referência-de-opções)
8. [Guia de ajuste de desempenho](#guia-de-ajuste-de-desempenho)
9. [Como funciona por dentro](#como-funciona-por-dentro)
10. [Solução de problemas](#solução-de-problemas)
11. [Limitações conhecidas](#limitações-conhecidas)
12. [Ferramentas offline](#ferramentas-offline)

---

## Arquivos

| Arquivo | Função |
|---|---|
| `live.py` | Programa principal: fontes de vídeo, agendador de quadros, HUD, atalhos globais, benchmark. |
| `overlay.py` | Janela overlay (Qt + OpenGL): upscale/nitidez no shader, click-through, apresentação por prazo. |
| `framegen.py` | Núcleo do algoritmo (fluxo óptico, warp, oclusão) + modos offline `demo`, `eval`, `upscale`. |
| `capture_wayland.py` | Captura no Wayland (xdg-desktop-portal + PipeWire + GStreamer). Roda como processo filho. |
| `bench.py` | Benchmark em tempo real: fps real do jogo, fps de saída, 1% low, médias com FG ligado/desligado. |
| `hotkeys.py` | Versão independente dos atalhos globais (evdev, com fallback pelo portal GlobalShortcuts). **Não é iniciado pelo `live.py`**, que já traz seus próprios atalhos via evdev. Serve como alternativa para quem quiser ligá-lo manualmente. |

Todos os arquivos precisam ficar **na mesma pasta**.

---

## Requisitos e instalação

### Base (sempre)

```bash
pip install numpy opencv-python
```

### Overlay (modo principal)

```bash
pip install PySide6 PyOpenGL
```

O click-through usa `libX11` e `libXfixes` (já presentes em qualquer sistema com X11/XWayland) via
`ctypes`; nada extra para instalar.

### Captura no Wayland

Exemplo para Arch Linux:

```bash
sudo pacman -S python-gobject gst-plugin-pipewire gst-plugins-base gst-plugins-good
```

Se você usa um ambiente virtual, ele precisa enxergar o `gi` do sistema:

```bash
sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' .venv/pyvenv.cfg
```

Também é preciso o portal do seu ambiente: `xdg-desktop-portal-kde`, `-gnome`, `-hyprland` ou `-wlr`.

### Captura no X11 (sem overlay)

```bash
pip install mss
```

### Atalhos globais de teclado (recomendado)

```bash
pip install evdev                    # ou: sudo pacman -S python-evdev
sudo usermod -aG input $USER         # depois, reinicie a sessão
```

Sem isso, os atalhos `Ctrl+Alt+F/H/N` não funcionam, mas os sinais (`pkill -USR1 -f live.py`)
continuam valendo.

---

## Uso rápido

### Overlay sobre um jogo (uso principal)

```bash
python live.py --screen --overlay --fps 30 --width 1280
```

1. Um diálogo do sistema pede para escolher o que capturar. Escolha **só a janela do jogo**
   (no modo overlay, só janelas são oferecidas; capturar a tela inteira capturaria o próprio overlay).
2. Deixe o jogo em **tela cheia sem bordas (borderless)** ou janelado. Tela cheia exclusiva
   normalmente não é capturável.
3. O overlay cobre o monitor e deixa mouse e teclado passarem para o jogo.

### Outros modos

```bash
python live.py --video demo.mp4                 # reproduz um vídeo com FG (teste sem jogo)
python live.py --screen                         # janela comum do framegen (precisa de foco p/ teclas)
python live.py --video demo.mp4 --headless      # sem janela, só imprime estatísticas
python live.py --screen --x11 --region 0,0,1280,720    # X11 puro (mss), recorte da tela
```

### Preencher a tela (`--fit`)

O overlay mantém a proporção da imagem. Se a janela do jogo tiver proporção diferente do monitor,
sobram faixas pretas:

| Valor | Efeito |
|---|---|
| `fit` (padrão) | Mantém a proporção; faixas pretas se diferir do monitor. |
| `stretch` | Preenche tudo, deformando um pouco se as proporções diferem. |
| `crop` | Preenche tudo mantendo a proporção, cortando as bordas. |

Com `stretch`/`crop` o que você vê deixa de coincidir exatamente com o que está sob o mouse. O ideal
é fazer o jogo ocupar o monitor inteiro na mesma proporção dele.

### Vários monitores e recortes

```bash
python live.py --screen --overlay --output 1                 # overlay no 2º monitor (0 = primeiro)
python live.py --screen --overlay --geometry 0,0,1280,720    # overlay só sobre uma região
```

---

## Multiplicador 2x a 10x

`--mult N` (de 2 a 10) define quantos quadros finais saem para cada quadro do jogo.

```bash
python live.py --screen --overlay --mult 4              # captura a 15 fps  -> saída 60 fps
python live.py --screen --overlay --mult 3              # captura a 20 fps  -> saída 60 fps
python live.py --screen --overlay --fps 30 --mult 2     # captura a 30 fps  -> saída 60 fps
python live.py --screen --overlay --fps 20 --mult 3     # captura a 20 fps  -> saída 60 fps
```

Regras:

- **Sem `--fps`:** a captura roda em `target-fps / N` (o alvo padrão é 60). Ex.: `--mult 4` captura a 15 fps.
- **Com `--fps`:** a saída é `fps × N`.
- Sem `--mult`, o fator continua sendo `round(target-fps / fps)`, como antes.

Para o multiplicador fazer sentido, **o jogo deve estar rodando perto da taxa de captura** (por
exemplo, limite o jogo a 15 fps para usar `--mult 4`). Se o jogo entregar menos quadros que a captura,
os repetidos são ignorados; se entregar mais, os extras são descartados e você perde ganho.

Dois cuidados com multiplicadores altos:

- **Não adianta a saída passar da taxa de atualização do monitor.** O overlay mostra no máximo um
  quadro por vsync; o excedente é descartado. Escolha `fps × N` próximo de 60/120/144 Hz.
- **Latência cresce com N** quando a taxa de captura cai (ver [Latência](#latência)). O `--mult 10`
  é para casos em que o jogo roda a poucos fps.

---

## Controles

### Atalhos globais (modo overlay)

Funcionam mesmo com o jogo em foco, sem roubar o teclado dele (leitura via evdev, sem `grab()`):

| Atalho | Ação |
|---|---|
| `Ctrl+Alt+F` | Liga/desliga a geração de quadros (FG ON/OFF) |
| `Ctrl+Alt+H` | Mostra/esconde o HUD |
| `Ctrl+Alt+N` | Fecha o framegen |

Desative com `--no-global-hotkeys`.

### Sinais (alternativa, sem evdev)

Ligue estes comandos a um atalho do seu ambiente (KDE, GNOME, Hyprland...):

```bash
pkill -USR1 -f live.py     # liga/desliga FG
pkill -USR2 -f live.py     # mostra/esconde HUD
pkill -TERM -f live.py     # fecha
```

### Teclas do modo janela (a janela precisa estar em foco)

| Tecla | Ação |
|---|---|
| `F` | Liga/desliga FG |
| `H` | HUD |
| `+` / `-` / `0` | Aumenta / diminui / restaura o tamanho da janela |
| `Z` | Tela cheia |
| `[` / `]` | Menos / mais qualidade do fluxo óptico |
| `Q` ou `Esc` | Sai |

---

## Benchmark e HUD

O HUD (linhas verdes no canto da tela) e o resumo impresso ao sair mostram o ganho **medido**.

```
JOGO  30 fps real | SAIDA  60 fps | FG ON x2 | ganho +100%
1% low  57 fps | frametime 16.7 ms (pico 17.5 ms)
proc 12/33 ms | escala 0.25 | atrasos 0 pulos 0 repetidos 0 descartes 0
media FG ON 60 | OFF 30 | medido +100%
```

| Campo | Significado |
|---|---|
| **JOGO** | fps **real** do jogo: quadros únicos que o portal entrega, antes de qualquer FG. É o que você teria sem o programa. |
| **SAIDA** | Quadros **únicos** apresentados por segundo (originais + gerados). Quadro repetido não conta. |
| **ganho** | `SAIDA / JOGO − 1`. |
| **1% low** | fps equivalente ao intervalo mais longo entre 99% dos quadros (últimos 3 s). É o número que denuncia a variação de fps que a média esconde. |
| **frametime** | Intervalo médio entre quadros e o pior caso (pico). |
| **proc X/Y ms** | Tempo médio de cálculo do FG por par de quadros / orçamento (1 intervalo de captura). Perto de Y = no limite. |
| **escala** | Resolução do fluxo óptico (0.12 a `--scale`). Cai sozinha quando o cálculo não cabe no tempo. |
| **atrasos** | Quadros apresentados depois do horário previsto. |
| **pulos** | Quadros gerados que foram descartados por já estarem fora do prazo. |
| **repetidos** | Quadros de captura ignorados porque o jogo não entregou nada novo. |
| **descartes** | Quadros de captura perdidos porque o processamento não deu conta. |
| **media FG ON \| OFF** | Médias de SAIDA em cada estado. Aparece depois que você alterna o FG. |

### Como medir o ganho real

1. Abra o jogo com o framegen em overlay.
2. Jogue alguns segundos com FG ligado.
3. Aperte `Ctrl+Alt+F` (FG desligado) e jogue alguns segundos na mesma cena.
4. Aperte de novo. A linha **media FG ON | OFF** mostra o ganho medido lado a lado.
5. Feche com `Ctrl+Alt+N`: o terminal imprime o resumo completo.

### Registro em CSV

```bash
python live.py --screen --overlay --fps 30 --bench-log bench.csv
```

Uma linha por segundo, com as colunas:

`t, jogo_fps, saida_fps, low1_fps, fg_on, proc_ms, escala, pulos, repetidos, atrasos`

Útil para comparar configurações (`--width`, `--scale`, `--cv-threads`, `--flow`) entre sessões.

### Como interpretar

- **JOGO acima do seu `--fps`:** a captura está consumindo só parte dos quadros do jogo. O ganho aparece baixo; aumente `--fps`.
- **JOGO abaixo do `--fps`:** os quadros repetidos são ignorados (não gastam CPU), mas o 1% low piora, porque o quadro novo só chega depois do atraso do próprio jogo.
- **proc ≈ orçamento, escala no mínimo (0.12), muitos pulos/atrasos:** a CPU não dá conta. Veja o [guia de ajuste](#guia-de-ajuste-de-desempenho).
- **1% low muito abaixo de SAIDA:** o pacing está irregular (picos de frametime). É o sintoma da "variação" de fps.

---

## Referência de opções

### Fonte

| Opção | Padrão | Descrição |
|---|---|---|
| `--video ARQ` | — | Reproduz um vídeo em tempo real (loop). |
| `--screen` | — | Captura a tela (Wayland: portal + PipeWire; X11: mss). Uma das duas é obrigatória. |
| `--x11` | — | Força o `mss` mesmo no Wayland (não combina com `--overlay`). |
| `--region x,y,l,a` | — | Recorte da tela (só com `--screen --x11`). |
| `--monitor N` | 1 | Monitor do `mss`. |

### Taxas

| Opção | Padrão | Descrição |
|---|---|---|
| `--fps F` | 30 (ou alvo/N com `--mult`) | FPS de captura. |
| `--target-fps F` | 60 | FPS de saída desejado (define o fator quando não há `--mult`). |
| `--mult N` | — | Multiplicador de 2 a 10. Ver [Multiplicador](#multiplicador-2x-a-10x). |

### Qualidade e desempenho

| Opção | Padrão | Descrição |
|---|---|---|
| `--width W` | 960 | Reduz a largura antes de processar (menos CPU, menos nitidez de entrada). |
| `--scale S` | 0.25 | Escala **máxima** do fluxo óptico. |
| `--no-auto` | — | Desliga a qualidade adaptativa (a escala fica fixa). |
| `--flow M` | farneback | `farneback` (melhor qualidade) ou `dis-ultrafast`/`dis-fast`/`dis-medium` (bem mais rápidos, qualidade menor). |
| `--cv-threads N` | 0 | Limita as threads do OpenCV (0 = padrão). Menos threads deixam CPU para o jogo. |

### Overlay

| Opção | Padrão | Descrição |
|---|---|---|
| `--overlay` | — | Mostra por cima da tela, click-through. |
| `--output N` | 0 | Monitor do overlay. |
| `--geometry x,y,l,a` | monitor inteiro | Região do overlay. |
| `--fit M` | fit | `fit`, `stretch` ou `crop`. |
| `--sharpness S` | 0.2 | Nitidez (RCAS): 0 = máxima, 2 = suave, negativo = desliga. |
| `--soft` | — | Sem OpenGL (QPainter), bem mais pesado para a CPU. |
| `--managed` | — | Janela normal (sem override-redirect), se o seu gerenciador de janelas reclamar. |
| `--no-global-hotkeys` | — | Desativa `Ctrl+Alt+F/H/N`. |

### Janela, execução e benchmark

| Opção | Padrão | Descrição |
|---|---|---|
| `--window-scale Z` | 1.0 | Zoom inicial da janela (modo janela). |
| `--headless` | — | Sem janela; imprime o status a cada segundo (8 s por padrão). |
| `--seconds N` | 0 | Duração (0 = até fechar). |
| `--bench-log ARQ.csv` | — | Grava o CSV do benchmark. |

---

## Guia de ajuste de desempenho

O cálculo roda na CPU, dentro de um orçamento de **um intervalo de captura** (33 ms a 30 fps). O
custo vem principalmente de dois pontos: o fluxo óptico (depende de `--scale`) e o warp/mistura em
resolução cheia (depende de `--width`).

Em ordem do que mais costuma ajudar:

1. **Reduza `--width`** (1280 → 960 → 720). Cai o custo de warp e mistura; o shader do overlay amplia de volta.
2. **Use `--cv-threads 2` (ou 3)** se o jogo está perdendo fps: o OpenCV deixa de disputar todos os núcleos com ele.
3. **Reduza `--scale`** (0.25 → 0.2) se o `proc` está perto do orçamento. A qualidade adaptativa já faz isso sozinha, mas um teto menor evita oscilação.
4. **Baixe `--fps`** (30 → 20 com `--mult 3`): mais tempo por par de quadros, mas mais latência.
5. **`--flow dis-fast`** como último recurso. É bem mais rápido, mas na cena de teste perdeu 6–7 dB de PSNR.

Mantenha `fps × N` perto da taxa de atualização do monitor. Use `--bench-log` para comparar cada
mudança com números, não por impressão.

Medidas de referência (vídeo de teste, 30 → 60 fps, máquina de 2 núcleos; a sua será diferente):

| Teste | Original | Atual |
|---|---|---|
| `--width 960`, tempo por par | 24 ms | 18 ms |
| `--width 960`, quadros atrasados | 14 | 3 |
| `--width 1280`, quadros atrasados | 280 | 68 |

---

## Como funciona por dentro

### Fluxo de dados

1. **Captura** (`capture_wayland.py`, processo filho): o portal ScreenCast entrega a janela via
   PipeWire; o GStreamer converte para BGR na largura `--width`. O processo reenvia o **último**
   quadro a taxa constante (`--fps`) pelo `stdout`.
2. **Protocolo** (por quadro): `<IIQ` = largura, altura, **contador de quadros reais** (uint32, uint32,
   uint64, little-endian), seguido de `largura × altura × 3` bytes. O contador só sobe quando o jogo
   entrega um quadro novo, e é dele que vêm o *fps real* e a detecção de repetidos.
3. **Thread de fonte** (`Source`): guarda só o quadro mais recente. Se o processamento atrasa, quadros
   antigos são descartados (e contados) em vez de acumular latência.
4. **Processador** (`processor` em `live.py`): para cada par de quadros únicos calcula o fluxo óptico
   nos dois sentidos e gera os quadros intermediários, cada um com seu horário de exibição.
5. **Overlay** (`overlay.py`): a cada vsync escolhe o quadro mais recente cujo horário já chegou.
   Quadros atrasados são pulados, nunca acumulados.

### Algoritmo de interpolação (`framegen.py`)

1. **Fluxo óptico** Farneback em escala reduzida (`--scale`), nos dois sentidos (A→B e B→A). Uma zona
   morta zera movimentos menores que ~0,5 px (ruído do estimador).
2. **Warp reverso** (*backward warping*): cada pixel de saída busca sua cor em `q − t·fluxo(q)`. Não
   deixa buracos, ao contrário de empurrar pixels para frente.
3. **Mistura:** `A` deformado por `t` e `B` deformado por `1−t`, ponderados.
4. **Oclusão:** onde os dois warps discordam, o pixel provavelmente é oclusão ou erro de fluxo; ali se
   usa só o quadro temporalmente mais próximo. O mapa é calculado em 1/4 da resolução.

Reaproveitamento: o cinza reduzido do quadro anterior é guardado entre pares, e o mapa do `remap` é
montado com uma única operação sobre uma grade de 2 canais.

### Agendamento e latência

Seja `I` o intervalo de captura e `f` o fator (multiplicador). O quadro atual é exibido em
`t_cap + L`, com `L = 2·I − I/f`. Entre dois quadros únicos separados por `gap`, geram-se
`n = round(gap / I · f)` partes, e o quadro `k` é agendado em `t_prev + L + k·gap/n`.

Quando o jogo atrasa e o `gap` passa de 1 intervalo, os primeiros quadros gerados podem já estar fora
do prazo e são pulados (aparecem em "pulos"). Acima de 3,2 intervalos não se interpola.

#### Latência

A imagem mostrada fica atrás da real em cerca de `L`:

| Captura | Multiplicador | `L` aproximado |
|---|---|---|
| 30 fps | 2x | 50 ms |
| 20 fps | 3x | 83 ms |
| 15 fps | 4x | 117 ms |
| 6 fps | 10x | 317 ms |

Por isso multiplicadores altos com captura lenta são mais adequados a conteúdo que tolera latência.

### Upscale e nitidez (shader)

Reimplementação simplificada, **inspirada** no FSR 1 da AMD (MIT); não é o `ffx_fsr1.h` original:

- **EASU-lite:** Lanczos-2 4×4 com kernel orientado pela direção da borda e anti-ringing.
- **RCAS:** nitidez adaptativa ao contraste (fórmula do FSR 1), controlada por `--sharpness`.

A razão de ampliação é a resolução do monitor sobre `--width`; ela **não** depende de `--mult`.

### Click-through (mouse e teclado atravessam)

- A janela é sem borda, sempre no topo, com *override-redirect* (`X11BypassWindowManagerHint`) e roda
  via XWayland (`xcb`), que é o jeito de ficar acima de jogos em tela cheia sem *layer-shell*.
- A região de **entrada** da janela é zerada com XFixes (`ShapeInput`), reaplicada após o `show()` e a
  cada 1,5 s (cobre resize/remapeamento). O compositor então entrega mouse e teclado à janela de baixo.
- A janela tem `WindowDoesNotAcceptFocus`; se algum dia receber foco, o terminal avisa.
- O cursor real do sistema fica por cima do overlay (a captura o oculta), sem atraso extra.
- Os atalhos globais usam evdev **sem** `grab()`, então as teclas continuam chegando ao jogo.

---

## Solução de problemas

| Sintoma | Causa provável / o que fazer |
|---|---|
| `ModuleNotFoundError: bench` | `bench.py` não está na mesma pasta do `live.py`. |
| Imagem corrompida ou a captura não inicia depois de atualizar | `live.py` e `capture_wayland.py` são de versões diferentes (o cabeçalho passou de 8 para 16 bytes). Atualize os dois. |
| `Dependências da captura no Wayland ausentes` | Instale os pacotes da seção de captura e habilite `include-system-site-packages` no venv. |
| Diálogo do portal não aparece / é recusado | Confirme o `xdg-desktop-portal-*` do seu ambiente e que o PipeWire está rodando. |
| Tela preta no overlay | Veja a linha `Primeiro quadro: ... brilho médio` no terminal; 0 = preto (a captura não está recebendo imagem). |
| Mouse/teclado não chegam ao jogo | Procure `click-through não confirmado` ou `o overlay recebeu foco` no terminal. Tente `--managed`. Informe seu compositor. |
| Overlay não aparece sobre o jogo | O jogo provavelmente está em tela cheia **exclusiva**; use borderless/janelado. |
| Faixas pretas nas laterais/topo | Proporção do jogo ≠ monitor. Use `--fit stretch` ou `crop`, ou `--geometry`. |
| `Atalhos globais indisponíveis` | `pip install evdev` e `sudo usermod -aG input $USER` (reinicie a sessão). Use os sinais enquanto isso. |
| `OpenGL indisponível` | Cai para QPainter (lento). Verifique PyOpenGL e o driver; o overlay precisa de OpenGL 3.3 core. |
| fps de saída oscila | Veja `proc`, `pulos`, `atrasos` e o 1% low. Aplique o [guia de ajuste](#guia-de-ajuste-de-desempenho). |
| Ganho aparece baixo | `JOGO` acima do `--fps` (aumente-o) ou `fps × N` acima do refresh do monitor. |
| Fantasmas/borrões em movimentos rápidos | Limitação do fluxo óptico em CPU. Suba `--scale`/`--width` se o `proc` permitir. |

---

## Limitações conhecidas

- É FG **por software, na CPU**: disputa CPU com o jogo e funciona melhor em jogos com pouca carga de CPU.
- **Latência de entrada** adicional (ver tabela). Não é recomendado para jogos competitivos.
- Sem acesso a vetores de movimento nem profundidade do jogo: o fluxo é estimado só pela imagem, então
  texto, HUD e partículas podem apresentar artefatos.
- O FSR do shader é uma reimplementação simplificada, não o original da AMD.
- O jogo precisa estar em **janela ou borderless**; tela cheia exclusiva não é capturada de forma confiável.
- O fps "real" medido é o que o portal entrega, que normalmente acompanha o fps do jogo, mas não é lido
  de dentro do jogo.
- Quando o jogo entrega menos quadros que a captura, o pacing fica irregular (1% low cai).
- O overlay depende de XWayland; em ambientes sem ele, o modo overlay não funciona.
- Testado em ambiente headless e com a captura simulada; o comportamento visual no seu compositor e no
  seu jogo depende da sua configuração.

---

## Ferramentas offline

`framegen.py` serve para avaliar a qualidade do algoritmo sem jogo nenhum.

```bash
python framegen.py demo demo.mp4                                  # vídeo sintético com movimento rápido
python framegen.py eval demo.mp4                                  # PSNR: mistura simples x movimento
python framegen.py eval demo.mp4 --flow dis-fast --scale 0.5      # compara estimadores
python framegen.py upscale in.mp4 out.mp4 --factor 3              # gera um vídeo com 3x mais fps
```

O `eval` pega os quadros `i` e `i+2`, gera o do meio e compara com o `i+1` real. Imprime PSNR da
mistura simples, PSNR com movimento, o ganho em dB e o tempo por quadro, e salva `comparacao.png`
(`[mistura | movimento] / [real | oclusão]`).
