#!/usr/bin/env bash
set -euo pipefail

applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

rm -f "${XDG_BIN_HOME:-$HOME/.local/bin}/biblioteca-de-ideias"
rm -f "$applications_dir/br.com.torugo.bibliotecadeideias.desktop"
rm -f "$icons_dir/br.com.torugo.bibliotecadeideias.svg"
update-desktop-database "$applications_dir"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi
echo "Aplicativo removido. O cofre local foi preservado."

