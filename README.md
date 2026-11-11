# FluxFrame — Frame Generation por Software para Linux

O **FluxFrame** é uma ferramenta avançada de geração de quadros (Frame Generation) em tempo real para Linux. Ele roda **por cima** de qualquer jogo ou aplicativo sem precisar modificá-lo. O software captura a janela do jogo, calcula o movimento entre os quadros via CPU e utiliza a GPU para interpolar, ampliar (upscale) e aplicar nitidez (FSR) em tempo real.

O grande diferencial do FluxFrame é o **Overlay Inteligente**: ele acompanha a janela do jogo automaticamente (mesmo em modo janela), é *click-through* (o mouse e o teclado continuam indo para o jogo) e não rouba o foco, evitando que o jogo pause.

---

## ✨ Funcionalidades Principais

- **Geração de Quadros (FG):** Multiplicador de 2x até 10x na taxa de quadros.
- **Upscaling na GPU (FSR / DLSS):** Shaders customizados via OpenGL, incluindo FSR 1 (EASU + RCAS), FSR 2/3 (Acumulação Temporal) e DLSS 1 (Upscale bilateral GPU-heavy).
- **Resolução Automática:** O overlay assume exatamente o tamanho e a posição da janela do jogo (ou monitor) e a segue se você mover ou redimensionar.
- **Click-Through & Anti-Foco:** Mouse e teclado passam direto para o jogo. O overlay não rouba o foco (evitando telas pretas em jogos que pausam ao perder o foco).
- **Atalhos Globais Nativos (X11):** Funcionam dentro do jogo sem precisar de permissões especiais (como o grupo `input` do evdev).
- **Painel Gráfico (GUI):** Interface amigável em PySide6 para configurar tudo com poucos cliques.
- **Suporte a Wayland e X11:** Captura via `xdg-desktop-portal` + PipeWire (Wayland) ou `mss` (X11).

---

## 🛠️ Requisitos e Instalação

### Instalação Automática (Recomendado - Arch/CachyOS)
Na pasta do projeto, execute:
```bash
bash install.sh
O script fará tudo:

Instala os pacotes necessários do sistema (python, gstreamer, portais, etc.).

Cria um ambiente virtual Python e instala as dependências (numpy, opencv-python, PySide6, PyOpenGL).

Copia os arquivos para ~/.local/share/framegen.

Cria o atalho na Área de Trabalho e no menu do sistema.

Instalação Manual (Outras Distros)
Se você não usa Arch Linux, instale as dependências base:

bash
# Dependências do sistema (exemplo no Ubuntu/Debian)
sudo apt install python3 python3-pip python3-venv python3-gi gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good libx11-6 libxfixes3

# Dependências Python
pip install numpy opencv-python PySide6-Essentials PyOpenGL
🚀 Como Usar
Pelo Painel Gráfico (Desktop)
Abra o Framegen pelo menu do sistema ou pelo atalho na Área de Trabalho.

Escolha o Multiplicador (2x a 10x).

Escolha o Upscaler:

FSR 1: Leve, roda em qualquer GPU.

FSR 2/3: Reduz tremulação (TAA).

DLSS 1: GPU-heavy, alivia a CPU.

Clique em Iniciar Geração de Quadros.

Importante: O diálogo do sistema (portal) vai abrir. Selecione apenas a janela do jogo (não a tela inteira) para que o overlay não capture a si mesmo.

Deixe o jogo em modo Janela ou Tela Cheia Sem Bordas (Borderless).

Pela Linha de Comando (Terminal)
bash
# Exemplo: Capturar janela, overlay ativo, FSR 3, resolução automática
python live.py --screen --overlay --width auto --fsr-mode fsr3 --target-fps 60

# Exemplo: Limitar o jogo a 30fps e gerar quadros para chegar a 60fps
python live.py --screen --overlay --mult 2 --target-fps 60
⌨️ Atalhos Globais (Durante o Jogo)
Você não precisa dar Alt+Tab para controlar o FluxFrame. Os atalhos abaixo funcionam mesmo com o jogo em foco:

Atalho	Ação
Ctrl + Alt + F	Pausa / Retoma a geração de quadros (útil para comparar o ganho).
Ctrl + Alt + H	Mostra / Esconde o HUD (estatísticas no canto da tela).
Ctrl + Alt + N	Fecha o Framegen imediatamente.
Ctrl + Alt + Q	Fecha o Framegen (atalho alternativo).
(Os atalhos são capturados nativamente via X11/XWayland e não são enviados ao jogo, evitando conflitos).

🎮 Modos de Upscale e Interpolação
Modo	Algoritmo	Características
fsr1	EASU 4x4 + RCAS	O mais leve e compatível. Ótimo para GPUs antigas/integradas.
fsr2	FSR 1 + TAA	Adiciona acumulação temporal. Reduz aliasing em movimento.
fsr3	FSR 2 + FG	Combina a estabilidade do FSR 2 com a Geração de Quadros nativa do FluxFrame.
dlss1	Bilateral 6x6 + Sobel	Shader pesado na GPU (faz 144 leituras por pixel). A CPU faz o movimento, a GPU faz o trabalho pesado de upscale.
fsr4	Escala ×N	Modo legado de multiplicação de quadros.
⚙️ Como Funciona a Arquitetura
Captura: O capture_wayland.py usa o portal do sistema e PipeWire para receber os quadros do jogo no tamanho nativo.

Detecção de Janela: O winfind.py usa X11 (XWayland) para achar a janela exata e mover o overlay para cima dela, acompanhando redimensionamentos.

Cálculo de Fluxo: A CPU usa OpenCV (Farneback ou DIS) para calcular os vetores de movimento (Flow) entre o Quadro A e o Quadro B.

Interpolação (GPU): A GPU usa os vetores para gerar os quadros intermediários (via shader OpenGL INTERP_FRAG), evitando buracos e fantasmas.

Upscale (GPU): Se a resolução do processamento for menor que a tela, o shader EASU/RCAS (ou DLSS) amplia e aplica nitidez sem perder performance para a CPU.

Apresentação: O overlay.py desenha o resultado na tela. A janela é "click-through" (os cliques atravessam).

🔧 Solução de Problemas (FAQ)
1. O jogo ficou com a tela preta ao ativar o gerador.

Causa: Muitos jogos pausam (e ficam pretos) quando perdem o foco para outra janela.

Solução: O FluxFrame foi otimizado para não roubar o foco. Porém, se o jogo estiver em modo "Janela" e o overlay for criado, clique na janela do jogo para garantir que ele despause. Recomendamos deixar o jogo em Borderless (Tela Cheia Sem Bordas).

2. A barra de tarefas do Linux (Painel) fica por cima do overlay.

O overlay usa o tamanho exato da janela do jogo (se estiver em modo janela). Se você usar --mask para tela cheia, o painel some, mas o Alt+Tab trava.

Solução para KDE Plasma: Vá em Configurações do Sistema → Gerenciamento de Janelas → Comportamento da Janela → Avançado e desmarque "Mostrar painéis quando uma janela está em tela cheia".

3. O overlay não está aplicando o FSR (imagem parece apenas esticada).

O FSR atua melhor quando há diferença entre a resolução de captura e a resolução de exibição. Se o seu jogo roda em 1080p e o monitor é 1080p, não há upscale. Use o parâmetro --fit stretch ou diminua a resolução do jogo para ver o FSR em ação.

4. O mouse e o teclado não funcionam dentro do jogo com o overlay ativo.

O overlay utiliza a extensão XFixes do X11 (libXfixes3) para recortar a área de input. Verifique se a biblioteca está instalada no sistema. Se estiver usando um ambiente Wayland puramente nativo (sem XWayland), o click-through pode falhar.

5. Onde estão os logs de erro?

Se o painel falhar ao iniciar o jogo, o log completo estará em: ~/.cache/framegen-gui/ultima-execucao.log.

📜 Licença e Créditos
FluxFrame é distribuído sob a Licença de Uso Proprietária (ver LICENSE.txt).

Agradecimentos à AMD pela fórmula do FSR 1 (EASU + RCAS), que serviu de inspiração para os shaders (licença MIT).

Desenvolvido com OpenCV, NumPy, PySide6 e PyOpenGL.

text
