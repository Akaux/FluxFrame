# Framegen Control (painel para KDE Plasma)

## Instalar
```bash
cd framegen-kde
bash install.sh
```
Dê dois cliques em **Framegen** na Área de Trabalho, abra pelo menu do KDE ou rode `framegen`.

Sem instalar: na pasta do projeto, dois cliques em `Framegen.desktop` (ou no arquivo executável `framegen`).

## Usar
1. **Multiplicador** 2× a 10× (quantos quadros finais por quadro do jogo).
2. **FPS alvo**
   - **60 FPS**: saída fixa em 60. A captura vira `60 ÷ multiplicador` (4× → captura 15 fps).
   - **Ilimitado**: sem teto. Você escolhe o fps do jogo (captura) e a saída é `captura × multiplicador`.
3. **Iniciar**: o diálogo do sistema pede a janela do jogo; escolha só ela. O mesmo botão vira **Parar**.
4. Dica: limite o jogo perto do fps de captura; o painel mostra a captura, a saída e a latência estimadas.

Os ajustes ficam travados enquanto roda (mudar exige reiniciar). "Alternar geração ON/OFF" liga/desliga o FG sem fechar o overlay (equivale a Ctrl+Alt+F).

Em **Avançado**: resolução de processamento (menos = mais leve), janela alvo, monitor do overlay, preenchimento da tela e a pasta do `live.py`.

### Resolução "Automática (Janela)"
- O overlay **cobre exatamente a janela do app** (posição e tamanho) em vez do monitor inteiro, e a **segue** se você mover ou redimensionar a janela. Se a janela for minimizada ou fechada, o overlay some; quando ela volta, o overlay volta.
- O processamento usa a largura da janela, limitada a 1280 px (o cálculo do fluxo óptico é na CPU). A imagem é ampliada de volta pelo shader.
- A janela é achada via X11/XWayland: funciona com jogos Proton/Wine e apps X11. **Apps Wayland nativos não são visíveis**; nesse caso o overlay cobre o monitor (use `--geometry x,y,largura,altura` na linha de comando).
- Se o painel escolher a janela errada, escreva em **Janela alvo** parte do título ou da classe da janela certa (ex.: `Cyberpunk`).
- Com uma resolução fixa (720/960/1280 px) o comportamento antigo continua: o overlay cobre o monitor escolhido.

Se algo falhar, a última linha de erro aparece no painel e o log completo fica em `~/.cache/framegen-gui/ultima-execucao.log`.

## Desinstalar
```bash
bash install.sh --remove
```