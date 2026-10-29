#!/bin/sh
set -eu

uuid='attendo@mnasih.local'
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
extension_dir="$data_home/gnome-shell/extensions/$uuid"

install -d "$extension_dir"
install -m 0644 "$script_dir/gnome-extension/extension.js" "$extension_dir/extension.js"
install -m 0644 "$script_dir/gnome-extension/metadata.json" "$extension_dir/metadata.json"
install -m 0755 "$script_dir/attendo.py" "$extension_dir/attendo.py"

printf '%s\n' "Installed Attendo Panel in $extension_dir"
printf '%s\n' 'Restart GNOME Shell (Alt+F2, type r, then Enter), then run:'
printf '  gnome-extensions enable %s\n' "$uuid"
