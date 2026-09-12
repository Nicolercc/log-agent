#!/bin/bash
# secret-scan.sh -- dependency-free secret scan for jt.
#
# Scans every file that would actually enter git (tracked files, plus
# anything untracked that `git add` would pick up) for credential-shaped
# content and credential-shaped filenames. Exits non-zero on any hit.
#
# This exists because .gitignore is a filename-pattern allowlist for git,
# not a content scanner: a secret saved under an unanticipated filename, or
# pasted into a file that's otherwise legitimately tracked, sails right past
# it. This script is the second layer.
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Content patterns. Kept broad on purpose -- a false positive costs a human
# a second glance; a false negative costs a leaked credential.
CONTENT_PATTERNS=(
  '"client_secret"[[:space:]]*:'          # Google OAuth client JSON
  '"refresh_token"[[:space:]]*:'          # Google OAuth token JSON
  '"private_key"[[:space:]]*:'            # Google service-account JSON
  'sk-ant-[A-Za-z0-9_-]{20,}'             # Anthropic API key
  'AIza[0-9A-Za-z_-]{35}'                 # Google API key
  'AKIA[0-9A-Z]{16}'                      # AWS access key ID
  'ghp_[0-9A-Za-z]{36}'                   # GitHub PAT
  'xox[baprs]-[0-9A-Za-z-]{10,}'          # Slack token
  '-----BEGIN[A-Z ]*PRIVATE KEY-----'     # any PEM private key
)

# Populated env-var secret assignment (KEY=realvalue, not KEY=... or KEY=).
# Only meaningful in files that are actually env-shaped -- applying it to
# docs/markdown flags every documented variable name as a false positive
# and trains people to ignore the scanner. Checked separately below.
ENV_VALUE_PATTERN='^[A-Z_]*(SECRET|API_KEY|TOKEN)[A-Z_]*=[^[:space:]#.]'
is_env_shaped() {
  [[ "$(basename "$1")" == .env* ]] || [[ "$1" == *.plist ]]
}

# Filename patterns for things that should never be tracked regardless of
# content (a binary DB, a token cache, an OAuth client download).
NAME_PATTERN='(^|/)(credentials.*\.json|.*client_secret.*\.json|.*oauth.*client.*\.json|token.*\.json|.*\.env(\.|$).*|.*\.env|.*\.db|.*\.sqlite3?|.*\.pem|.*\.key)$'

candidate_files() {
  { git ls-files; git ls-files --others --exclude-standard; } | sort -u
}

fail=0
findings=()

while IFS= read -r f; do
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  if [[ "$base" == ".env.example" ]]; then
    continue  # documented placeholder, deliberately tracked
  fi
  if [[ "$f" =~ $NAME_PATTERN ]]; then
    findings+=("FILENAME  $f")
    fail=1
  fi
  # grep -I itself skips files it detects as binary (NUL-byte heuristic),
  # which correctly still scans JSON/CSV/plist -- unlike `file --mime`,
  # which reports JSON as application/json rather than text/*, and so was
  # silently exempting every credentials*.json-shaped file from content
  # scanning. Do not reintroduce a `file`-based pre-filter here.
  for pat in "${CONTENT_PATTERNS[@]}"; do
    if grep -EIlqe "$pat" "$f" 2>/dev/null; then
      findings+=("CONTENT   $f  (pattern: ${pat:0:40})")
      fail=1
    fi
  done
  if is_env_shaped "$f" && grep -EIlqe "$ENV_VALUE_PATTERN" "$f" 2>/dev/null; then
    findings+=("CONTENT   $f  (pattern: populated env secret)")
    fail=1
  fi
done < <(candidate_files)

if ((fail)); then
  printf 'secret-scan: FAIL -- %d finding(s)\n' "${#findings[@]}" >&2
  for line in "${findings[@]}"; do
    printf '  %s\n' "$line" >&2
  done
  exit 1
fi

echo "secret-scan: PASS -- no credential-shaped files or content in tracked/trackable paths"
