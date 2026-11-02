#!/usr/bin/env bash
# OpenSwarm Universal Linux Installer
# Supports: Ubuntu, Debian, Pop!_OS, Linux Mint, Fedora, RHEL, Rocky, Alma, Arch Linux, Manjaro, openSUSE, Alpine.
#
# Usage:
#   ./install.sh              install dependencies + opencode + openswarm
#   ./install.sh --uninstall  remove openswarm
#
# Environment variables:
#   OPENSWARM_SRC_URL=<git url>   clone source when running standalone from curl
#   OPENSWARM_SKIP_GUI=1          skip installing GTK4/WebKitGTK packages
#   OPENSWARM_EXTRAS=1            install extra document/OCR tools (pandoc, libreoffice, tesseract)
#   OPENSWARM_SELFTEST=1          run offline selftest after installation
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

[[ $EUID -eq 0 ]] && die "Run as a normal user; sudo is used only for system package manager."
SUDO=""
if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
elif [[ $EUID -ne 0 ]]; then
  die "sudo is required to install system packages."
fi

# --- 1. Distro Detection -----------------------------------------------------
DISTRO="unknown"
DISTRO_NAME="Linux"
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  DISTRO_NAME="${NAME:-Linux}"
fi

say "Detected Linux Distribution: $DISTRO_NAME ($DISTRO)"

# --- 2. System Packages ------------------------------------------------------
install_system_packages() {
  case "$DISTRO" in
    arch|manjaro|endeavouros|artix|garuda)
      say "Using pacman package manager…"
      $SUDO pacman -S --needed --noconfirm git python python-pip nodejs npm curl poppler bubblewrap
      if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
        say "Installing native GTK4 + WebKitGTK stack…"
        $SUDO pacman -S --needed --noconfirm python-gobject gtk4 webkitgtk-6.0 || warn "GUI libraries failed; will fall back to Chromium or browser."
      fi
      if [[ -n "${OPENSWARM_EXTRAS:-}" ]]; then
        $SUDO pacman -S --needed --noconfirm pandoc libreoffice-fresh tesseract tesseract-data-eng tesseract-data-rus || true
      fi
      ;;
    ubuntu|debian|pop|mint|elementary|kali|raspbian)
      say "Using apt package manager…"
      $SUDO apt-get update -y
      $SUDO apt-get install -y git python3 python3-pip python3-venv nodejs npm curl poppler-utils bubblewrap
      if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
        say "Installing native GTK4 + WebKitGTK stack…"
        $SUDO apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-webkit-6.0 libwebkitgtk-6.0-4 || \
        $SUDO apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-webkit2-4.1 || warn "GUI libraries failed; will fall back to Chromium or browser."
      fi
      if [[ -n "${OPENSWARM_EXTRAS:-}" ]]; then
        $SUDO apt-get install -y pandoc libreoffice tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus || true
      fi
      ;;
    fedora|rhel|centos|rocky|almalinux)
      PKG_MGR="dnf"
      command -v dnf >/dev/null 2>&1 || PKG_MGR="yum"
      say "Using $PKG_MGR package manager…"
      $SUDO $PKG_MGR install -y git python3 python3-pip nodejs npm curl poppler-utils bubblewrap
      if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
        say "Installing native GTK4 + WebKitGTK stack…"
        $SUDO $PKG_MGR install -y python3-gobject gtk4 webkitgtk6.0 || warn "GUI libraries failed; will fall back to Chromium or browser."
      fi
      if [[ -n "${OPENSWARM_EXTRAS:-}" ]]; then
        $SUDO $PKG_MGR install -y pandoc libreoffice tesseract tesseract-langpack-rus || true
      fi
      ;;
    opensuse*|suse)
      say "Using zypper package manager…"
      $SUDO zypper --non-interactive install git python3 python3-pip nodejs npm curl poppler-tools bubblewrap
      if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
        say "Installing native GTK4 + WebKitGTK stack…"
        $SUDO zypper --non-interactive install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-WebKit-6_0 || warn "GUI libraries failed; will fall back to Chromium or browser."
      fi
      ;;
    alpine)
      say "Using apk package manager…"
      $SUDO apk add git python3 py3-pip nodejs npm curl poppler-utils bubblewrap
      if [[ -z "${OPENSWARM_SKIP_GUI:-}" ]]; then
        $SUDO apk add py3-gobject gtk4.0 webkit2gtk-6.0 || warn "GUI libraries failed; will fall back to Chromium or browser."
      fi
      ;;
    *)
      warn "Unknown Linux distribution ($DISTRO). Checking for available package managers…"
      if command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm git python python-pip nodejs npm curl poppler bubblewrap
      elif command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update -y && $SUDO apt-get install -y git python3 python3-pip python3-venv nodejs npm curl poppler-utils bubblewrap
      elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y git python3 python3-pip nodejs npm curl poppler-utils bubblewrap
      elif command -v zypper >/dev/null 2>&1; then
        $SUDO zypper --non-interactive install git python3 python3-pip nodejs npm curl poppler-tools bubblewrap
      else
        die "No supported package manager found (pacman, apt, dnf, zypper, apk). Please install git, python3, nodejs, npm, curl manually."
      fi
      ;;
  esac
}

install_system_packages

python3 - <<'PY' || die "Python 3.11+ is required."
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

# --- 3. OpenCode CLI ---------------------------------------------------------
if command -v opencode >/dev/null 2>&1; then
  say "opencode already installed: $(opencode --version 2>/dev/null || echo unknown)"
else
  say "Installing opencode CLI…"
  if [[ "$DISTRO" =~ ^(arch|manjaro|endeavouros)$ ]] && pacman -Si opencode >/dev/null 2>&1; then
    $SUDO pacman -S --needed --noconfirm opencode
  elif npm install -g --prefix "$NPM_PREFIX" opencode-ai; then
    say "Installed opencode via npm to $NPM_PREFIX/bin"
  else
    warn "npm install failed, running the official opencode installer script…"
    curl -fsSL https://opencode.ai/install | bash
  fi
fi

# Ensure npm prefix bin is on PATH for current session
export PATH="$NPM_PREFIX/bin:$BIN_DIR:$PATH"

# --- 4. Locate / Fetch OpenSwarm Source ---------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  SRC="$SCRIPT_DIR"
elif [[ -n "${OPENSWARM_SRC_URL:-}" ]]; then
  SRC="$APP_DIR/src"
  say "Cloning $OPENSWARM_SRC_URL …"
  rm -rf "$SRC"; mkdir -p "$APP_DIR"
  git clone --depth 1 "$OPENSWARM_SRC_URL" "$SRC"
else
  die "pyproject.toml not found next to install.sh. Run it from the project directory, or set OPENSWARM_SRC_URL."
fi

# --- 5. Virtualenv & Python dependencies --------------------------------------
say "Setting up virtual environment in $APP_DIR/venv …"
mkdir -p "$APP_DIR" "$BIN_DIR"
python3 -m venv --system-site-packages "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -e "$SRC"
ln -sf "$APP_DIR/venv/bin/openswarm" "$BIN_DIR/openswarm"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add this line to your ~/.bashrc or ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- 6. Desktop Entry & App Icon ----------------------------------------------
say "Installing desktop menu integration and application icon…"
PATH="$BIN_DIR:$PATH" "$APP_DIR/venv/bin/openswarm" desktop-install >/dev/null || warn "Could not create desktop entry (non-fatal)."

if [[ -z "${OPENSWARM_SKIP_SKILLS:-}" ]]; then
  say "Installing bundled opencode skills…"
  PATH="$BIN_DIR:$PATH" "$APP_DIR/venv/bin/openswarm" skills install || warn "Skill install skipped/failed (non-fatal)."
fi

if [[ -n "${OPENSWARM_SELFTEST:-}" ]]; then
  say "Running self-test suite…"
  PATH="$BIN_DIR:$PATH" "$APP_DIR/venv/bin/openswarm" selftest || warn "Selftest had warnings."
fi

say "Verifying installation with openswarm doctor…"
(cd "$SRC" && PATH="$BIN_DIR:$PATH" "$APP_DIR/venv/bin/openswarm" --doctor --config /tmp/openswarm-doctor.yaml) || \
  warn "Doctor note: project-specific checks skipped outside active git repo."
rm -f /tmp/openswarm-doctor.yaml

cat <<MSG

========================================================================
✔ OpenSwarm installed successfully on $DISTRO_NAME!
========================================================================
Quick start:
  1. openswarm gui       # Native Linux desktop window (GTK4 / WebKitGTK)
  2. openswarm web       # Web UI in your default browser or LAN
  3. openswarm           # Terminal TUI app
  4. openswarm --help    # Command-line options & swarm workflows
========================================================================
MSG
