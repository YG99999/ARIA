#!/usr/bin/env bash
# ARIA — One-Line Installer
#
# Usage (paste this into your terminal):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/YOUR_USER/ARIA/main/install.sh)
#
# Works on: Raspberry Pi OS, Debian, Ubuntu, and most apt-based Linux distros.
# Run as a normal user with sudo access — do NOT run as root.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/YG99999/ARIA.git"
INSTALL_DIR="$HOME/aria"
VENV_DIR="$INSTALL_DIR/.venv"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10
LOG_FILE="/tmp/aria_install.log"

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
DIM="\033[2m"
NC="\033[0m"

log()     { echo -e "${GREEN}✔${NC}  $*"; }
info()    { echo -e "${CYAN}→${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
die()     { echo -e "${RED}✘${NC}  $*" >&2; echo "    See $LOG_FILE for details." >&2; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }
run()     { "$@" >> "$LOG_FILE" 2>&1 || die "Command failed: $*"; }

# ── Stdin guard ───────────────────────────────────────────────────────────────
# If stdin is not a terminal the script was piped in (curl ... | bash).
# We can't run the interactive wizard that way. Download and re-exec correctly.
if [ ! -t 0 ]; then
    echo ""
    echo "  ARIA installer — re-launching with interactive stdin..."
    echo ""
    TMP=$(mktemp /tmp/aria_install_XXXXXX.sh)
    # Read ourselves from the pipe into a temp file
    cat > "$TMP"
    chmod +x "$TMP"
    # Re-exec with stdin from the terminal
    exec bash "$TMP" < /dev/tty
fi

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   ARIA — Autonomous Resident Intelligence    ║"
echo "  ║              One-Line Installer              ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Install directory : $INSTALL_DIR"
echo "  Log file          : $LOG_FILE"
echo ""

# Wipe old log
> "$LOG_FILE"

# ── Root check ────────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    die "Do not run this installer as root. Run as a normal user with sudo access."
fi

# ── OS / package manager detection ───────────────────────────────────────────
section "Detecting system"

PKG_MGR=""
if command -v apt-get &>/dev/null; then
    PKG_MGR="apt"
    log "Package manager: apt"
elif command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
    log "Package manager: dnf"
elif command -v pacman &>/dev/null; then
    PKG_MGR="pacman"
    log "Package manager: pacman"
else
    die "Unsupported Linux distribution (no apt/dnf/pacman found)."
fi

# Detect Pi
IS_PI=false
if grep -qi "raspberry" /proc/cpuinfo 2>/dev/null || \
   grep -qi "raspberry" /proc/device-tree/model 2>/dev/null; then
    IS_PI=true
    log "Raspberry Pi detected"
fi

# ── System package helper ─────────────────────────────────────────────────────
pkg_install() {
    case "$PKG_MGR" in
        apt)    sudo apt-get install -y "$@" ;;
        dnf)    sudo dnf install -y "$@" ;;
        pacman) sudo pacman -S --noconfirm "$@" ;;
    esac >> "$LOG_FILE" 2>&1
}

pkg_update() {
    case "$PKG_MGR" in
        apt)    sudo apt-get update -qq ;;
        dnf)    sudo dnf check-update -q || true ;;
        pacman) sudo pacman -Sy --noconfirm ;;
    esac >> "$LOG_FILE" 2>&1
}

# ── Install system dependencies ───────────────────────────────────────────────
section "Installing system dependencies"

info "Updating package index…"
pkg_update

# Core tools
CORE_PKGS=(curl git)
info "Ensuring core tools (curl, git)…"
pkg_install "${CORE_PKGS[@]}"
log "Core tools ready"

# Python 3.10+
section "Checking Python version"

find_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
            maj=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
            if [ "$maj" -ge "$PYTHON_MIN_MAJOR" ] && [ "$ver" -ge "$PYTHON_MIN_MINOR" ]; then
                echo "$cmd"
                return
            fi
        fi
    done
}

PYTHON=$(find_python || true)

if [ -z "$PYTHON" ]; then
    warn "Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ not found — installing…"
    case "$PKG_MGR" in
        apt)
            # Try deadsnakes PPA on Ubuntu; plain apt on Debian/Pi
            if command -v add-apt-repository &>/dev/null; then
                sudo add-apt-repository -y ppa:deadsnakes/ppa >> "$LOG_FILE" 2>&1 || true
                pkg_update
            fi
            pkg_install python3.11 python3.11-venv python3.11-dev python3-pip || \
            pkg_install python3 python3-venv python3-dev python3-pip
            ;;
        dnf)
            pkg_install python3.11 python3-pip python3-venv
            ;;
        pacman)
            pkg_install python python-pip
            ;;
    esac
    PYTHON=$(find_python || true)
    [ -n "$PYTHON" ] || die "Could not install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+. Install it manually and re-run."
fi

PYVER=$("$PYTHON" --version 2>&1)
log "Python: $PYVER ($PYTHON)"

# venv support — on Debian/Ubuntu, python3.X-venv must be installed separately.
# `python3.X -m venv --help` returns 0 even when the package is missing, so we
# always install the venv package proactively rather than relying on the help check.
PYMINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
case "$PKG_MGR" in
    apt)
        pkg_install "python3.${PYMINOR}-venv" 2>/dev/null || \
        pkg_install "python3-venv" 2>/dev/null || true
        ;;
    dnf|pacman)
        pkg_install python3-venv 2>/dev/null || true
        ;;
esac

# Playwright OS-level deps (Chromium headless)
section "Installing Chromium system libraries"
case "$PKG_MGR" in
    apt)
        CHROMIUM_DEPS=(
            libnspr4 libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0
            libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1
            libxfixes3 libxrandr2 libgbm1 libxshmfence1
            libasound2 libpango-1.0-0 libpangocairo-1.0-0
            fonts-liberation xdg-utils
        )
        # libasound2 was renamed on Debian 12+
        pkg_install "${CHROMIUM_DEPS[@]}" 2>/dev/null || \
        pkg_install libasound2t64 "${CHROMIUM_DEPS[@]:1}" 2>/dev/null || true
        ;;
    dnf)
        pkg_install nss atk at-spi2-atk cups-libs libdrm libxkbcommon \
                    libXcomposite libXdamage libXrandr mesa-libgbm alsa-lib pango || true
        ;;
    pacman)
        pkg_install nss atk at-spi2-atk libcups libdrm libxkbcommon \
                    libxcomposite libxdamage libxrandr mesa alsa-lib pango || true
        ;;
esac
log "Chromium system libraries installed"

# Raspberry Pi: virtual display for headless browser
if [ "$IS_PI" = true ]; then
    info "Raspberry Pi: installing Xvfb for headless display…"
    pkg_install xvfb x11vnc || true
    log "Xvfb installed (virtual display for headless Chromium)"
fi

# ── Clone or update the repo ──────────────────────────────────────────────────
section "Getting ARIA"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "ARIA directory exists — pulling latest…"
    run git -C "$INSTALL_DIR" pull --ff-only
    log "Repository updated"
elif [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/aria/main.py" ]; then
    info "ARIA directory found (no git) — skipping clone"
    run git -C "$INSTALL_DIR" init
    log "Initialised git in existing directory"
else
    info "Cloning ARIA into $INSTALL_DIR…"
    run git clone "$REPO_URL" "$INSTALL_DIR"
    log "Repository cloned"
fi

# ── Python virtual environment ────────────────────────────────────────────────
section "Setting up Python environment"

if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment…"
    if ! "$PYTHON" -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1; then
        warn "venv creation failed — installing python3-venv / python3-full and retrying…"
        PYMINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
        case "$PKG_MGR" in
            apt)
                pkg_install "python3.${PYMINOR}-venv" "python3.${PYMINOR}-full" 2>/dev/null || \
                pkg_install python3-venv python3-full 2>/dev/null || true
                ;;
            dnf|pacman)
                pkg_install python3-venv 2>/dev/null || true
                ;;
        esac
        run "$PYTHON" -m venv "$VENV_DIR"
    fi
    log "Virtual environment created at $VENV_DIR"
else
    log "Virtual environment already exists"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

info "Upgrading pip…"
run "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel

# ── Python dependencies ───────────────────────────────────────────────────────
section "Installing Python dependencies"
info "This may take a few minutes on a Raspberry Pi…"
run "$VENV_PIP" install -r "$INSTALL_DIR/aria/requirements.txt"
log "Python dependencies installed"

# ── Playwright + Chromium ─────────────────────────────────────────────────────
section "Installing Playwright + Chromium"
info "Downloading Chromium (one-time, ~150 MB)…"
run "$VENV_PYTHON" -m playwright install chromium
# Install any remaining OS deps playwright needs
run "$VENV_PYTHON" -m playwright install-deps chromium 2>/dev/null || true
log "Playwright + Chromium ready"

# ── Write launcher script ─────────────────────────────────────────────────────
section "Writing launcher"

LAUNCHER="$INSTALL_DIR/aria-start"
cat > "$LAUNCHER" << EOF
#!/usr/bin/env bash
# ARIA launcher — activates venv and starts the agent
set -euo pipefail
cd "$INSTALL_DIR/aria"
source "$VENV_DIR/bin/activate"
exec python main.py "\$@"
EOF
chmod +x "$LAUNCHER"

# Symlink to /usr/local/bin if writable, otherwise ~/bin
if [ -w /usr/local/bin ]; then
    sudo ln -sf "$LAUNCHER" /usr/local/bin/aria 2>/dev/null || true
    log "Added 'aria' command to /usr/local/bin"
else
    mkdir -p "$HOME/.local/bin"
    ln -sf "$LAUNCHER" "$HOME/.local/bin/aria" 2>/dev/null || true
    log "Added 'aria' command to ~/.local/bin"
    warn "Make sure ~/.local/bin is in your PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ── Summary before onboarding ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║         Installation complete!              ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${DIM}Install log: $LOG_FILE${NC}"
echo ""
echo "  Starting the ARIA setup wizard now…"
echo "  (You will need your Telegram bot token and LLM API key)"
echo ""
echo -e "${DIM}  Press Enter to begin, or Ctrl+C to do it later and run: aria --setup${NC}"
read -r

# ── Launch onboarding wizard ──────────────────────────────────────────────────
cd "$INSTALL_DIR/aria"
source "$VENV_DIR/bin/activate"
exec python main.py --setup
