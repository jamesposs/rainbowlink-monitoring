#!/usr/bin/env python3
"""Generate grafana/dashboards/status.json (the service status board). Services are NOT listed here: every rl_service_status series
(defined in prometheus/rules/70-service-status.yml) appears on its own. This only lays out the rows, so it needs a rerun only to add a new GROUP.
Run, then gen_map.py (keeps navigation links in sync), then apply.sh."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_map import DS, OUT, nav_for, tgt   # noqa: E402

GREEN, ORANGE, RED = "#22C55E", "#FFA726", "#EF4444"
STATES = {"0": ("No Issues", GREEN), "1": ("Degraded", ORANGE), "2": ("Outage", RED)}
MAPPINGS = [{"type": "value", "options": {k: {"text": t, "color": c, "index": int(k)} for k, (t, c) in STATES.items()}}]
THRESHOLDS = {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": RED, "value": 2}]}

# (title, x, y, width): the row layout. The title must equal the `group` label used in the rules file.
GROUPS = [("Core network", 0, 4, 16), ("Communications", 16, 4, 8), ("Platform", 0, 10, 10), ("Applications", 10, 10, 14)]


def banner():
    mappings = [{"type": "value", "options": {"0": {"text": "All systems operational", "color": GREEN, "index": 0},
                                              "1": {"text": "Some services are degraded", "color": ORANGE, "index": 1},
                                              "2": {"text": "Service outage", "color": RED, "index": 2}}}]
    return {"id": 1, "type": "stat", "title": "", "datasource": DS, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
            "targets": [{**tgt("max(rl_service_status)", "A"), "format": "time_series"}],
            "fieldConfig": {"defaults": {"mappings": mappings, "color": {"mode": "thresholds"}, "thresholds": THRESHOLDS, "noValue": "Status unknown"}, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "textMode": "value", "justifyMode": "center",
                        "text": {"valueSize": 40}, "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def group_panel(pid, title, x, y, w):
    return {"id": pid, "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": 6},
            "targets": [{**tgt(f'rl_service_status{{group="{title}"}}', "A"), "format": "time_series", "legendFormat": "{{service}}"}],
            "fieldConfig": {"defaults": {"mappings": MAPPINGS, "color": {"mode": "thresholds"}, "thresholds": THRESHOLDS, "noValue": "unknown"}, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center", "orientation": "auto",
                        "text": {"titleSize": 14, "valueSize": 22}, "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def issues():
    hide = ["Time", "__name__", "alertstate", "job", "instance", "kind", "parent", "role", "monitor"]
    return {"id": 20, "type": "table", "title": "What is firing right now", "datasource": DS, "gridPos": {"x": 0, "y": 16, "w": 24, "h": 7},
            "description": "Alerts that have waited out their own delay. A tile can be Degraded before anything shows here (it reacts after ~3 minutes; alerts after 5-15).",
            "targets": [{**tgt('ALERTS{alertstate="firing", alertname!="Watchdog"}', "A"), "format": "table"}],
            "transformations": [{"id": "organize", "options": {"excludeByName": {k: True for k in hide},
                                 "renameByName": {"alertname": "Alert", "severity": "Severity", "host": "Host", "site": "Site", "service": "Service"}}}],
            "fieldConfig": {"defaults": {"noValue": "Nothing is firing", "custom": {"align": "auto"}}, "overrides": [
                {"matcher": {"id": "byName", "options": "Severity"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "mappings", "value": [
                    {"type": "value", "options": {"critical": {"color": RED, "index": 0}, "warning": {"color": ORANGE, "index": 1}}}]}]}]},
            "options": {"showHeader": True, "cellHeight": "sm"}}


def timeline():
    return {"id": 21, "type": "state-timeline", "title": "Status history", "datasource": DS, "gridPos": {"x": 0, "y": 23, "w": 24, "h": 11},
            "targets": [{**tgt("rl_service_status", "A"), "instant": False, "range": True, "format": "time_series", "legendFormat": "{{service}}", "interval": "1m"}],
            "fieldConfig": {"defaults": {"mappings": MAPPINGS, "color": {"mode": "thresholds"}, "thresholds": THRESHOLDS,
                                         "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
            "options": {"mergeValues": True, "showValue": "never", "rowHeight": 0.85, "alignValue": "center", "legend": {"showLegend": False},
                        "tooltip": {"mode": "single", "sort": "none"}}}


def main():
    d = {"uid": "rl-status", "title": "Status", "tags": ["rainbowlink", "status"], "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
         "graphTooltip": 0, "refresh": "30s", "time": {"from": "now-24h", "to": "now"}, "links": nav_for("rl-status"),
         "description": "One tile per service: No Issues, Degraded or Outage. What each state means is defined in prometheus/rules/70-service-status.yml.",
         "panels": [banner()] + [group_panel(10 + i, *g) for i, g in enumerate(GROUPS)] + [issues(), timeline()],
         "templating": {"list": []}, "annotations": {"list": []}}
    with open(os.path.join(OUT, "status.json"), "w") as f:
        json.dump(d, f, indent=1)
    print("wrote status.json")


if __name__ == "__main__":
    main()
