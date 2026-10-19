#!/bin/sh
# Clean Mic installer / updater for Decky Loader (no developer mode needed).
#   curl -L https://github.com/VetTV-Studios/clean-mic/raw/main/install.sh | sh
# Run as your normal user (Desktop Mode > Konsole, or over SSH). Safe to re-run to update.
# sudo is only used if Decky's plugin folder isn't writable by you (it usually isn't on
# SteamOS/Bazzite, because Decky runs as root) and to restart Decky afterwards.
set -eu

REPO="VetTV-Studios/clean-mic"
PLUGIN_DIR_NAME="Clean Mic"   # folder name inside the release zip
FALLBACK_URL="https://github.com/$REPO/releases/latest/download/clean-mic.zip"
PLUGINS_DIR="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘ Error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
[ -d "$PLUGINS_DIR" ] || die "Decky Loader not found ($PLUGINS_DIR missing). Install Decky first: https://decky.xyz"
command -v curl >/dev/null 2>&1 || [ -n "${CLEAN_MIC_ZIP:-}" ] || die "curl is required"

if [ "$(id -u)" -eq 0 ] || [ -w "$PLUGINS_DIR" ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || die "sudo is required ($PLUGINS_DIR is not writable)"
    SUDO="sudo"
    info "Administrator rights are needed to write to $PLUGINS_DIR."
    info "If you never set a password, run 'passwd' in Konsole first."
    $SUDO -v || die "sudo authentication failed"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/clean-mic.XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

# --- find & download -------------------------------------------------------
if [ -n "${CLEAN_MIC_ZIP:-}" ]; then          # local zip override (testing/offline)
    info "Using local zip: $CLEAN_MIC_ZIP"
    cp "$CLEAN_MIC_ZIP" "$TMP/plugin.zip" || die "cannot read $CLEAN_MIC_ZIP"
else
    info "Looking up the latest release..."
    URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -n 1 | sed 's/.*"\(https[^"]*\)"/\1/' || true)"
    if [ -z "$URL" ]; then
        info "GitHub API unavailable or no release yet; trying the fallback URL."
        URL="$FALLBACK_URL"
    fi
    case "$URL" in
        "https://github.com/$REPO/releases/"*) ;;
        *) die "unexpected download URL: $URL" ;;
    esac
    info "Downloading $URL"
    curl -fL --retry 3 -o "$TMP/plugin.zip" "$URL" \
        || die "download failed (no release published yet? use CLEAN_MIC_ZIP=/path/to/clean-mic.zip)"
fi

# --- validate & extract ----------------------------------------------------
command -v python3 >/dev/null 2>&1 || command -v unzip >/dev/null 2>&1 || die "need python3 or unzip"
if command -v python3 >/dev/null 2>&1; then
    # Refuse absolute paths, '..' and anything outside "$PLUGIN_DIR_NAME/" before extracting.
    python3 - "$TMP/plugin.zip" "$PLUGIN_DIR_NAME" <<'PY' || die "zip failed validation"
import sys, zipfile, posixpath
z = zipfile.ZipFile(sys.argv[1]); top = sys.argv[2] + "/"
for n in z.namelist():
    p = posixpath.normpath(n)
    if n.startswith("/") or p == ".." or p.startswith("../") or not (n == top or n.startswith(top)):
        sys.exit("bad member: " + n)
z.testzip() is None or sys.exit("corrupt zip")
z.extractall(sys.argv[1] + ".d")
PY
    mv "$TMP/plugin.zip.d" "$TMP/x"
else
    if unzip -Z1 "$TMP/plugin.zip" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then die "zip has unsafe paths"; fi
    mkdir "$TMP/x"
    unzip -q "$TMP/plugin.zip" -d "$TMP/x" || die "zip is corrupt"
fi
SRC="$TMP/x/$PLUGIN_DIR_NAME"
for f in plugin.json main.py cleanmic.py dist/index.js package.json; do
    [ -f "$SRC/$f" ] || die "unexpected zip layout (missing '$PLUGIN_DIR_NAME/$f')"
done
VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SRC/package.json" | head -n 1)"

# --- install atomically ----------------------------------------------------
DEST="$PLUGINS_DIR/$PLUGIN_DIR_NAME"
STAGE="$PLUGINS_DIR/.clean-mic.new"
OLD="$PLUGINS_DIR/.clean-mic.old"
$SUDO rm -rf "$STAGE" "$OLD"
$SUDO cp -r "$SRC" "$STAGE" || die "could not write to $PLUGINS_DIR"
if [ -e "$DEST" ]; then
    $SUDO mv "$DEST" "$OLD"
fi
if ! $SUDO mv "$STAGE" "$DEST"; then
    [ -e "$OLD" ] && $SUDO mv "$OLD" "$DEST"
    die "could not install into $DEST (previous version restored)"
fi
$SUDO rm -rf "$OLD"
ok "Installed Clean Mic ${VERSION:-?} to $DEST"

# --- check the engine (informational) --------------------------------------
found=""
for d in /usr/lib64/ladspa /usr/lib/ladspa /usr/lib/x86_64-linux-gnu/ladspa "$HOME/homebrew/data/clean-mic/ladspa"; do
    if [ -f "$d/librnnoise_ladspa.so" ]; then found="$d/librnnoise_ladspa.so"; break; fi
done
if [ -n "$found" ]; then
    ok "RNNoise found: $found"
else
    info "RNNoise (librnnoise_ladspa.so) not found; Clean Mic will use PipeWire's WebRTC fallback."
    info "See https://github.com/$REPO#rnnoise to add it."
fi

# --- restart Decky ---------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then RESTART=""; elif [ -n "$SUDO" ]; then RESTART="sudo"; else RESTART="sudo -n"; fi
if command -v systemctl >/dev/null 2>&1 && $RESTART systemctl restart plugin_loader 2>/dev/null; then
    ok "Restarted Decky Loader (plugin_loader)."
else
    printf 'Could not restart plugin_loader; reboot or run: sudo systemctl restart plugin_loader\n' >&2
fi
ok "Done. Open Quick Access > Decky > Clean Mic (Game Mode)."
