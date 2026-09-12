#!/bin/bash
set -Eeuo pipefail

# Derived, not hardcoded -- see jt-automation.sh for why.
readonly REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly LOG_DIR="${JT_LOG_DIR:-$HOME/.jobtrack/logs}"
readonly LOG_FILE="${LOG_DIR}/jt-daily-stale.log"
readonly AUTOMATION_SCRIPT="${REPO_DIR}/scripts/jt-automation.sh"
readonly DEFAULT_PYTHON="${REPO_DIR}/.venv/bin/python"
# Overridable for tests (a fake notifier that records calls) -- same
# pattern as JT_PYTHON. Hardcoded absolute default deliberately, not a
# bare `osascript` PATH lookup, so a hostile PATH can't substitute a
# different binary in production.
readonly OSASCRIPT="${JT_OSASCRIPT:-/usr/bin/osascript}"

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

notify() {
  local body="$1"
  local subtitle="$2"
  if ((DRY_RUN)); then
    log "DRY RUN notification subtitle: ${subtitle}"
    log "DRY RUN notification body: ${body}"
    return 0
  fi
  [[ -x "$OSASCRIPT" ]] || die "osascript is not executable: ${OSASCRIPT}"
  "$OSASCRIPT" - "$body" "$subtitle" <<'APPLESCRIPT'
on run argv
  display notification (item 1 of argv) with title "jt stale" subtitle (item 2 of argv)
end run
APPLESCRIPT
}

# jt.py's review_alerts() (jt review stale) is the source of truth for
# thresholds -- JT_REVIEW_ALERT_THRESHOLD, JT_REVIEW_AGE_ALERT_DAYS,
# JT_CLASSIFY_FAILURE_ALERT_THRESHOLD -- and is the only thing that reads
# jt-classify.jsonl history, so it's the only thing that can see a
# classifier that's been failing across several hourly runs, not just
# tonight's. This function only reformats its "ALERT " lines into wording
# fit for a glanced-at push notification; it never re-derives the
# thresholds itself.
review_alert_phrases() {
  local review_stale_out="$1"
  local alert_lines
  alert_lines="$(grep '^ALERT ' <<<"$review_stale_out" || true)"
  [[ -n "$alert_lines" ]] || return 0

  if grep -q '^ALERT total_review_queue_size=' <<<"$alert_lines"; then
    local n
    n="$(grep -oE 'total_review_queue_size=[0-9]+' <<<"$alert_lines" | head -1 | cut -d= -f2)"
    printf '%s\n' "${n} item(s) awaiting review"
  fi
  if grep -q '^ALERT oldest_review_item_age_days=' <<<"$alert_lines"; then
    local age
    age="$(grep -oE 'oldest_review_item_age_days=[0-9]+' <<<"$alert_lines" | head -1 | cut -d= -f2)"
    printf '%s\n' "oldest one is ${age}d old"
  fi
  if grep -q '^ALERT classifier_exhaustion_event' <<<"$alert_lines"; then
    printf '%s\n' "classifier exhausted retries against Anthropic"
  fi
  if grep -q '^ALERT repeated_classify_failures=' <<<"$alert_lines"; then
    local n
    n="$(grep -oE 'repeated_classify_failures=[0-9]+' <<<"$alert_lines" | head -1 | cut -d= -f2)"
    printf '%s\n' "classifier failed ${n} runs in a row"
  fi
}

# One notification path, not two, so a run that is both hard-failing and
# has review-alert history produces a single informative page instead of
# either a generic "it failed" (quiet on specifics) or two separate
# notifications (noisy, and macOS may coalesce/drop one anyway).
notify_health(){
  local automation_status="$1"
  local review_stale_out="$2"
  local phrases=()
  if ((automation_status != 0)); then
    # A hard failure (sync auth expired, DB error, disk full -- anything
    # that isn't a classifier-specific API failure) never touches
    # jt-classify.jsonl, so review_alerts() alone can't see it. It's also
    # the more severe signal, so it's always the lead phrase.
    phrases+=("jt-automation.sh exited ${automation_status}")
  fi
  while IFS= read -r phrase; do
    [[ -n "$phrase" ]] && phrases+=("$phrase")
  done < <(review_alert_phrases "$review_stale_out")

  if ((${#phrases[@]} == 0)); then
    log "no alerts; notification skipped"
    return 0
  fi

  local body
  body="$(IFS='; '; echo "${phrases[*]}")"
  local subtitle="jt needs attention"
  ((automation_status != 0)) && subtitle="jt automation failed"
  notify "${body}. Run: jt review stale. Log: ${LOG_DIR}/jt-automation.log" "$subtitle"
}

verify_setup() {
  local py="$1"
  [[ -x "$AUTOMATION_SCRIPT" ]] || die "automation script is not executable: ${AUTOMATION_SCRIPT}"
  [[ -r "${REPO_DIR}/jt.py" ]] || die "missing jt.py"
  [[ -w "$LOG_DIR" ]] || die "log directory is not writable: ${LOG_DIR}"
  PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" "$py" -m py_compile "${REPO_DIR}/jt.py"
  JT_DB="${LOG_DIR}/verify.db" PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" "$py" "${REPO_DIR}/jt.py" where
  log "verified daily stale setup with Python: ${py}"
}

main() {
  local py stale_output review_stale_out automation_status=0
  py="$(python_bin)"

  log "----- jt daily stale start -----"
  verify_setup "$py"

  if ((VERIFY_ONLY)); then
    "$AUTOMATION_SCRIPT" --verify
    log "verification complete"
    return 0
  fi

  # Deliberately not "set -e"-guarded: a failure here must still reach the
  # notification step below, not kill the script before anyone finds out.
  # Silently dying on the worst failure while paging on routine review
  # items would be exactly backwards.
  if ((DRY_RUN)); then
    "$AUTOMATION_SCRIPT" --dry-run || automation_status=$?
  else
    "$AUTOMATION_SCRIPT" || automation_status=$?
  fi
  if ((automation_status != 0)); then
    log "ERROR: ${AUTOMATION_SCRIPT} exited ${automation_status}"
  fi

  # Guarded, not just unguarded-and-hoped-for: these are status reads for
  # the notification below, and a DB/log problem in *reading* status must
  # not itself prevent the page that a DB/log problem would deserve.
  stale_output="$(PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" "$py" "${REPO_DIR}/jt.py" stale)" \
    || stale_output="(jt stale failed to run)"
  /usr/bin/printf '%s\n' "$stale_output"

  review_stale_out="$(PYTHONPATH="$REPO_DIR" PYTHONPYCACHEPREFIX="${LOG_DIR}/pycache" \
    "$py" "${REPO_DIR}/jt.py" review stale --no-fail)" \
    || review_stale_out="(jt review stale failed to run)"
  /usr/bin/printf '%s\n' "$review_stale_out"

  notify_health "$automation_status" "$review_stale_out"

  log "----- jt daily stale complete -----"
  return "$automation_status"
}

if main; then
  exit 0
else
  exit_code=$?
  exit "$exit_code"
fi
