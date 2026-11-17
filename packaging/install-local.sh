#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

mkdir -p "$bin_dir" "$applications_dir" "$icons_dir"
install -Dm755 "$project_dir/packaging/biblioteca-de-ideias" \
  "$bin_dir/biblioteca-de-ideias"
sed -i "s|@PROJECT_DIR@|$project_dir|g" "$bin_dir/biblioteca-de-ideias"
install -Dm644 "$project_dir/packaging/br.com.torugo.bibliotecadeideias.desktop" \
  "$applications_dir/br.com.torugo.bibliotecadeideias.desktop"
install -Dm644 "$project_dir/assets/br.com.torugo.bibliotecadeideias.svg" \
  "$icons_dir/br.com.torugo.bibliotecadeideias.svg"
update-desktop-database "$applications_dir"
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi
echo "Biblioteca de Ideias instalada no menu de aplicativos."

