#!/usr/bin/env bash
set -euo pipefail

REPO="yukai08008/td-agent"
TOOL_NAME="toe-dac"
BIN_NAME="toe-dac"
INSTALL_DIR="${HOME}/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/td-agent"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { printf "${GREEN}[td-agent]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[td-agent]${NC} %s\n" "$*"; }
error() { printf "${RED}[td-agent]${NC} %s\n" "$*" >&2; exit 1; }

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  info "uv not found; installing it from astral.sh..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${INSTALL_DIR}:${PATH}"
  command -v uv >/dev/null 2>&1 || error "uv installation failed; see https://docs.astral.sh/uv/"
}

ensure_path() {
  mkdir -p "$INSTALL_DIR"
  case ":${PATH}:" in
    *:"${INSTALL_DIR}":*) return ;;
  esac

  local rc_file="${HOME}/.profile"
  if [ -n "${ZSH_VERSION:-}" ]; then
    rc_file="${HOME}/.zshrc"
  elif [ -n "${BASH_VERSION:-}" ]; then
    rc_file="${HOME}/.bashrc"
  fi
  warn "Adding ${INSTALL_DIR} to PATH in ${rc_file}"
  printf '\nexport PATH="%s:$PATH"\n' "$INSTALL_DIR" >> "$rc_file"
  export PATH="${INSTALL_DIR}:${PATH}"
}

ensure_config() {
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"

  if [ ! -f "${CONFIG_DIR}/models.json" ]; then
    curl -fsSL "${RAW_BASE}/config/models.json" -o "${CONFIG_DIR}/models.json"
    info "Created ${CONFIG_DIR}/models.json"
  fi
  if [ ! -f "${CONFIG_DIR}/.env.example" ]; then
    curl -fsSL "${RAW_BASE}/.env.example" -o "${CONFIG_DIR}/.env.example"
  fi
  if [ ! -f "${CONFIG_DIR}/.env" ]; then
    curl -fsSL "${RAW_BASE}/.env" -o "${CONFIG_DIR}/.env"
  fi
  if [ ! -f "${CONFIG_DIR}/.env.local" ]; then
    cp "${CONFIG_DIR}/.env.example" "${CONFIG_DIR}/.env.local"
    chmod 600 "${CONFIG_DIR}/.env.local"
    warn "Add your API keys to ${CONFIG_DIR}/.env.local"
  fi
}

install_or_update() {
  local action="$1"
  info "${action} TD Agent from github.com/${REPO}..."
  uv tool install --force "git+https://github.com/${REPO}.git"
  ensure_config
  if command -v "$BIN_NAME" >/dev/null 2>&1; then
    info "TD Agent is ready: $($BIN_NAME --version | sed -n '1p')"
    info "Run '${BIN_NAME} doctor', then '${BIN_NAME} new'."
  elif [ -x "${INSTALL_DIR}/${BIN_NAME}" ]; then
    warn "Installed successfully. Restart the terminal so ${INSTALL_DIR} is in PATH."
  else
    error "Installation failed."
  fi
}

uninstall() {
  if command -v uv >/dev/null 2>&1; then
    uv tool uninstall "$TOOL_NAME" 2>/dev/null || true
  fi
  rm -f "${INSTALL_DIR}/${BIN_NAME}"
  info "TD Agent uninstalled. Configuration was preserved at ${CONFIG_DIR}."
}

main() {
  case "${1:-install}" in
    install)
      ensure_uv
      ensure_path
      install_or_update "Installing"
      ;;
    update|upgrade)
      ensure_uv
      ensure_path
      install_or_update "Updating"
      ;;
    uninstall)
      uninstall
      ;;
    *)
      error "Usage: install.sh [install|update|uninstall]"
      ;;
  esac
}

main "$@"
