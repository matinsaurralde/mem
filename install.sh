#!/usr/bin/env bash
# mem installer — curl -fsSL https://raw.githubusercontent.com/matinsaurralde/mem/main/install.sh | bash
#
# Installs mem via pipx (preferred) or pip as fallback.
# Requires: Python 3.10+, macOS 26.0+

set -euo pipefail

BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

info()  { printf "${BOLD}${GREEN}==>${RESET} ${BOLD}%s${RESET}\n" "$1"; }
warn()  { printf "${BOLD}${YELLOW}warning:${RESET} %s\n" "$1"; }
error() { printf "${BOLD}${RED}error:${RESET} %s\n" "$1" >&2; exit 1; }

# --- Pre-flight checks ---

info "Checking requirements..."

# macOS check
if [[ "$(uname -s)" != "Darwin" ]]; then
  error "mem requires macOS. Linux and Windows are not supported yet."
fi

# Python version check
if ! command -v python3 &>/dev/null; then
  error "Python 3.10+ is required. Install it from https://www.python.org or via brew install python@3.12"
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || [[ "$PYTHON_MAJOR" -eq 3 && "$PYTHON_MINOR" -lt 10 ]]; then
  error "Python 3.10+ is required (found $PYTHON_VERSION)"
fi

printf "  Python %s ${GREEN}OK${RESET}\n" "$PYTHON_VERSION"

# --- Install ---

if command -v pipx &>/dev/null; then
  info "Installing mem via pipx..."
  pipx install mem-cli
else
  warn "pipx not found — falling back to pip install"
  info "Installing mem via pip..."
  python3 -m pip install --user mem-cli
fi

# --- Verify ---

if ! command -v mem &>/dev/null; then
  warn "mem was installed but is not on your PATH"
  echo ""
  echo "  Add this to your shell config:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
  echo "  Then restart your shell and run:"
  echo "    mem --version"
  exit 0
fi

info "Installed $(mem --version)"

# --- Shell hook setup ---

echo ""
info "Setting up shell hook..."
echo ""
printf "  Add this line to your ${BOLD}~/.zshrc${RESET}:\n"
echo ""
printf "    ${DIM}eval \"\$(mem init zsh)\"${RESET}\n"
echo ""
printf "  Then reload your shell:\n"
echo ""
printf "    ${DIM}source ~/.zshrc${RESET}\n"
echo ""
info "Done. Start using your shell — mem will remember everything."
