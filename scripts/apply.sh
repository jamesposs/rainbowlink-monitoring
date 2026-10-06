#!/bin/sh
# Validate, then apply, the monitoring config. Safe to run any time.
#   sudo ./scripts/apply.sh
# Prometheus re-reads targets/*.yml by itself; this is for rules, routing and compose changes.
set -e
cd "$(dirname "$0")/.."

echo "== validating"
docker exec prometheus promtool check config /etc/prometheus/prometheus.yml | grep -E "FAILED|error" && exit 1 || true
docker exec alertmanager amtool check-config /etc/alertmanager/alertmanager.yml | grep -E "FAILED|error" && exit 1 || true
docker compose config -q

echo "== installing collector timers (systemd/)"
for u in systemd/*.service systemd/*.timer; do install -m 644 "$u" /etc/systemd/system/; done
systemctl daemon-reload
for t in systemd/*.timer; do systemctl enable --now "$(basename "$t")" >/dev/null 2>&1; done

echo "== applying"
docker compose up -d --remove-orphans
docker exec prometheus wget -qO- --post-data= http://localhost:9090/-/reload >/dev/null && echo "prometheus reloaded"
docker exec alertmanager wget -qO- --post-data= http://localhost:9093/-/reload >/dev/null && echo "alertmanager reloaded"

echo "== committing to git"
git add -A >/dev/null 2>&1 && git -c user.name=monitoring -c user.email=monitoring@rainbowlink.org commit -qm "apply $(date -Iseconds)" 2>/dev/null && echo "committed" || echo "no changes"
