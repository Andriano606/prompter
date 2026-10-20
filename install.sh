#!/usr/bin/env bash
# Installs Prompter and its GNOME Shell extension for the current user.
#   ./install.sh              install / update
#   ./install.sh --uninstall  remove (settings in ~/.config/prompter stay)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
UUID="prompter@andrii.local"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
APP_DIR="$HOME/.local/share/prompter"
BIN="$HOME/.local/bin/prompter"
DESKTOP="$HOME/.local/share/applications/org.andrii.Prompter.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/org.andrii.Prompter.svg"

# The first version registered GNOME custom shortcuts (prompter-*) and its own
# .desktop file; they would block the extension from grabbing the same keys.
remove_legacy() {
    rm -f "$HOME/.local/share/applications/com.andrii.Prompter.desktop"
    python3 - <<'PY'
from gi.repository import Gio
KEYS = "org.gnome.settings-daemon.plugins.media-keys"
s = Gio.Settings.new(KEYS)
paths = s.get_strv("custom-keybindings")
for p in [p for p in paths if "/prompter-" in p]:
    kb = Gio.Settings.new_with_path(KEYS + ".custom-keybinding", p)
    for key in ("name", "command", "binding"):
        kb.reset(key)
s.set_strv("custom-keybindings", [p for p in paths if "/prompter-" not in p])
Gio.Settings.sync()
PY
}

if [[ "${1:-}" == "--uninstall" ]]; then
    gnome-extensions disable "$UUID" 2>/dev/null || true
    rm -rf "$EXT_DIR" "$APP_DIR"
    rm -f "$BIN" "$DESKTOP" "$ICON"
    remove_legacy
    echo "Prompter видалено."
    exit 0
fi

remove_legacy
mkdir -p "$EXT_DIR" "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
cp "$SRC/extension/metadata.json" "$SRC/extension/extension.js" "$EXT_DIR/"
cp "$SRC/prompter.py" "$APP_DIR/"
chmod +x "$APP_DIR/prompter.py"
ln -sf "$APP_DIR/prompter.py" "$BIN"
cp "$SRC/data/org.andrii.Prompter.svg" "$ICON"

cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=Prompter
Name[uk]=Суфлер
Comment=Teleprompter that stays above all windows
Comment[uk]=Суфлер поверх усіх вікон з глобальними гарячими клавішами
Exec=$BIN %f
Icon=org.andrii.Prompter
Terminal=false
Categories=Utility;GTK;
Keywords=teleprompter;prompter;суфлер;текст;
MimeType=text/plain;text/markdown;
StartupNotify=true
DESK
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# Enable the extension (a new extension loads only after re-login on Wayland).
if gnome-extensions list 2>/dev/null | grep -qx "$UUID"; then
    gnome-extensions enable "$UUID" || true
else
    current=$(gsettings get org.gnome.shell enabled-extensions)
    if [[ "$current" != *"$UUID"* ]]; then
        if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
            gsettings set org.gnome.shell enabled-extensions "['$UUID']"
        else
            gsettings set org.gnome.shell enabled-extensions "${current%]*}, '$UUID']"
        fi
    fi
fi

if gdbus call --session --dest org.andrii.Prompter.Shell --object-path /org/andrii/Prompter/Shell \
        --method org.andrii.Prompter.Shell.ShortcutStatus >/dev/null 2>&1; then
    echo "Готово. Запустіть «Prompter» з меню програм."
else
    echo "Встановлено. Вийдіть із сесії й увійдіть знову, щоб GNOME Shell завантажив розширення,"
    echo "потім запустіть «Prompter» з меню програм."
fi
