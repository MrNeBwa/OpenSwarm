#!/usr/bin/env bash
# OpenSwarm installer for Arch Linux (and Arch-based distros).
#   ./install.sh              install deps + opencode + openswarm
#   ./install.sh --uninstall  remove openswarm (leaves opencode and system packages alone)
# Optional env: OPENSWARM_SRC_URL=<git url> to clone the source when this script is run standalone.
set -euo pipefail

APP_DIR="${OPENSWARM_HOME:-$HOME/.local/share/openswarm}"
BIN_DIR="$HOME/.local/bin"
NPM_PREFIX="$HOME/.local"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$APP_DIR" "$BIN_DIR/openswarm" "$HOME/.local/share/applications/dev.openswarm.app.desktop" "$HOME/.local/share/icons/hicolor/scalable/apps/openswarm.svg"
  say "OpenSwarm removed (opencode and system packages were left untouched)."
  exit 0
fi

[[ $EUID -eq 0 ]] && die "Run as a normal user; sudo is used only for pacman."
command -v pacman >/dev/null 2>&1 || die "pacman not found - this installer targets Arch Linux."
SUDO=""; command -v sudo >/dev/null 2>&1 && SUDO="sudo"
[[ -z "$SUDO" ]] && die "sudo is required to install system packages."

# --- 1. system packages -------------------------------------------------------
say "Installing system packages (git, python, nodejs, npm, curl, poppler, bubblewrap)…"
$SUDO pacman -S --needed --noconfirm git python python-pip nodejs npm curl poppler bubblewrap
if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
  say "Installing the native window stack (python-gobject, gtk4, webkitgtk-6.0; set OPENSWARM_SKIP_GUI=1 to skip)…"
  $SUDO pacman -S --needed --noconfirm python-gobject gtk4 webkitgtk-6.0 || warn "GUI libraries failed to install; openswarm gui will fall back to a Chromium window or your browser"
fi

python3 - <<'PY' || die "Python 3.11+ is required."
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

# --- 2. opencode CLI ----------------------------------------------------------
if command -v opencode >/dev/null 2>&1; then
  say "opencode already installed: $(opencode --version 2>/dev/null || echo unknown)"
else
  say "Installing opencode…"
  if pacman -Si opencode >/dev/null 2>&1; then
    $SUDO pacman -S --needed --noconfirm opencode
  elif npm install -g --prefix "$NPM_PREFIX" opencode-ai; then
    :
  else
    warn "npm install failed, trying the official installer script…"
    curl -fsSL https://opencode.ai/install | bash
  fi
fi

# --- 3. locate / fetch OpenSwarm source ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  SRC="$SCRIPT_DIR"
elif [[ -n "${OPENSWARM_SRC_URL:-}" ]]; then
  SRC="$APP_DIR/src"
  say "Cloning $OPENSWARM_SRC_URL …"
  rm -rf "$SRC"; mkdir -p "$APP_DIR"
  git clone --depth 1 "$OPENSWARM_SRC_URL" "$SRC"
else
  die "pyproject.toml not found next to install.sh. Run it from the project, or set OPENSWARM_SRC_URL."
fi

# --- 4. virtualenv + launcher ---------------------------------------------------
say "Creating virtualenv in $APP_DIR/venv and installing OpenSwarm…"
mkdir -p "$APP_DIR" "$BIN_DIR"
python3 -m venv --system-site-packages "$APP_DIR/venv"  # system site-packages so the venv can see python-gobject (GTK)
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "$SRC"
ln -sf "$APP_DIR/venv/bin/openswarm" "$BIN_DIR/openswarm"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add this to your shell rc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- 5. native opencode skills + app-menu launcher ---------------------------------
if [[ -z "${OPENSWARM_SKIP_SKILLS:-}" ]]; then
  say "Installing bundled opencode skills (adds ~/.config/opencode/skill/openswarm-*; set OPENSWARM_SKIP_SKILLS=1 to skip)…"
  PATH="$BIN_DIR:$PATH" openswarm skills install || warn "skill install failed (non-fatal)"
fi
say "Adding OpenSwarm to your application menu (icon + launcher)…"
PATH="$BIN_DIR:$PATH" openswarm desktop-install >/dev/null || warn "could not write the desktop entry (non-fatal)"

if [[ -n "${OPENSWARM_EXTRAS:-}" ]]; then
  say "Installing optional tools (pandoc, LibreOffice, tesseract + eng/rus data)…"
  $SUDO pacman -S --needed --noconfirm pandoc libreoffice-fresh tesseract tesseract-data-eng tesseract-data-rus || warn "extras failed (non-fatal)"
fi
if [[ -n "${OPENSWARM_SELFTEST:-}" ]]; then
  say "Running the bundled self-test (offline, ~30 s)…"
  PATH="$BIN_DIR:$PATH" openswarm selftest || warn "self-test reported failures"
fi

say "Running diagnostics…"
(cd "$SRC" && PATH="$BIN_DIR:$PATH" openswarm --doctor --config /tmp/openswarm-doctor.yaml) || \
  warn "Doctor reported problems (likely 'not a git repo' here, which is fine)."
rm -f /tmp/openswarm-doctor.yaml

cat <<MSG

Done. Next steps:
  1. Log in / pick a provider once:   opencode auth login   (free opencode models need no key)
  2. cd into a git repo with at least one commit
  3. openswarm --doctor --live        (checks YOUR opencode: flags, serve, password, sandbox)  |  openswarm selftest
  4. openswarm gui                     (native window; also in your app menu as 'OpenSwarm')
     openswarm gui --check            (which window backend is used: GTK4/WebKit, Chromium app or browser)
     openswarm                        (terminal UI)   |   openswarm --headless --mode storm "your task"
MSG
