#!/bin/bash
set -Eeuo pipefail

# Derived from this script's own location, not hardcoded, so a clone at any
# path (a second machine, a renamed folder) works without editing this file.
# launchd invokes this script by the absolute ProgramArguments path in the
# plist, so BASH_SOURCE still resolves correctly there.
readonly REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly DEFAULT_ENV_FILE="${HOME}/.jobtrack/jt.env"

load_env_file() {
  local env_file="${JT_ENV_FILE:-$DEFAULT_ENV_FILE}"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

load_env_file

readonly LOG_DIR="${JT_LOG_DIR:-$HOME/.jobtrack/logs}"
readonly LOG_FILE="${LOG_DIR}/jt-automation.log"
readonly DEFAULT_PYTHON="${REPO_DIR}/.venv/bin/python"

DRY_RUN=0
VERIFY_ONLY=0

usage() {
  /usr/bin/printf 'usage: %s [--dry-run] [--verify]\n' "$0"
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --verify)
      VERIFY_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 64
      ;;
  esac
  shift
done

/bin/mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

log() {
  /usr/bin/printf '[%s] %s\n' "$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

on_error() {
  local exit_code=$?
  log "ERROR: command failed at line ${BASH_LINENO[0]} with exit ${exit_code}"
  exit "$exit_code"
}
trap on_error ERR

python_bin() {
  local configured="${JT_PYTHON:-$DEFAULT_PYTHON}"
  case "$configured" in
    /*) ;;
    *) die "JT_PYTHON must be an absolute path, got: ${configured}" ;;
  esac
  [[ -x "$configured" ]] || die "Python is not executable: ${configured}"
  /usr/bin/printf '%s\n' "$configured"
}

verify_setup() {
  local py="$1"
  [[ -d "$REPO_DIR" ]] || die "repo directory missing: ${REPO_DIR}"
  [[ -r "${REPO_DIR}/sync.py" ]] || die "missing sync.py"
  [[ -r "${REPO_DIR}/classify.py" ]] || die "missing classify.py"
  [[ -r "${REPO_DIR}/jt.py" ]] || die "missing jt.py"
  [[ -w "$LOG_DIR" ]] || die "log directory is not writable: ${LOG_DIR}"
  PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" "$py" -m py_compile "${REPO_DIR}/sync.py" "${REPO_DIR}/classify.py" "${REPO_DIR}/jt.py"
  JT_DB="${LOG_DIR}/verify.db" PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" "$py" "${REPO_DIR}/jt.py" where
  log "verified setup with Python: ${py}"
}

run_step() {
  local name="$1"
  shift
  local start_ts end_ts status
  log "START ${name}"
  start_ts="$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')"
  if "$@"; then
    status=0
  else
    status=$?
  fi
  end_ts="$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')"
  /usr/bin/printf '{"ts":"%s","component":"jt-automation","step":"%s","event":"complete","exit_status":%s,"started_at":"%s"}\n' \
    "$end_ts" "$name" "$status" "$start_ts"
  if [[ "$status" -ne 0 ]]; then
    log "ERROR: ${name} failed with exit ${status}"
    exit "$status"
  fi
  log "DONE ${name}"
}

main() {
  local py
  py="$(python_bin)"

  log "----- jt automation start -----"
  verify_setup "$py"

  if ((VERIFY_ONLY)); then
    log "verification complete"
    return 0
  fi

  if ((DRY_RUN)); then
    run_step "sync dry-run" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/sync.py" --dry-run
    run_step "classify dry-run" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/classify.py" --dry-run
    run_step "review health dry-run" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/jt.py" review stale --no-fail
  else
    run_step "sync" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/sync.py"
    run_step "classify" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/classify.py"
    run_step "review health" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/jt.py" review stale --no-fail
  fi

  run_step "stale summary" /usr/bin/env JT_UNATTENDED=1 "$py" "${REPO_DIR}/jt.py" stale
  log "----- jt automation complete -----"
}

main
