#!/bin/sh
# Clean Mic uninstaller.
#   curl -L https://github.com/VetTV-Studios/clean-mic/raw/main/uninstall.sh | sh
#   PURGE=1 ... also deletes settings and logs.
# Run as your normal user: it restores your default microphone in *your* audio session first.
set -eu

PLUGIN_DIR_NAME="Clean Mic"
PLUGINS_DIR="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
SETTINGS_DIR="$HOME/homebrew/settings/clean-mic"
DEST="$PLUGINS_DIR/$PLUGIN_DIR_NAME"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘ Error:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root (it needs your audio session)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# --- 1. give the default mic back -----------------------------------------
STATE="$SETTINGS_DIR/state.json"
if [ -f "$STATE" ] && grep -q '"we_set_default": *true' "$STATE"; then
    PREV="$(sed -n 's/.*"prev_default": *"\([^"]*\)".*/\1/p' "$STATE" | head -n 1)"
    if [ -n "$PREV" ] && command -v pactl >/dev/null 2>&1 && pactl set-default-source "$PREV" 2>/dev/null; then
        ok "Default microphone restored to $PREV"
    elif command -v pw-metadata >/dev/null 2>&1 && pw-metadata -d 0 default.configured.audio.source >/dev/null 2>&1; then
        ok "Default microphone reset to automatic"
    else
        info "Could not restore the default microphone; pick one in Settings > Audio."
    fi
fi

# --- 2. stop the filter process --------------------------------------------
if command -v pkill >/dev/null 2>&1; then
    if pkill -TERM -f "homebrew/settings/clean-mic/clean-mic.conf" 2>/dev/null; then
        ok "Stopped the Clean Mic filter"
    fi
fi

# --- 3. remove the plugin ---------------------------------------------------
if [ -e "$DEST" ]; then
    if [ -w "$PLUGINS_DIR" ]; then SUDO=""; else
        command -v sudo >/dev/null 2>&1 || die "sudo is required ($PLUGINS_DIR is not writable)"
        SUDO="sudo"; info "Administrator rights are needed to remove $DEST."
    fi
    $SUDO rm -rf "$DEST"
    ok "Removed $DEST"
else
    SUDO="sudo -n"
    info "Plugin folder not found ($DEST); nothing to remove."
fi

if [ "${PURGE:-0}" = "1" ]; then
    for d in "$SETTINGS_DIR" "$HOME/homebrew/settings/$PLUGIN_DIR_NAME" "$HOME/homebrew/logs/$PLUGIN_DIR_NAME"; do
        [ -e "$d" ] || continue
        rm -rf "$d" 2>/dev/null || sudo rm -rf "$d"
    done
    ok "Removed settings and logs"
fi

if command -v systemctl >/dev/null 2>&1 && ${SUDO:-sudo -n} systemctl restart plugin_loader 2>/dev/null; then
    ok "Restarted Decky Loader (plugin_loader)."
else
    printf 'Restart Decky to finish: sudo systemctl restart plugin_loader (or reboot)\n' >&2
fi
ok "Clean Mic is uninstalled. Nothing else on the system was changed."
