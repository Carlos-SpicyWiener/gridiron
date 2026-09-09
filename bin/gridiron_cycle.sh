#!/usr/bin/env bash
# One full weekly cycle, in the only order that is correct:
#
#   sync    pull new results and next week's schedule
#   rate    rebuild ratings from those results
#   grade   score the picks whose games have now finished
#   odds    observe the market (skipped silently with no key)
#   predict write picks for the upcoming slate, from the NEW ratings
#   export  write the pick record to tracked CSV and commit it locally
#
# grade runs before predict so a week is always scored against the ratings that
# existed when the pick was made, never against ratings that have since seen the
# result. Reordering these two would quietly turn the record into a fiction.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GI="$ROOT/bin/gridiron"
LOG="$ROOT/log/cycle.log"

exec >>"$LOG" 2>&1
echo "=== cycle $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

"$GI" sync
"$GI" rate
"$GI" grade
if [ -n "${GRIDIRON_ODDS_KEY:-}" ]; then
  "$GI" odds || echo "odds step failed; continuing without market data"
else
  echo "GRIDIRON_ODDS_KEY unset — skipping market lines"
fi
"$GI" predict

# Refresh the durable record. Committed locally so a lost database cannot take
# the pick history with it; pushing stays manual, because pushing publishes.
"$GI" export --commit

echo "=== cycle complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
