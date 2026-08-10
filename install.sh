#!/usr/bin/env bash
set -euo pipefail

REPO="yukai08008/td-agent"
LATEST_VERSION="0.8.0"
TOOL_NAME="toe-dac"
BIN_NAME="toe-dac"
INSTALL_DIR="${HOME}/.local/bin"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/td-agent"
APP_HOME="${TD_AGENT_HOME:-${HOME}/.td-agent}"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"
PACKAGE_SPEC="git+https://github.com/${REPO}.git"
PACKAGE_FALLBACK="git+https://github.com/${REPO}.git"
RELEASE_LABEL="latest"
CONFIG_BUNDLED="false"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { printf "${GREEN}[td-agent]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[td-agent]${NC} %s\n" "$*"; }
error() { printf "${RED}[td-agent]${NC} %s\n" "$*" >&2; exit 1; }

download() {
  local label="$1"
  local url="$2"
  local destination="$3"
  info "Downloading ${label}..."
  curl -fL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 1 \
    --progress-bar "$url" -o "$destination"
}

select_package() {
  local version="$1"
  local major minor patch
  IFS='.' read -r major minor patch <<< "$version"
  patch="${patch%%-*}"
  PACKAGE_FALLBACK="git+https://github.com/${REPO}.git@v${version}"
  if [[ "$version" != *-* ]] && { [ "$major" -gt 0 ] || [ "$minor" -ge 4 ]; }; then
    PACKAGE_SPEC="https://github.com/${REPO}/releases/download/v${version}/toe_dac-${version}-py3-none-any.whl"
  else
    PACKAGE_SPEC="$PACKAGE_FALLBACK"
  fi
  if [[ "$version" != *-* ]] && { [ "$major" -gt 0 ] || [ "$minor" -gt 4 ] || { [ "$minor" -eq 4 ] && [ "$patch" -ge 2 ]; }; }; then
    CONFIG_BUNDLED="true"
  else
    CONFIG_BUNDLED="false"
  fi
}

select_release() {
  local requested="${1:-latest}"
  if [ "$requested" = "latest" ]; then
    RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"
    select_package "$LATEST_VERSION"
    PACKAGE_FALLBACK="git+https://github.com/${REPO}.git"
    RELEASE_LABEL="latest (v${LATEST_VERSION})"
    return
  fi

  requested="${requested#v}"
  [[ "$requested" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || \
    error "Invalid version '${requested}'; expected X.Y.Z or latest."
  if [ "$requested" = "0.1.0" ]; then
    error "v0.1.0 used a private local dependency; public standalone versions start at v0.2.0."
  fi
  RAW_BASE="https://raw.githubusercontent.com/${REPO}/v${requested}"
  select_package "$requested"
  RELEASE_LABEL="v${requested}"
}

run_uv_install() {
  local package="$1"
  local process_id heartbeat_id started_at current_time elapsed status
  local -a command=(uv tool install --force "$package")
  case "${TD_AGENT_INSTALL_VERBOSE:-false}" in
    1|true|TRUE|yes|YES) command=(uv --verbose tool install --force "$package") ;;
  esac

  info "Package source: ${package}"
  info "Runtime dependencies: rich, questionary (8 packages on an empty cache)."
  info "uv cache: $(uv cache dir)"
  info "uv tools: $(uv tool dir)"
  "${command[@]}" &
  process_id=$!
  started_at=$(date +%s)
  (
    while sleep 5; do
      if ! kill -0 "$process_id" 2>/dev/null; then
        exit
      fi
      current_time=$(date +%s)
      elapsed=$((current_time - started_at))
      info "uv is still working (${elapsed}s): resolving metadata or downloading packages..."
    done
  ) &
  heartbeat_id=$!
  status=0
  wait "$process_id" || status=$?
  kill "$heartbeat_id" 2>/dev/null || true
  wait "$heartbeat_id" 2>/dev/null || true
  return "$status"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  info "uv not found; installing it from astral.sh..."
  curl -fL --connect-timeout 10 --max-time 120 --retry 3 --progress-bar \
    https://astral.sh/uv/install.sh | sh
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
  if [ "$CONFIG_BUNDLED" = "true" ]; then
    info "Initializing bundled machine-local configuration..."
    "$BIN_NAME" config --init
    return
  fi
  mkdir -p "$CONFIG_DIR"
  chmod 700 "$CONFIG_DIR"

  if [ ! -f "${CONFIG_DIR}/models.json" ]; then
    download "model registry" "${RAW_BASE}/config/models.json" "${CONFIG_DIR}/models.json"
    info "Created ${CONFIG_DIR}/models.json"
  fi
  if [ ! -f "${CONFIG_DIR}/.env.example" ]; then
    download "environment template" "${RAW_BASE}/.env.example" "${CONFIG_DIR}/.env.example"
  fi
  if [ ! -f "${CONFIG_DIR}/.env" ]; then
    download "environment defaults" "${RAW_BASE}/.env" "${CONFIG_DIR}/.env"
  fi
  if [ ! -f "${CONFIG_DIR}/.env.local" ]; then
    cp "${CONFIG_DIR}/.env.example" "${CONFIG_DIR}/.env.local"
    chmod 600 "${CONFIG_DIR}/.env.local"
    warn "Add your API keys to ${CONFIG_DIR}/.env.local"
  fi
}

ensure_runtime_dirs() {
  mkdir -p "${APP_HOME}/data" "${APP_HOME}/logs" "${APP_HOME}/credentials"
  chmod 700 "${APP_HOME}" "${APP_HOME}/credentials"
  info "Runtime data: ${APP_HOME}/data"
  info "Access logs: ${APP_HOME}/logs"
  info "Credentials: ${APP_HOME}/credentials"
}

install_or_update() {
  local action="$1"
  info "${action} TD Agent ${RELEASE_LABEL} from github.com/${REPO}..."
  info "Installing the isolated CLI package. Dependency progress will appear below."
  if ! UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-30}" run_uv_install "$PACKAGE_SPEC"; then
    if [ "$PACKAGE_SPEC" = "$PACKAGE_FALLBACK" ]; then
      error "Package installation failed. Check the network output above."
    fi
    warn "Prebuilt wheel unavailable; falling back to the Git repository."
    UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-30}" run_uv_install "$PACKAGE_FALLBACK"
  fi
  ensure_config
  ensure_runtime_dirs
  if command -v "$BIN_NAME" >/dev/null 2>&1; then
    info "TD Agent is ready: $($BIN_NAME --version | sed -n '1p')"
    info "Run '${BIN_NAME}'. It will guide model configuration when required."
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
  info "TD Agent uninstalled. Configuration and runtime data were preserved."
  info "Configuration: ${CONFIG_DIR}"
  info "Runtime data: ${APP_HOME}"
}

main() {
  case "${1:-install}" in
    install)
      select_release "${2:-${TD_AGENT_VERSION:-latest}}"
      ensure_uv
      ensure_path
      install_or_update "Installing"
      ;;
    update|upgrade)
      select_release "${2:-${TD_AGENT_VERSION:-latest}}"
      ensure_uv
      ensure_path
      install_or_update "Updating"
      ;;
    uninstall)
      uninstall
      ;;
    *)
      error "Usage: install.sh [install|update] [latest|X.Y.Z] | uninstall"
      ;;
  esac
}

main "$@"
