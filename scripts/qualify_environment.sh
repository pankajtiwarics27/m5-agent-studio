#!/usr/bin/env sh
# Run the M5 environment qualification suite on a Docker/Podman host.
#
#   ./scripts/qualify_environment.sh                       # both suites, timestamped artifacts
#   ./scripts/qualify_environment.sh --hardware            # M5-P4-01 only
#   ./scripts/qualify_environment.sh --all --pressure-gb 12
#
# Artifacts land in dist/qualification/ (git-ignored) as
# qualification-<utc-timestamp>.json and .md.
#
# Exit codes: 0 qualified, 1 a required check failed, 2 required checks could
# not run (no runtime / not the target hardware). 2 is the expected result on a
# machine that is not the target environment — nothing is claimed in that case.
#
# Server-startup use: this script is safe to call from an entrypoint or a
# systemd ExecStartPre because it never pulls images, never installs anything,
# and only writes inside the output directory and a temporary workspace. It is
# NOT run automatically by the server; qualification is an operator action, and
# allocating memory on every boot would be hostile to a serving machine. See
# docs/qualification-suite.md for the one-line hook if you want it anyway.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=${QUALIFICATION_OUT:-"$ROOT/dist/qualification"}
JSON="$OUT/qualification-$STAMP.json"
MARKDOWN="$OUT/qualification-$STAMP.md"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not found. Set PYTHON=/path/to/python3." >&2
  exit 2
fi

DRY_RUN=0
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

[ "$DRY_RUN" = "0" ] && mkdir -p "$OUT"

if [ "$DRY_RUN" = "0" ] && ! command -v docker >/dev/null 2>&1 && ! command -v podman >/dev/null 2>&1; then
  echo "warning: neither docker nor podman is on PATH; the sandbox suite will be blocked." >&2
fi

echo "M5 environment qualification -> $OUT"
set +e
"$PYTHON" "$ROOT/scripts/qualify_environment.py" \
  --json "$JSON" \
  --markdown "$MARKDOWN" \
  "$@"
STATUS=$?
set -e

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "dry run: nothing was executed and no artifacts were written."
  exit "$STATUS"
fi

echo
case "$STATUS" in
  0) echo "verdict: qualified (both requested suites passed)";;
  1) echo "verdict: UNQUALIFIED — a required check failed; the guarantee does not hold.";;
  2) echo "verdict: blocked — required checks could not run here (no runtime, no image, an unusable workspace mount, and/or not the target hardware).";;
  *) echo "verdict: suite error (exit $STATUS)";;
esac
echo "artifacts: $JSON $MARKDOWN"
exit "$STATUS"
