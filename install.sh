#!/usr/bin/env bash
# Instala o Framegen + painel no CachyOS/Arch (KDE Plasma). Sem root, exceto para os pacotes do pacman.
#   bash install.sh            instala
#   bash install.sh --remove   desinstala tudo
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/framegen"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"

if [[ "${1:-}" == "--remove" ]]; then
  DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  rm -rf "$DEST" "$BIN/framegen" "$BIN/framegen-gui" "$APPS/framegen.desktop" "$ICONS/framegen.svg" \
         "$DESK/Framegen.desktop" \
         "$HOME/.cache/framegen-gui" "$HOME/.config/framegen"
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "Framegen removido."; exit 0
fi

[[ -f "$SRC/live.py" && -f "$SRC/framegen_gui.py" ]] || { echo "Rode na pasta que tem live.py e framegen_gui.py"; exit 1; }

echo "==> Pacotes do sistema (captura Wayland, portal, atalhos globais)"
sudo pacman -S --needed --noconfirm python python-gobject python-evdev \
  gst-plugin-pipewire gst-plugins-base gst-plugins-good xdg-desktop-portal-kde

echo "==> Copiando arquivos para $DEST"
mkdir -p "$DEST" "$BIN" "$APPS" "$ICONS"
cp "$SRC"/{live,overlay,framegen,capture_wayland,bench,hotkeys,winfind,framegen_gui}.py "$DEST/"
cp "$SRC/framegen" "$DEST/framegen" && chmod +x "$DEST/framegen"   # lançador (sem extensão)
cp "$SRC/framegen.svg" "$DEST/" && cp "$SRC/framegen.svg" "$ICONS/framegen.svg"

echo "==> Ambiente Python (enxerga o gi/evdev do sistema)"
[[ -x "$DEST/.venv/bin/python" ]] || python -m venv --system-site-packages "$DEST/.venv"
if ! "$DEST/.venv/bin/pip" install --upgrade numpy opencv-python PySide6-Essentials PyOpenGL; then
  echo "pip falhou; usando os pacotes do pacman (maiores)."
  sudo pacman -S --needed --noconfirm pyside6 python-numpy python-opencv python-pyopengl
fi

# comando no PATH + entrada no menu + atalho de duplo clique na Área de Trabalho
ln -sf "$DEST/framegen" "$BIN/framegen"
ln -sf "$DEST/framegen" "$BIN/framegen-gui"        # nome antigo continua funcionando

DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$DESK"
for target in "$APPS/framegen.desktop" "$DESK/Framegen.desktop"; do
  cat > "$target" <<ENTRY
[Desktop Entry]
Type=Application
Name=Framegen
GenericName=Geração de quadros
Comment=Frame generation por software para jogos, em overlay
Exec=$DEST/framegen
Icon=framegen
Categories=Game;Utility;
Keywords=fps;frame generation;overlay;
Terminal=false
StartupWMClass=framegen
ENTRY
  chmod +x "$target"
done
gio set "$DESK/Framegen.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$APPS" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo
echo "Pronto. Dê dois cliques em \"Framegen\" na Área de Trabalho, abra pelo menu do KDE ou rode: framegen"
if ! id -nG | grep -qw input; then
  echo
  echo "Atalhos globais (Ctrl+Alt+F/H/N) precisam do grupo 'input':"
  echo "  sudo usermod -aG input \$USER   (depois saia e entre na sessão)"
fi
[[ ":$PATH:" == *":$BIN:"* ]] || echo "Obs.: adicione $BIN ao PATH (fish: fish_add_path ~/.local/bin)."