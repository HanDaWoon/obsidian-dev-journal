#!/usr/bin/env bash
# SessionEnd / PreCompact hook entrypoint for obsidian-dev-journal.
#
# Claude Code gives SessionEnd hooks a very small shared time budget, so this
# wrapper does almost nothing: it reads the hook payload from stdin, hands it to
# a detached background process, and exits immediately. The capture itself never
# blocks the user and never calls an LLM (zero token cost).

set -uo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.claude/obsidian-dev-journal"
mkdir -p "${STATE_DIR}/pending" "${STATE_DIR}/logs" 2>/dev/null || true

# Housekeeping (cheap, bounded):
# - cap each log at ~512KB by keeping the newest half when it overflows
# - remove payloads older than a day (orphans from runs that never completed)
#
# The rotation truncates in place (`cat >`) instead of `mv`-ing a new file over
# the old one. A capture from a previous session may still be running with the
# log open in append mode: replacing the inode leaves it writing to a file that
# no longer has a name, so that session's log — the one `doctor` reports as
# last_capture_log_line — vanishes exactly when something went wrong.
for lf in "${STATE_DIR}/logs/capture.log" "${STATE_DIR}/logs/hook.log"; do
  if [ -f "$lf" ]; then
    sz=$(wc -c <"$lf" 2>/dev/null || echo 0)
    if [ "${sz:-0}" -gt 524288 ]; then
      if tail -c 262144 "$lf" >"${lf}.tmp.$$" 2>/dev/null; then
        cat "${lf}.tmp.$$" >"$lf" 2>/dev/null
      fi
      rm -f "${lf}.tmp.$$" 2>/dev/null || true
    fi
  fi
done
find "${STATE_DIR}/pending" -name '*.json' -mmin +1440 -delete 2>/dev/null || true

# Pick a python interpreter.
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "obsidian-dev-journal: python3 not found, skipping capture" >>"${STATE_DIR}/logs/hook.log"
  exit 0
fi

# Persist the hook payload, then process it out-of-band.
PAYLOAD="${STATE_DIR}/pending/$(date +%s)-$$.json"
cat >"${PAYLOAD}" 2>/dev/null || exit 0

# Double-fork: the intermediate subshell exits immediately, so the capture is
# reparented to init and survives even if Claude Code kills the hook's process
# group. </dev/null so the child doesn't hold Claude Code's stdin pipe open.
(
  nohup "$PY" "${PLUGIN_ROOT}/scripts/capture_session.py" --payload "${PAYLOAD}" \
    </dev/null >>"${STATE_DIR}/logs/capture.log" 2>&1 &
) &
disown 2>/dev/null || true

exit 0
