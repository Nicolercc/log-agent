#!/bin/bash
# install-launchd.sh -- render the launchd/*.plist.template files for this
# machine and (optionally) load them.
#
# The templates are tracked because they're product: reinstalling automation
# from a fresh clone should not require retyping absolute paths by hand. The
# rendered plists are NOT tracked -- they are machine-local state (this
# user's home directory, this clone's location on disk) and are written
# under ~/Library/LaunchAgents, same as any other launchd job on the system.
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="${REPO_DIR}/launchd"
DEST_DIR="${HOME}/Library/LaunchAgents"
JOBTRACK_HOME="${JT_HOME:-$HOME/.jobtrack}"

LOAD=0
usage() {
  /usr/bin/printf 'usage: %s [--load]\n' "$0"
  /usr/bin/printf '  --load   also launchctl bootstrap the rendered jobs\n'
}
while (($#)); do
  case "$1" in
    --load) LOAD=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
  shift
done

shopt -s nullglob
templates=("${TEMPLATE_DIR}"/*.plist.template)
if ((${#templates[@]} == 0)); then
  echo "error: no *.plist.template files found in ${TEMPLATE_DIR}" >&2
  exit 1
fi

/bin/mkdir -p "$DEST_DIR" "$JOBTRACK_HOME/logs"

rendered=()
for tmpl in "${templates[@]}"; do
  name="$(basename "$tmpl" .template)"
  out="${DEST_DIR}/${name}"
  sed \
    -e "s#__REPO_DIR__#${REPO_DIR}#g" \
    -e "s#__JOBTRACK_HOME__#${JOBTRACK_HOME}#g" \
    "$tmpl" > "$out"
  rendered+=("$out")
  echo "rendered: $out"
done

if ((LOAD)); then
  for plist in "${rendered[@]}"; do
    label="$(/usr/bin/basename "$plist" .plist)"
    /bin/launchctl bootout "gui/$(/usr/bin/id -u)" "$plist" 2>/dev/null || true
    /bin/launchctl bootstrap "gui/$(/usr/bin/id -u)" "$plist"
    echo "loaded: $label"
  done
fi

echo
echo "Next: create/verify a Gmail token before enabling unattended mode:"
echo "  ${REPO_DIR}/.venv/bin/python ${REPO_DIR}/sync.py --dry-run"
echo "Reload after editing a template:"
echo "  $0 --load"
