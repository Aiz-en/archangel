#!/bin/zsh
# Started by launchd each weekday morning (see com.archangel.trading.plist.template).
# Runs the main (+70% strict) and shadow (+30% relaxed, separate DB) live
# runners until the closing bell, logging under logs/. Both exit on their own
# after the close (--exit-after-close), or immediately on holidays.
set -u
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# launchd runs a bare non-login shell: no PATH conveniences, no venv. Fail
# LOUDLY if the venv is gone — silently skipping a session is the one thing
# this script must never do.
if [[ ! -r .venv/bin/activate ]]; then
    echo "FATAL: venv missing at $PROJECT_DIR/.venv — trading session skipped." >&2
    exit 1
fi
source .venv/bin/activate
mkdir -p logs
STAMP=$(date +%Y%m%d)

# Keep the machine from idle-sleeping mid-session (lid-close still sleeps —
# see the pmset note in the plist template). Exits when this script does.
caffeinate -i -w $$ &

python code_base/live_runner.py --exit-after-close \
    >> "logs/live_$STAMP.log" 2>&1 &
MAIN_PID=$!

python code_base/live_runner.py --exit-after-close \
    --min-change 30 --db archangel_shadow.db \
    >> "logs/shadow_$STAMP.log" 2>&1 &
SHADOW_PID=$!

MAIN_RC=0; SHADOW_RC=0
wait $MAIN_PID  || MAIN_RC=$?
wait $SHADOW_PID || SHADOW_RC=$?
if (( MAIN_RC != 0 )); then
    echo "main runner exited rc=$MAIN_RC (see logs/live_$STAMP.log)" >&2
fi
if (( SHADOW_RC != 0 )); then
    echo "shadow runner exited rc=$SHADOW_RC (see logs/shadow_$STAMP.log)" >&2
fi
exit $(( MAIN_RC != 0 ? MAIN_RC : SHADOW_RC ))
