#!/usr/bin/env sh
# Stops the companion started by start-companion.sh. Only the recorded PID is stopped, and only if it is still
# companion_server.py; records, worktrees and mission state stay on disk.
cd "$(dirname "$0")"
if [ ! -f data/companion.pid ]; then
  echo "no data/companion.pid; nothing to stop"
  exit 0
fi
PID="$(cat data/companion.pid)"
if ps -p "$PID" -o command= 2>/dev/null | grep -q companion_server.py; then
  kill "$PID"
  rm -f data/companion.pid
  echo "AI DEV companion stopped; records and worktrees preserved."
else
  rm -f data/companion.pid
  echo "PID $PID is not companion_server.py (already stopped?); pid file removed."
fi
