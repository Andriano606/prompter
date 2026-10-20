#!/usr/bin/env bash
# Runs a command inside an isolated headless GNOME Shell with the extension
# from this repo enabled. Nothing touches the real session: own D-Bus, own
# XDG dirs, keyfile GSettings, and WAYLAND_DISPLAY of the headless compositor.
#   tests/integration/headless_shell.sh python3 tests/integration/scenario.py
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UUID="prompter@andrii.local"
TEST_UUID="unsafe-mode@prompter.test"  # enables Shell.Eval for the scenario

if [[ -z "${_PR_INNER:-}" ]]; then
    command -v gnome-shell >/dev/null || { echo "gnome-shell not found" >&2; exit 77; }
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    mkdir -p "$TMP/gnome-shell/extensions" "$TMP/config/glib-2.0/settings"
    ln -s "$REPO/extension" "$TMP/gnome-shell/extensions/$UUID"
    ln -s "$REPO/tests/integration/$TEST_UUID" "$TMP/gnome-shell/extensions/$TEST_UUID"
    : >"$TMP/config/glib-2.0/settings/keyfile"
    export XDG_DATA_HOME="$TMP" XDG_CONFIG_HOME="$TMP/config" GSETTINGS_BACKEND=keyfile \
        _PR_INNER=1 _PR_TMP="$TMP"
    dbus-run-session -- "$0" "$@"
    exit $?
fi

gnome-shell --headless --wayland --no-x11 --virtual-monitor 1920x1080 \
    >"$_PR_TMP/shell.log" 2>&1 &
SHELL_PID=$!
trap 'kill $SHELL_PID 2>/dev/null; wait $SHELL_PID 2>/dev/null || true' EXIT

call() { gdbus call --session "$@" >/dev/null 2>&1; }
for _ in $(seq 60); do
    call --dest org.gnome.Shell.Extensions --object-path /org/gnome/Shell/Extensions \
        --method org.gnome.Shell.Extensions.ListExtensions && break
    sleep 0.5
done
for ext in "$TEST_UUID" "$UUID"; do
    call --dest org.gnome.Shell.Extensions --object-path /org/gnome/Shell/Extensions \
        --method org.gnome.Shell.Extensions.EnableExtension "$ext"
done
for _ in $(seq 20); do
    call --dest org.andrii.Prompter.Shell --object-path /org/andrii/Prompter/Shell \
        --method org.andrii.Prompter.Shell.ShortcutStatus && break
    sleep 0.5
done

WAYLAND_DISPLAY="$(grep -oP "Using Wayland display name '\K[^']+" "$_PR_TMP/shell.log" | head -1)"
if [[ -z "$WAYLAND_DISPLAY" ]]; then
    echo "headless shell did not report its Wayland display" >&2
    exit 1
fi
export WAYLAND_DISPLAY
unset DISPLAY

set +e
"$@"
rc=$?
if [[ $rc -ne 0 ]]; then
    echo "---- gnome-shell log (JS errors) ----" >&2
    grep -E "JS ERROR|prompter|Extension" "$_PR_TMP/shell.log" >&2 || true
fi
exit $rc
