#!/usr/bin/env python3
"""Generates grafana/dashboards/map.json: a US map of the sites with every link coloured by its health.

  python3 tools/gen_map.py && sudo ./scripts/apply.sh      (Grafana picks the file up within ~2 minutes)

ADD A SITE: add it to SITES (city-level coordinates are enough: do not use a street address). Links are drawn between every
pair of sites, using the ping mesh (rl_mesh_* metrics, measured from each site; see the monitoring_agent Ansible role).
Line colour = link health, worst of the two directions:
  green  normal                                    orange  loss > 2%, or latency > 2x its own 1-day norm (and +30 ms)
  red    loss > 10% (or the link is down)
Marker colour = site health (failing checks + firing alerts at that site).
This script also adds the "Site map" shortcut to the other dashboards' navigation (idempotent).
"""
import itertools, json, os, glob

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grafana", "dashboards")
DS = {"type": "prometheus", "uid": "prometheus"}
GREEN, ORANGE, RED, GREY = "#22C55E", "#FFA726", "#EF4444", "#6b7280"

# City-level positions only. `site` must match the `site` label used by the mesh (pdx/pcb/sjc/bos).
SITES = {
    "pdx": {"name": "Home (Portland)",   "lat": 45.52, "lon": -122.68},
    "sjc": {"name": "San Jose VPS",      "lat": 37.34, "lon": -121.89},
    "bos": {"name": "Boston VPS / PBX",  "lat": 42.36, "lon": -71.06},
    "pcb": {"name": "Panama City Beach", "lat": 30.18, "lon": -85.80},
}
PAIRS = list(itertools.combinations(SITES, 2))
OV = ', path="overlay"'


def lab(expr, **labels):
    """Attach constant labels to an unlabelled vector (label_replace with an empty source always matches)."""
    for k, v in labels.items():
        expr = f'label_replace({expr}, "{k}", "{v}", "", "")'
    return expr


def union(metric, a, b, extra):
    return f'({metric}{{site="{a}", to="{b}"{extra}}} or {metric}{{site="{b}", to="{a}"{extra}}})'


def link_exprs(a, b):
    """Health classes for the link a<->b, taking the worst of both directions. Each expression is an unlabelled vector."""
    rtt = f'avg({union("rl_mesh_rtt_seconds", a, b, ", stat=\"avg\"" + OV)})'
    loss = f'max({union("rl_mesh_loss_ratio", a, b, OV)})'
    base = (f'avg(avg_over_time(rl_mesh_rtt_seconds{{site="{a}", to="{b}", stat="avg"{OV}}}[1d]) '
            f'or avg_over_time(rl_mesh_rtt_seconds{{site="{b}", to="{a}", stat="avg"{OV}}}[1d]))')
    slow = f'(({rtt}) > 2 * ({base})) and (({rtt}) > ({base}) + 0.03)'
    return {
        "good": f'(({rtt}) * 1000) unless ({loss} > 0.02) unless ({slow})',
        "degraded": f'((({rtt}) * 1000) and (({loss} > 0.02) or ({slow}))) unless ({loss} > 0.10)',
        "bad": f'100 * ({loss} > 0.10)',
    }


def class_query(kind):
    """One instant query per class, returning BOTH end points of every link in that class (rows grouped by `link`)."""
    parts = []
    for a, b in PAIRS:
        core = link_exprs(a, b)[kind]
        name = f'{SITES[a]["name"].split(" ")[0]} - {SITES[b]["name"].split(" ")[0]}'
        for end, s in (("a", a), ("b", b)):
            parts.append(lab(core, link=f'{a.upper()} - {b.upper()}', end=end, lat=SITES[s]["lat"], lon=SITES[s]["lon"]))
    return " or ".join(parts)


def site_query():
    parts = []
    for s, v in SITES.items():
        health = (f'((count(probe_success{{site="{s}"}} == 0) or vector(0)) + '
                  f'(count(ALERTS{{site="{s}", alertstate="firing", severity!="none"}}) or vector(0)))')
        parts.append(lab(health, name=v["name"], site=s, lat=v["lat"], lon=v["lon"]))
    return " or ".join(parts)


def tgt(expr, ref):
    return {"datasource": DS, "expr": expr, "refId": ref, "instant": True, "range": False, "format": "table"}


def route_layer(name, ref, color):
    return {"type": "route", "name": name, "tooltip": True, "filterData": {"id": "byRefId", "options": ref},
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
            "config": {"style": {"color": {"fixed": color}, "opacity": 0.9, "lineWidth": 4,
                                 "size": {"fixed": 4, "min": 1, "max": 10},
                                 "rotation": {"fixed": 0, "min": -360, "max": 360, "mode": "mod"}}, "arrow": 0}}


def hide(*fields):
    return [{"matcher": {"id": "byName", "options": f}, "properties": [{"id": "custom.hideFrom", "value": {"legend": True, "tooltip": True, "viz": False}}]} for f in fields]


def geomap():
    layers = [
        route_layer("Healthy links", "A", GREEN),
        route_layer("Degraded links", "B", ORANGE),
        route_layer("Down / lossy links", "C", RED),
        {"type": "markers", "name": "Sites", "tooltip": True, "filterData": {"id": "byRefId", "options": "D"},
         "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
         "config": {"showLegend": False, "style": {
             "size": {"fixed": 16, "min": 2, "max": 15}, "color": {"field": "Value", "fixed": GREEN}, "opacity": 1,
             "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
             "text": {"mode": "field", "field": "name", "fixed": ""},
             "textConfig": {"fontSize": 13, "offsetX": 0, "offsetY": -20, "textAlign": "center", "textBaseline": "middle"},
             "rotation": {"fixed": 0, "min": -360, "max": 360, "mode": "mod"}}}},
    ]
    return {
        "id": 1, "type": "geomap", "title": "Site-to-site links", "datasource": DS, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 17},
        "description": "Line colour = health of the link (worst of both directions): green normal, orange loss >2% or latency >2x its norm, red loss >10% / down. Marker colour = site health.",
        "targets": [tgt(class_query("good"), "A"), tgt(class_query("degraded"), "B"), tgt(class_query("bad"), "C"), tgt(site_query(), "D")],
        "transformations": [
            {"id": "partitionByValues", "filter": {"id": "byRefId", "options": "A|B|C"}, "options": {"fields": ["link"], "keepFields": True, "naming": {"asLabels": False}}},
            {"id": "convertFieldType", "options": {"conversions": [{"targetField": "lat", "destinationType": "number"}, {"targetField": "lon", "destinationType": "number"}], "fields": {}}},
        ],
        "fieldConfig": {
            "defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": RED, "value": 3}]},
                         "color": {"mode": "thresholds"}},
            "overrides": hide("Time", "lat", "lon", "end", "site") + [
                {"matcher": {"id": "byFrameRefID", "options": "A"}, "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "Latency"}]},
                {"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [{"id": "unit", "value": "ms"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "Latency"}]},
                {"matcher": {"id": "byFrameRefID", "options": "C"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0}, {"id": "displayName", "value": "Packet loss"}]},
                {"matcher": {"id": "byFrameRefID", "options": "D"}, "properties": [{"id": "decimals", "value": 0}, {"id": "displayName", "value": "Open issues"}]},
            ]},
        "options": {
            "view": {"id": "coords", "lat": 38.6, "lon": -96.5, "zoom": 4, "allLayers": True},
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showAttribution": True, "showScale": False, "showMeasure": False, "showDebug": False},
            "basemap": {"type": "carto", "name": "Basemap", "config": {"showLabels": True, "theme": "auto"}},
            "layers": layers, "tooltip": {"mode": "details"}},
    }


def link_table():
    q = lambda e, ref: {"datasource": DS, "expr": e, "refId": ref, "instant": True, "format": "table"}
    th = lambda *p: {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in p]}
    ov = lambda col, props: {"matcher": {"id": "byName", "options": col}, "properties": props}
    return {
        "id": 2, "type": "table", "title": "Link detail (each direction is measured from its own site)", "datasource": DS, "gridPos": {"x": 0, "y": 17, "w": 15, "h": 9},
        "targets": [q('avg by (site, to) (rl_mesh_rtt_seconds{stat="avg", path="overlay"})', "A"), q('avg by (site, to) (rl_mesh_loss_ratio{path="overlay"})', "B"),
                    q('avg by (site, to) (rl_mesh_rtt_seconds{stat="jitter", path="overlay"})', "C"), q('avg by (site, to) (avg_over_time(rl_mesh_rtt_seconds{stat="avg", path="overlay"}[1d]))', "D")],
        "transformations": [{"id": "merge", "options": {}},
                            {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                           "renameByName": {"site": "From", "to": "To", "Value #A": "Latency", "Value #B": "Loss", "Value #C": "Jitter", "Value #D": "Normal (1 day)"},
                                                           "indexByName": {"site": 0, "to": 1, "Value #A": 2, "Value #D": 3, "Value #B": 4, "Value #C": 5}}}],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True}},
                        "overrides": [ov("Latency", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 3}, {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                                                    {"id": "thresholds", "value": th((None, "green"), (0.15, "orange"), (0.3, "red"))}]),
                                      ov("Normal (1 day)", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 3}]),
                                      ov("Loss", [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 1}, {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                                                  {"id": "thresholds", "value": th((None, "green"), (0.02, "orange"), (0.1, "red"))}]),
                                      ov("Jitter", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 4}])]},
        "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "From", "desc": False}]},
    }


def legend():
    md = ("### How to read the map\n"
          "**Lines** are the tailnet links between sites, coloured by health (worst of the two directions):\n\n"
          "- <span style='color:#22C55E'>**green**</span> normal\n"
          "- <span style='color:#FFA726'>**orange**</span> loss above 2%, or latency more than twice its own 1-day norm\n"
          "- <span style='color:#EF4444'>**red**</span> loss above 10%, or the link is down\n\n"
          "**Markers** are the sites: green = nothing wrong there, orange = failing checks or firing alerts at that site.\n\n"
          "Hover a line or marker for numbers. Latency is compared with each link's *own* normal, so a long link (Portland to Florida, ~90 ms) is not flagged for being long.\n\n"
          "Underlying data: ping mesh (10 pings a minute from every site), plus the Network dashboard for the full matrix.")
    return {"id": 3, "type": "text", "title": "", "gridPos": {"x": 15, "y": 17, "w": 9, "h": 9}, "options": {"mode": "markdown", "content": md}}


NAV = [("rl-overview", "Overview"), ("rl-map", "Site map"), ("rl-network", "Network"), ("rl-servers", "Servers & storage"), ("rl-services", "Services")]


def nav_for(uid):
    return [{"title": t, "type": "link", "url": f"/d/{u}", "icon": "dashboard", "targetBlank": False} for u, t in NAV if u != uid]


def main():
    d = {"uid": "rl-map", "title": "Site map", "tags": ["rainbowlink", "network"], "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
         "graphTooltip": 1, "refresh": "1m", "time": {"from": "now-6h", "to": "now"}, "links": nav_for("rl-map"),
         "panels": [geomap(), link_table(), legend()], "templating": {"list": []}, "annotations": {"list": []}}
    with open(os.path.join(OUT, "map.json"), "w") as f:
        json.dump(d, f, indent=1)
    print("wrote map.json")
    for p in glob.glob(os.path.join(OUT, "*.json")):
        j = json.load(open(p))
        if j.get("uid") in dict(NAV) and j["uid"] != "rl-map":
            want = nav_for(j["uid"])
            if j.get("links") != want:
                j["links"] = want
                json.dump(j, open(p, "w"), indent=1)
                print("updated navigation in", os.path.basename(p))


if __name__ == "__main__":
    main()
