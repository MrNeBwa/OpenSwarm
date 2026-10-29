#!/usr/bin/env bash
# ============================================================================
# OpenSwarm v1.0 — Self-Contained Portable Launcher
# Runs directly from this folder (or USB drive) without global installation.
# ============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Portable state & config directory right inside the project folder
if [[ "${OPENSWARM_SYSTEM_STATE:-0}" != "1" ]]; then
  export XDG_STATE_HOME="${ROOT_DIR}/.openswarm-portable/state"
  export XDG_CONFIG_HOME="${ROOT_DIR}/.openswarm-portable/config"
  mkdir -p "${XDG_STATE_HOME}" "${XDG_CONFIG_HOME}"
fi

# Ensure Android APK is built
if [[ ! -f "${ROOT_DIR}/dist/OpenSwarm-Mobile.apk" ]] && [[ -f "${ROOT_DIR}/scripts/build_apk.py" ]]; then
  python3 "${ROOT_DIR}/scripts/build_apk.py" >/dev/null 2>&1 || true
fi

case "${1:-}" in
  build-apk)
    shift
    exec python3 "${ROOT_DIR}/scripts/build_apk.py" "$@"
    ;;
  test)
    shift
    exec python3 -m unittest discover -t "${ROOT_DIR}/src" -s "${ROOT_DIR}/src/openswarm/tests" "$@"
    ;;
  "")
    exec python3 -m openswarm web
    ;;
  *)
    exec python3 -m openswarm "$@"
    ;;
esac
