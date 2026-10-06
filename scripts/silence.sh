#!/bin/sh
# Maintenance window.  ./scripts/silence.sh <host> <duration e.g. 2h> "reason"
# Silences every alert for that host (and its services). Expires by itself.
[ $# -ge 2 ] || { echo "usage: $0 <host> <duration> [reason]"; exit 1; }
docker exec alertmanager amtool --alertmanager.url=http://localhost:9093 silence add host="$1" \
  --duration="$2" --author="${SUDO_USER:-$USER}" --comment="${3:-maintenance}"
