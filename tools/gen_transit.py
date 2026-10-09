#!/usr/bin/env python3
"""Generate grafana/dashboards/transit.json (Home Assistant transit board). Run, then run gen_map.py (it keeps the navigation links in sync), then apply.sh.
Data comes from scripts/ha-transit-metrics.py (rl_ha_transit_*); a new stop in Home Assistant shows up on its own."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_map import DS, OUT, nav_for, tgt   # noqa: E402

INFO = " * on(entity) group_left(name, stop, line, destination) rl_ha_transit_info"
TILE_NAME = "{{name}} · {{line}} to {{destination}}"
GREEN, ORANGE, RED = "#22C55E", "#FFA726", "#EF4444"


def stat(pid, title, expr, unit, x, w, steps, desc="", legend=TILE_NAME):
    return {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": {"x": x, "y": 0, "w": w, "h": 6},
            "targets": [{**tgt(expr, "A"), "legendFormat": legend}],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "noValue": "no service", "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center", "orientation": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def arrivals_table():
    hide = ["Time", "__name__", "entity", "idx", "job", "instance", "host", "kind", "parent", "role", "site"]
    return {"id": 3, "type": "table", "title": "Upcoming arrivals", "datasource": DS, "gridPos": {"x": 0, "y": 6, "w": 16, "h": 11},
            "description": "Every arrival Home Assistant knows about, soonest first. 'Catchable' = you can still walk to the stop in time.",
            "targets": [{**tgt("rl_ha_transit_arrival_minutes" + INFO.replace(", line, destination", ""), "A"), "instant": True, "range": False, "format": "table"}],
            "transformations": [{"id": "organize", "options": {"excludeByName": {k: True for k in hide},
                                 "renameByName": {"name": "Stop", "line": "Line", "destination": "To", "direction": "Direction", "Value": "In", "catchable": "Catchable", "status": "Status"},
                                 "indexByName": {"name": 0, "line": 1, "destination": 2, "direction": 3, "Value": 4, "catchable": 5, "status": 6}}}],
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                            "overrides": [
                                {"matcher": {"id": "byName", "options": "In"}, "properties": [
                                    {"id": "unit", "value": "m"}, {"id": "decimals", "value": 0}, {"id": "custom.width", "value": 90},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": RED, "value": None}, {"color": ORANGE, "value": 4}, {"color": GREEN, "value": 8}]}}]},
                                {"matcher": {"id": "byName", "options": "Catchable"}, "properties": [
                                    {"id": "custom.width", "value": 100}, {"id": "mappings", "value": [{"type": "value", "options": {
                                        "yes": {"text": "yes", "color": GREEN, "index": 0}, "no": {"text": "too soon", "color": "#94A3B8", "index": 1}}}]},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                                {"matcher": {"id": "byName", "options": "Status"}, "properties": [
                                    {"id": "mappings", "value": [{"type": "value", "options": {"on time": {"text": "on time", "color": GREEN, "index": 0}}},
                                                                  {"type": "regex", "options": {"pattern": "canceled", "result": {"color": RED, "index": 1}}},
                                                                  {"type": "regex", "options": {"pattern": "\\+.*", "result": {"color": ORANGE, "index": 2}}}]},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]},
            "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "In", "desc": False}]}}


def stops_table():
    hide = ["Time", "__name__", "entity", "job", "instance", "host", "kind", "parent", "role", "site", "summary", "line", "destination", "vehicle"]
    return {"id": 4, "type": "table", "title": "Stops", "datasource": DS, "gridPos": {"x": 16, "y": 6, "w": 8, "h": 11},
            "targets": [{**tgt("rl_ha_transit_approach_minutes * on(entity) group_left(name, stop, stop_id, service) rl_ha_transit_info", "A"), "instant": True, "range": False, "format": "table"}],
            "transformations": [{"id": "organize", "options": {"excludeByName": {k: True for k in hide},
                                 "renameByName": {"name": "Sensor", "stop": "Stop", "stop_id": "ID", "Value": "Walk (min)", "service": "In service"},
                                 "indexByName": {"name": 0, "stop": 1, "stop_id": 2, "Value": 3, "service": 4}}}],
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": [
                {"matcher": {"id": "byName", "options": "In service"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "mappings", "value": [
                    {"type": "value", "options": {"yes": {"text": "yes", "color": GREEN, "index": 0}, "no": {"text": "no", "color": RED, "index": 1}}}]}]}]},
            "options": {"showHeader": True, "cellHeight": "sm"}}


def series(pid, title, expr, unit, x, y, desc=""):
    return {"id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": DS, "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
            "targets": [{**tgt(expr, "A"), "legendFormat": "{{name}}"}],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0, "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never", "spanNulls": False}}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "none"}}}


def main():
    d = {"uid": "rl-transit", "title": "Transit", "tags": ["rainbowlink", "home-assistant"], "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
         "graphTooltip": 1, "refresh": "30s", "time": {"from": "now-6h", "to": "now"}, "links": nav_for("rl-transit"),
         "description": "Live TriMet arrivals from Home Assistant (sensors with an 'arrivals' attribute are discovered automatically).",
         "panels": [
             stat(1, "Next catchable", "rl_ha_transit_next_catchable_minutes" + INFO, "m", 0, 16,
                  [{"color": RED, "value": None}, {"color": ORANGE, "value": 3}, {"color": GREEN, "value": 8}],
                  "Minutes until the next vehicle you can still walk to in time. Red = leave now or you'll miss it."),
             stat(2, "Delay of next vehicle", "rl_ha_transit_next_delay_minutes" + INFO.replace(", line, destination", ""), "m", 16, 8,
                  [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 2}, {"color": RED, "value": 6}], "How late the very next vehicle is running.", "{{name}}"),
             arrivals_table(), stops_table(),
             series(5, "Minutes to next catchable arrival", "rl_ha_transit_next_catchable_minutes" + INFO.replace(", line, destination", ""), "m", 0, 17,
                    "Sawtooth: counts down, jumps up when a vehicle is gone. Long jumps = a gap in service."),
             series(6, "Delay of next vehicle", "rl_ha_transit_next_delay_minutes" + INFO.replace(", line, destination", ""), "m", 12, 17)],
         "templating": {"list": []}, "annotations": {"list": []}}
    with open(os.path.join(OUT, "transit.json"), "w") as f:
        json.dump(d, f, indent=1)
    print("wrote transit.json")


if __name__ == "__main__":
    main()
