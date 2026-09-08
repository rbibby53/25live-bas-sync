#!/bin/sh
# Entrypoint for the 25Live -> Niagara sync container.
#
# Default (SYNC_AT unset): a ONE-SHOT run. Whatever args were passed after the
# image name go straight to main.py (e.g. --validate / --dry-run / --discover),
# then the container exits with main.py's exit code. Schedule it however you
# already schedule jobs — host cron, `docker compose run`, or a k8s CronJob.
#
# Optional built-in scheduler (SYNC_AT=HH:MM, 24-hour, in the container's TZ):
# the container stays up and runs the sync once per day at that time. Set
# SYNC_ON_START=1 to also run once immediately on startup. This makes
# `docker compose up -d` a self-contained nightly sync with no external cron.
set -eu

run_sync() {
    # Don't let a single failed run kill the scheduler loop; main.py already
    # logs the reason and alerts (if configured).
    python main.py "$@" || echo "[entrypoint] sync exited non-zero ($?)" >&2
}

# One-shot mode — the idiomatic container default.
if [ -z "${SYNC_AT:-}" ]; then
    exec python main.py "$@"
fi

# Validate SYNC_AT once up front so a typo fails fast instead of looping.
if ! echo "$SYNC_AT" | grep -Eq '^[0-2][0-9]:[0-5][0-9]$'; then
    echo "[entrypoint] SYNC_AT='$SYNC_AT' is not HH:MM (24-hour). Exiting." >&2
    exit 2
fi

echo "[entrypoint] scheduler on: 'python main.py $*' daily at $SYNC_AT (TZ=${TZ:-UTC})"

if [ "${SYNC_ON_START:-0}" = "1" ]; then
    echo "[entrypoint] running once on start"
    run_sync "$@"
fi

while true; do
    now_s=$(date +%s)
    next_s=$(date -d "today $SYNC_AT" +%s)
    [ "$next_s" -le "$now_s" ] && next_s=$(date -d "tomorrow $SYNC_AT" +%s)
    wait_s=$((next_s - now_s))
    echo "[entrypoint] next run at $(date -d "@$next_s" '+%Y-%m-%d %H:%M %Z') (in ${wait_s}s)"
    sleep "$wait_s"
    run_sync "$@"
done
