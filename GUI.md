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

Em **Avançado**: resolução de processamento (menos = mais leve), monitor do overlay, preenchimento da tela (padrão: sem bordas) e a pasta do `live.py`.

Se algo falhar, a última linha de erro aparece no painel e o log completo fica em `~/.cache/framegen-gui/ultima-execucao.log`.

## Desinstalar
```bash
bash install.sh --remove
```
