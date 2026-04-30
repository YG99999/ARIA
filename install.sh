#!/usr/bin/env bash
# ARIA — One-Line Installer (idempotent — safe to re-run at any time)
#
# Usage (paste this into your terminal):
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/YG99999/ARIA/main/install.sh)
#
# Works on: Raspberry Pi OS, Debian, Ubuntu, and most apt-based Linux distros.
# Run as a normal user with sudo access — do NOT run as root.
#
# Re-run any time: already-completed steps are detected and skipped.
# If a previous run failed mid-way, it resumes from that point.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/YG99999/ARIA.git"
INSTALL_DIR="$HOME/aria"
VENV_DIR="$INSTALL_DIR/.venv"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10
LOG_FILE="/tmp/aria_install.log"

# Progress state — persists across runs so we never redo completed work.
# Stored in the home dir so it survives even if INSTALL_DIR gets wiped.
STATE_FILE="$HOME/.aria_install_state"

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
DIM="\033[2m"
NC="\033[0m"

log()     { echo -e "${GREEN}✔${NC}  $*"; }
skip()    { echo -e "${DIM}✔  $* (already done)${NC}"; }
info()    { echo -e "${CYAN}→${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠${NC}  $*"; }
die()     { echo -e "${RED}✘${NC}  $*" >&2; echo "    See $LOG_FILE for details." >&2; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }
run()     { "$@" >> "$LOG_FILE" 2>&1 || die "Command failed: $*"; }

# ── Progress tracking ─────────────────────────────────────────────────────────
# step_done STEP   — true if step was previously completed
# mark_done STEP   — record step as completed
# clear_step STEP  — remove a step (force redo on next run)
step_done() { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }
mark_done() { echo "$1" >> "$STATE_FILE"; }
clear_step() { sed -i "/^${1}$/d" "$STATE_FILE" 2>/dev/null || true; }

# ── Stdin guard ───────────────────────────────────────────────────────────────
# If stdin is not a terminal the script was piped in (curl ... | bash).
# We can't run the interactive wizard that way. Download and re-exec correctly.
if [ ! -t 0 ]; then
    echo ""
    echo "  ARIA installer — re-launching with interactive stdin..."
    echo ""
    TMP=$(mktemp /tmp/aria_install_XXXXXX.sh)
    cat > "$TMP"
    chmod +x "$TMP"
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
echo "  Progress file     : $STATE_FILE"
echo "  Log file          : $LOG_FILE"
echo ""

# Append to log across runs — don't wipe it, so failures are traceable.
echo "" >> "$LOG_FILE"
echo "=== ARIA install run: $(date) ===" >> "$LOG_FILE"

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

# ── System package helpers ────────────────────────────────────────────────────
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

# Check if a single apt package is already installed (apt only; no-op on others)
apt_installed() {
    [ "$PKG_MGR" != "apt" ] && return 1
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

# ── Step 1: Update package index ─────────────────────────────────────────────
section "System dependencies"

if step_done "pkg_updated"; then
    skip "Package index already updated"
else
    info "Updating package index…"
    pkg_update
    mark_done "pkg_updated"
    log "Package index updated"
fi

# ── Step 2: Core tools (curl, git) ───────────────────────────────────────────
NEED_CORE=()
command -v curl &>/dev/null || NEED_CORE+=(curl)
command -v git  &>/dev/null || NEED_CORE+=(git)

if [ ${#NEED_CORE[@]} -eq 0 ]; then
    skip "Core tools (curl, git) already installed"
else
    info "Installing: ${NEED_CORE[*]}…"
    pkg_install "${NEED_CORE[@]}"
    log "Core tools installed"
fi

# ── Step 3: Python 3.10+ ──────────────────────────────────────────────────────
section "Python"

find_python() {
    for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
            maj=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
            if [ "$maj" -ge "$PYTHON_MIN_MAJOR" ] && [ "$ver" -ge "$PYTHON_MIN_MINOR" ]; then
                echo "$cmd"; return
            fi
        fi
    done
}

PYTHON=$(find_python || true)

if [ -n "$PYTHON" ]; then
    PYVER=$("$PYTHON" --version 2>&1)
    log "Python: $PYVER ($PYTHON)"
else
    warn "Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ not found — installing…"
    case "$PKG_MGR" in
        apt)
            if command -v add-apt-repository &>/dev/null; then
                sudo add-apt-repository -y ppa:deadsnakes/ppa >> "$LOG_FILE" 2>&1 || true
                pkg_update
            fi
            pkg_install python3.11 python3.11-venv python3.11-dev python3-pip || \
            pkg_install python3 python3-venv python3-dev python3-pip
            ;;
        dnf)  pkg_install python3.11 python3-pip python3-venv ;;
        pacman) pkg_install python python-pip ;;
    esac
    PYTHON=$(find_python || true)
    [ -n "$PYTHON" ] || die "Could not install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+. Install it manually and re-run."
    PYVER=$("$PYTHON" --version 2>&1)
    log "Python installed: $PYVER"
fi

PYMINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

# ── Step 4: python3.X-venv ───────────────────────────────────────────────────
# On Debian/Ubuntu, python3.X-venv is a separate package. `python3.X -m venv --help`
# returns 0 even when it's missing, so we check by attempting to create a test venv.
if step_done "venv_pkg_installed"; then
    skip "python3.${PYMINOR}-venv already confirmed"
else
    if "$PYTHON" -m venv --without-pip /tmp/aria_venv_test >> "$LOG_FILE" 2>&1; then
        rm -rf /tmp/aria_venv_test
        mark_done "venv_pkg_installed"
        log "python3.${PYMINOR}-venv confirmed present"
    else
        rm -rf /tmp/aria_venv_test
        info "Installing python3.${PYMINOR}-venv…"
        case "$PKG_MGR" in
            apt)
                pkg_install "python3.${PYMINOR}-venv" "python3.${PYMINOR}-full" 2>/dev/null || \
                pkg_install python3-venv python3-full 2>/dev/null || true
                ;;
            dnf|pacman) pkg_install python3-venv 2>/dev/null || true ;;
        esac
        mark_done "venv_pkg_installed"
        log "python3.${PYMINOR}-venv installed"
    fi
fi

# ── Step 5: Chromium system libraries ────────────────────────────────────────
section "Chromium system libraries"

if step_done "chromium_libs_installed"; then
    skip "Chromium system libraries already installed"
else
    case "$PKG_MGR" in
        apt)
            # Base deps — no libasound variant here, handled separately below
            CHROMIUM_DEPS=(
                libnspr4 libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0
                libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1
                libxfixes3 libxrandr2 libgbm1 libxshmfence1
                libpango-1.0-0 libpangocairo-1.0-0
                fonts-liberation xdg-utils
            )
            pkg_install "${CHROMIUM_DEPS[@]}" 2>/dev/null || true
            # libasound2 was renamed to libasound2t64 on Ubuntu 24.04 / Debian 12+
            # Try the new name first, fall back to the old name
            pkg_install libasound2t64 2>/dev/null || \
            pkg_install libasound2    2>/dev/null || true
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
    mark_done "chromium_libs_installed"
    log "Chromium system libraries installed"
fi

# ── Step 6: Xvfb (Raspberry Pi only) ─────────────────────────────────────────
if [ "$IS_PI" = true ]; then
    if command -v Xvfb &>/dev/null; then
        skip "Xvfb already installed"
    else
        info "Raspberry Pi: installing Xvfb for headless display…"
        pkg_install xvfb x11vnc || true
        log "Xvfb installed"
    fi
fi

# ── Step 7: Clone or update repo ─────────────────────────────────────────────
section "Getting ARIA"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repository exists — pulling latest changes…"
    run git -C "$INSTALL_DIR" pull --ff-only
    log "Repository up to date"
    # Invalidate pip/playwright steps if requirements.txt changed since last install
    REQS="$INSTALL_DIR/aria/requirements.txt"
    if [ -f "$REQS" ]; then
        REQS_HASH=$(md5sum "$REQS" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
        STORED_HASH=$(grep "^pip_deps:" "$STATE_FILE" 2>/dev/null | cut -d: -f2 || echo "")
        if [ "$REQS_HASH" != "$STORED_HASH" ]; then
            info "requirements.txt changed — will reinstall Python deps"
            clear_step "pip_deps_installed"
            sed -i "/^pip_deps:/d" "$STATE_FILE" 2>/dev/null || true
        fi
    fi
elif [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/aria/main.py" ]; then
    info "ARIA files found (no git) — initialising git…"
    run git -C "$INSTALL_DIR" init
    log "Git initialised in existing directory"
else
    info "Cloning ARIA into $INSTALL_DIR…"
    run git clone "$REPO_URL" "$INSTALL_DIR"
    log "Repository cloned"
fi

# ── Step 8: Python virtual environment ───────────────────────────────────────
section "Python environment"

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
    skip "Virtual environment already exists at $VENV_DIR"
else
    info "Creating virtual environment…"
    if ! "$PYTHON" -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1; then
        warn "venv creation failed — retrying after installing dependencies…"
        case "$PKG_MGR" in
            apt)
                pkg_install "python3.${PYMINOR}-venv" "python3.${PYMINOR}-full" 2>/dev/null || \
                pkg_install python3-venv python3-full 2>/dev/null || true
                ;;
            dnf|pacman) pkg_install python3-venv 2>/dev/null || true ;;
        esac
        run "$PYTHON" -m venv "$VENV_DIR"
    fi
    log "Virtual environment created"
    # Force pip/playwright reinstall since venv is new
    clear_step "pip_deps_installed"
    clear_step "playwright_installed"
    sed -i "/^pip_deps:/d" "$STATE_FILE" 2>/dev/null || true
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# Always ensure pip itself is up to date (fast, idempotent)
info "Ensuring pip is up to date…"
run "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel

# ── Step 9: Python dependencies ───────────────────────────────────────────────
section "Python dependencies"

REQS="$INSTALL_DIR/aria/requirements.txt"
REQS_HASH=$(md5sum "$REQS" 2>/dev/null | cut -d' ' -f1 || echo "unknown")
STORED_HASH=$(grep "^pip_deps:" "$STATE_FILE" 2>/dev/null | cut -d: -f2 || echo "")

if step_done "pip_deps_installed" && [ "$REQS_HASH" = "$STORED_HASH" ]; then
    skip "Python dependencies already installed (requirements.txt unchanged)"
else
    info "Installing Python dependencies (this may take a few minutes on a Pi)…"
    run "$VENV_PIP" install -r "$REQS"
    clear_step "pip_deps_installed"
    sed -i "/^pip_deps:/d" "$STATE_FILE" 2>/dev/null || true
    mark_done "pip_deps_installed"
    echo "pip_deps:${REQS_HASH}" >> "$STATE_FILE"
    log "Python dependencies installed"
fi

# ── Step 10: Playwright + Chromium ───────────────────────────────────────────
section "Playwright + Chromium"

# Detect if playwright's Chromium is already downloaded.
# The binary lives under playwright's driver directory.
PW_CHROMIUM_FOUND=false
PW_DRIVER=$("$VENV_PYTHON" -c \
    "import playwright; import os; print(os.path.dirname(playwright.__file__))" \
    2>/dev/null || echo "")
if [ -n "$PW_DRIVER" ]; then
    if find "$PW_DRIVER" -name "chrome" -o -name "chromium" 2>/dev/null | grep -q .; then
        PW_CHROMIUM_FOUND=true
    fi
fi

if step_done "playwright_installed" && [ "$PW_CHROMIUM_FOUND" = true ]; then
    skip "Playwright + Chromium already downloaded"
else
    info "Downloading Playwright Chromium (one-time, ~150 MB)…"
    run "$VENV_PYTHON" -m playwright install chromium
    "$VENV_PYTHON" -m playwright install-deps chromium >> "$LOG_FILE" 2>&1 || true
    mark_done "playwright_installed"
    log "Playwright + Chromium ready"
fi

# ── Step 11: Launcher script ──────────────────────────────────────────────────
section "Launcher"

LAUNCHER="$INSTALL_DIR/aria-start"
# Always (re)write the launcher — it's tiny and must stay in sync with paths
cat > "$LAUNCHER" << LAUNCHEREOF
#!/usr/bin/env bash
# ARIA launcher — activates venv and starts the agent
set -euo pipefail
cd "$INSTALL_DIR/aria"
source "$VENV_DIR/bin/activate"
exec python main.py "\$@"
LAUNCHEREOF
chmod +x "$LAUNCHER"

# Symlink into PATH (idempotent — ln -sf overwrites safely)
if [ -w /usr/local/bin ]; then
    sudo ln -sf "$LAUNCHER" /usr/local/bin/aria 2>/dev/null || true
    log "'aria' command available at /usr/local/bin/aria"
else
    mkdir -p "$HOME/.local/bin"
    ln -sf "$LAUNCHER" "$HOME/.local/bin/aria" 2>/dev/null || true
    log "'aria' command available at ~/.local/bin/aria"
    if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
        warn "Add ~/.local/bin to your PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
fi

# ── All done ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║         Installation complete!              ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${DIM}Progress saved to: $STATE_FILE${NC}"
echo -e "  ${DIM}Full log at:       $LOG_FILE${NC}"
echo ""

# Check if ARIA is already configured (has a .env with real tokens)
ENV_FILE="$INSTALL_DIR/aria/.env"
ALREADY_CONFIGURED=false
if [ -f "$ENV_FILE" ] && grep -q "^TELEGRAM_TOKEN=" "$ENV_FILE" && \
   ! grep -q "your_telegram_bot_token_here" "$ENV_FILE"; then
    ALREADY_CONFIGURED=true
fi

if [ "$ALREADY_CONFIGURED" = true ]; then
    echo "  ARIA is already configured. Start it with:"
    echo ""
    echo "    aria"
    echo ""
    echo -e "  ${DIM}Re-run setup wizard: aria --setup${NC}"
else
    echo "  Starting the ARIA setup wizard now…"
    echo "  (You will need your Telegram bot token and LLM API key)"
    echo ""
    echo -e "${DIM}  Press Enter to begin, or Ctrl+C to skip and run: aria --setup${NC}"
    read -r
    cd "$INSTALL_DIR/aria"
    source "$VENV_DIR/bin/activate"
    exec python main.py --setup
fi
