#!/usr/bin/env bash
# One full weekly cycle, in the only order that is correct:
#
#   sync    pull new results and next week's schedule
#   rate    rebuild ratings from those results
#   grade   score the picks whose games have now finished
#   odds    observe the market (skipped silently with no key)
#   predict write picks for the upcoming slate, from the NEW ratings
#   export  write the pick record to tracked CSV, commit, and push it
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

# Settle any bet whose game has now finished. This runs after grade, so the
# final score is already in, and before export so the record it publishes is
# current. Kalshi quotes are captured separately by gridiron-poll.timer -- a
# closing price cannot be recovered here, because a settled market quotes
# 0.99/0.01 over a dead book.
"$GI" bet grade || echo "bet grade failed; continuing"

# Refresh the durable record and publish it. Only record/ is ever staged, so a
# cycle cannot commit the database, the logs, or the token file. A failed push
# leaves the commit in place rather than retrying blind.
"$GI" export --push

echo "=== cycle complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
