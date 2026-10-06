#!/bin/sh
# Send a test alert through the real routing.   ./scripts/test-notify.sh [warning|critical]
# critical -> Pushover EMERGENCY (will repeat until you acknowledge it in the app!)
# warning  -> sits in the morning digest (only delivered 08:00-09:00)
sev="${1:-warning}"
docker exec alertmanager amtool --alertmanager.url=http://localhost:9093 alert add TestAlert \
  severity="$sev" host=test site=pdx \
  --annotation=summary="Test alert ($sev) from $(hostname)" --annotation=description="Safe to ignore. Expires in 5 minutes." \
  --end="$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)" && echo "sent $sev test alert"
