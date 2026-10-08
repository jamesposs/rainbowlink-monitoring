#!/usr/bin/env python3
"""Generates grafana/dashboards/map.json: a US map of the sites with every link coloured by its health.

  python3 tools/gen_map.py && sudo ./scripts/apply.sh      (Grafana picks the file up within ~2 minutes)

ADD A SITE: add it to SITES (city-level coordinates are enough: do not use a street address). Links are drawn between every
pair of sites, using the ping mesh (rl_mesh_* metrics, measured from each site; see the monitoring_agent Ansible role).
Line colour = link health, worst of the two directions:
  green  normal                                    orange  loss > 2% or latency > 2x its own 1-day norm (and +30 ms), both as 5-minute averages
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
    def smooth(metric, extra):   # 5-minute average per direction, so one lost ping cannot flip a line's colour
        return (f'avg_over_time({metric}{{site="{a}", to="{b}"{extra}}}[5m]) or avg_over_time({metric}{{site="{b}", to="{a}"{extra}}}[5m])')
    rtt = f'avg({smooth("rl_mesh_rtt_seconds", ", stat=\"avg\"" + OV)})'
    loss = f'max({smooth("rl_mesh_loss_ratio", OV)})'
    base = (f'avg(avg_over_time(rl_mesh_rtt_seconds{{site="{a}", to="{b}", stat="avg"{OV}}}[1d]) '
            f'or avg_over_time(rl_mesh_rtt_seconds{{site="{b}", to="{a}", stat="avg"{OV}}}[1d]))')
    slow = f'(({rtt}) > 2 * ({base})) and (({rtt}) > ({base}) + 0.03)'
    return {
        "good": f'(({rtt}) * 1000) unless ({loss} > 0.02) unless ({slow})',
        "degraded": f'((({rtt}) * 1000) and (({loss} > 0.02) or ({slow}))) unless ({loss} > 0.10)',
        "bad": f'100 * ({loss} > 0.10)',
    }


CLASS_PREFIX = {"good": "g", "degraded": "d", "bad": "b"}


def link_targets():
    """One instant query per (class, link). Each returns one frame with two rows (the link's two end points) when the link is in
    that class, and nothing otherwise. refIds g1.. (healthy) d1.. (degraded) b1.. (down/lossy) drive the layer filters."""
    out = []
    for kind, pre in CLASS_PREFIX.items():
        for i, (a, b) in enumerate(PAIRS, 1):
            core = link_exprs(a, b)[kind]
            parts = [lab(core, link=f"{a.upper()} - {b.upper()}", end=end, lat=SITES[s]["lat"], lon=SITES[s]["lon"]) for end, s in (("a", a), ("b", b))]
            out.append(tgt(" or ".join(parts), f"{pre}{i}"))
    return out


# Traffic bands (bits/s, both directions, 5-minute average). Each link lands in exactly one band and is drawn as a translucent blue ribbon of that
# width under its health line. (Grafana's route layer does not scale line width from a data field, so bands are separate fixed-width layers.)
# Typical load here is 2-25 kbit/s; a backup burst lands in "busy". Adjust the limits and widths if the links get busier.
TRAFFIC_BANDS = [("q", "quiet", None, 5_000, 6), ("m", "moderate", 5_000, 25_000, 12), ("b", "busy", 25_000, None, 20)]   # (id, name, over, up to, width px)
RIBBON = "#3B82F6"


def traffic_targets():
    """One instant query per (band, link): both end points, value = traffic in bits/s; empty when the link is not in that band."""
    out = []
    for i, (a, b) in enumerate(PAIRS, 1):
        t = traffic_exprs()[i - 1]
        both = f'({t["fwd"]} + {t["rev"]})'
        for bid, _, lo, hi, _w in TRAFFIC_BANDS:
            conds = ([f'({both} > {lo})'] if lo is not None else []) + ([f'({both} <= {hi})'] if hi is not None else [])
            core = " and ".join(conds) if conds else both
            parts = [lab(core, link=f"{a.upper()} - {b.upper()}", end=end, lat=SITES[x]["lat"], lon=SITES[x]["lon"]) for end, x in (("a", a), ("b", b))]
            out.append(tgt(" or ".join(parts), f"t{bid}{i}"))
    return out


def mid(a, b):
    return round((SITES[a]["lat"] + SITES[b]["lat"]) / 2, 3), round((SITES[a]["lon"] + SITES[b]["lon"]) / 2, 3)


def label_targets():
    """ML: latency (ms) and MT: traffic (bits/s), one row per link at the link's midpoint, shown as small text labels on the map."""
    lat_parts, trf_parts = [], []
    for i, (a, b) in enumerate(PAIRS, 1):
        la, lo = mid(a, b)
        rtt_ms = (f'round(avg(avg_over_time(rl_mesh_rtt_seconds{{site="{a}", to="{b}", stat="avg"{OV}}}[5m]) '
                  f'or avg_over_time(rl_mesh_rtt_seconds{{site="{b}", to="{a}", stat="avg"{OV}}}[5m])) * 1000)')
        t = traffic_exprs()[i - 1]
        lat_parts.append(lab(rtt_ms, link=f"{a.upper()} - {b.upper()}", lat=la, lon=lo))
        trf_parts.append(lab(f'({t["fwd"]} + {t["rev"]})', link=f"{a.upper()} - {b.upper()}", lat=la, lon=lo))
    return [tgt(" or ".join(lat_parts), "ML"), tgt(" or ".join(trf_parts), "MT")]


def label_layer(name, ref, color, dy, field):
    return {"type": "markers", "name": name, "tooltip": True, "filterData": {"id": "byRefId", "options": ref},
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
            "config": {"showLegend": False, "style": {
                "size": {"fixed": 3, "min": 2, "max": 15}, "color": {"fixed": color}, "opacity": 1,
                "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"},
                "text": {"mode": "field", "field": field, "fixed": ""},
                "textConfig": {"fontSize": 12, "offsetX": 0, "offsetY": dy, "textAlign": "center", "textBaseline": "middle"},
                "rotation": {"fixed": 0, "min": -360, "max": 360, "mode": "mod"}}}}


def ribbon_layers():
    out = []
    for bid, name, _lo, _hi, width in TRAFFIC_BANDS:
        for i, (a, b) in enumerate(PAIRS, 1):
            out.append({"type": "route", "name": f"Traffic ({name}): {a.upper()} - {b.upper()}", "tooltip": True, "filterData": {"id": "byRefId", "options": f"t{bid}{i}"},
                        "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
                        "config": {"style": {"color": {"fixed": RIBBON}, "opacity": 0.35, "lineWidth": width,
                                             "size": {"fixed": width, "min": 1, "max": 30},
                                             "rotation": {"fixed": 0, "min": -360, "max": 360, "mode": "mod"}}, "arrow": 0}})
    return out


CLASS_STYLE = {"good": ("Healthy", GREEN), "degraded": ("Degraded", ORANGE), "bad": ("Down/lossy", RED)}


def link_layers():
    """Bottom-to-top: healthy, then degraded, then down/lossy, so the worst state of a link is always on top."""
    out = []
    for kind in ("good", "degraded", "bad"):
        label, color = CLASS_STYLE[kind]
        for i, (a, b) in enumerate(PAIRS, 1):
            out.append(route_layer(f"{label}: {a.upper()} - {b.upper()}", f"{CLASS_PREFIX[kind]}{i}", color))
    return out


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


# 1x1 transparent PNG. A tile server (Carto/OSM) would need an API key or send every map view to a third party, and Carto's free tiles now
# demand a key. Grafana ships public/maps/usa-states.geojson, so the map draws entirely from the local server instead.
def _solid_png(rgb):
    """A valid 1x1 PNG of one colour, as a data: URI (stdlib only)."""
    import base64, struct, zlib
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"\x00" + bytes(rgb)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


BLANK_TILE = _solid_png((11, 16, 32))        # #0b1020: the "ocean", a dark navy to match the dashboards


def states_layer():
    return {"type": "geojson", "name": "US states", "tooltip": False,
            "config": {"src": "public/maps/usa-states.geojson", "rules": [],
                       "style": {"color": {"fixed": "#1e293b"}, "opacity": 0.85, "lineWidth": 1, "size": {"fixed": 5, "min": 1, "max": 10},
                                 "rotation": {"fixed": 0, "min": -360, "max": 360, "mode": "mod"}}}}


OVERRIDE_UNITS = ([(f"{CLASS_PREFIX[k]}{i}", u) for k, u in (("good", "ms"), ("degraded", "ms"), ("bad", "percent")) for i in range(1, len(PAIRS) + 1)]
                  + [(f"t{bid}{i}", "bps") for bid, *_ in TRAFFIC_BANDS for i in range(1, len(PAIRS) + 1)] + [("ML", "ms"), ("MT", "bps"), ("S", "none")])


def value_names():
    """Give each query's number field a readable name (also what the midpoint labels read).
    Grafana names a query's number field "Value #<refId>" when the panel has several queries, so that is what we rename."""
    names = {}
    for k, n in (("good", "Latency"), ("degraded", "Latency"), ("bad", "Packet loss")):
        for i in range(1, len(PAIRS) + 1):
            names[f"{CLASS_PREFIX[k]}{i}"] = n
    for bid, *_ in TRAFFIC_BANDS:
        for i in range(1, len(PAIRS) + 1):
            names[f"t{bid}{i}"] = "Traffic (both directions)"
    names.update({"ML": "Latency", "MT": "Traffic", "S": "Open issues"})
    renames = [{"id": "organize", "filter": {"id": "byRefId", "options": ref}, "options": {"renameByName": {f"Value #{ref}": n, "Value": n}}} for ref, n in names.items()]
    return renames


def geomap():
    layers = [
        states_layer(),
        *ribbon_layers(),
        *link_layers(),
        label_layer("Latency labels", "ML", "#F8FAFC", -13, "Latency"),
        label_layer("Traffic labels", "MT", "#93C5FD", 13, "Traffic"),
        {"type": "markers", "name": "Sites", "tooltip": True, "filterData": {"id": "byRefId", "options": "S"},
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
        "targets": link_targets() + traffic_targets() + label_targets() + [tgt(site_query(), "S")],
        "transformations": value_names() + [
            {"id": "convertFieldType", "options": {"conversions": [{"targetField": "lat", "destinationType": "number"}, {"targetField": "lon", "destinationType": "number"}], "fields": {}}},
        ],
        "fieldConfig": {
            "defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": RED, "value": 3}]},
                         "color": {"mode": "thresholds"}},
            "overrides": hide("Time", "lat", "lon", "end", "site") + [
                {"matcher": {"id": "byFrameRefID", "options": ref}, "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": 0}]}
                for ref, u in (OVERRIDE_UNITS)]},
        "options": {
            "view": {"id": "coords", "lat": 38.6, "lon": -96.5, "zoom": 4, "allLayers": True},
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showAttribution": True, "showScale": False, "showMeasure": False, "showDebug": False},
            "basemap": {"type": "xyz", "name": "Blank", "config": {"url": BLANK_TILE, "attribution": "US outline: Grafana bundled data"}},
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


def _view(a, b):
    """Traffic for a link is read on the remote-site host (each remote site is one node): for a home pair that is the remote end,
    for two remote sites it is the first of them. Returns (viewing site, other site)."""
    return (b, a) if a == "pdx" else (a, b)


def _sel(metric, v, o):
    return f'{metric}{{site="{v}", to_site="{o}"}}'


def traffic_exprs():
    """PromQL per pair, as dicts of expression strings (each an unlabelled vector)."""
    out = []
    for a, b in PAIRS:
        v, o = _view(a, b)
        rx, tx = "rl_tailnet_peer_rx_bytes_total", "rl_tailnet_peer_tx_bytes_total"
        a_to_b, b_to_a = ((rx, tx) if v == b else (tx, rx))     # at the viewing host, rx = traffic arriving from the other site
        out.append({"link": f"{a.upper()} - {b.upper()}", "v": v, "o": o,
                    "fwd": f'(sum(rate({_sel(a_to_b, v, o)}[5m])) * 8)', "rev": f'(sum(rate({_sel(b_to_a, v, o)}[5m])) * 8)',
                    "day": f'sum(increase({_sel(rx, v, o)}[24h]) + increase({_sel(tx, v, o)}[24h]))',
                    "relayed": f'sum({_sel("rl_tailnet_peer_relayed", v, o)})'})
    return out


def _or(key):
    return " or ".join(lab(t[key], link=t["link"]) for t in traffic_exprs())


def traffic_panels():
    q = lambda e, ref: {"datasource": DS, "expr": e, "refId": ref, "instant": True, "format": "table"}
    th = lambda *p: {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in p]}
    ov = lambda col, props: {"matcher": {"id": "byName", "options": col}, "properties": props}
    both = " or ".join(lab(f'({t["fwd"]} + {t["rev"]})', link=t["link"]) for t in traffic_exprs())
    series = {"id": 4, "type": "timeseries", "title": "Traffic between sites (both directions, tailnet)", "datasource": DS, "gridPos": {"x": 0, "y": 26, "w": 11, "h": 9},
              "description": "Bits per second carried over the tailnet between each pair of sites (sum of every host at each end). Includes backups, DNS sync, PBX trunks, monitoring. Internet traffic is not included.",
              "targets": [{"datasource": DS, "expr": both, "refId": "A", "legendFormat": "{{link}}", "range": True, "instant": False}],
              "fieldConfig": {"defaults": {"unit": "bps", "min": 0, "custom": {"lineWidth": 1, "fillOpacity": 12, "showPoints": "never", "stacking": {"mode": "none"}}}, "overrides": []},
              "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}}}
    tbl = {"id": 5, "type": "table", "title": "Traffic per link (now / last 24 h)", "datasource": DS, "gridPos": {"x": 11, "y": 26, "w": 13, "h": 9},
           "description": "Rates are 5-minute averages. First -> second follows the link name (PDX - PCB: PDX to PCB). Relayed = hosts reachable only through a DERP relay (slower path).",
           "targets": [q(_or("fwd"), "A"), q(_or("rev"), "B"), q(_or("day"), "C"), q(_or("relayed"), "D")],
           "transformations": [{"id": "merge", "options": {}},
                               {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                              "renameByName": {"link": "Link", "Value #A": "First to second", "Value #B": "Second to first", "Value #C": "Last 24 h", "Value #D": "Relayed hosts"},
                                                              "indexByName": {"link": 0, "Value #A": 1, "Value #B": 2, "Value #C": 3, "Value #D": 4}}}],
           "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                           "overrides": [ov("First to second", [{"id": "unit", "value": "bps"}, {"id": "decimals", "value": 1}]),
                                         ov("Second to first", [{"id": "unit", "value": "bps"}, {"id": "decimals", "value": 1}]),
                                         ov("Last 24 h", [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 1}]),
                                         ov("Relayed hosts", [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}, {"id": "thresholds", "value": th((None, "green"), (1, "orange"))}])]},
           "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Last 24 h", "desc": True}]}}
    talkers = {"id": 6, "type": "table", "title": "Busiest hosts on the links (last 24 h)", "datasource": DS, "gridPos": {"x": 0, "y": 35, "w": 24, "h": 8},
               "description": "Which host is moving the data. 'Measured at' is the remote-site machine; 'Other end' is the machine it talks to (home hosts are shown by name). Counters come from Tailscale's per-peer accounting.",
               "targets": [q('topk(12, sum by (site, to_site, peer) (increase(rl_tailnet_peer_rx_bytes_total[24h]) + increase(rl_tailnet_peer_tx_bytes_total[24h])))', "A"),
                           q('sum by (site, to_site, peer) (increase(rl_tailnet_peer_rx_bytes_total[24h]))', "B"), q('sum by (site, to_site, peer) (increase(rl_tailnet_peer_tx_bytes_total[24h]))', "C")],
               "transformations": [{"id": "merge", "options": {}},
                                   {"id": "filterByValue", "options": {"type": "include", "match": "any", "filters": [{"fieldName": "Value #A", "config": {"id": "greater", "options": {"value": 0}}}]}},
                                   {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                                  "renameByName": {"site": "Measured at", "to_site": "Other site", "peer": "Other end", "Value #A": "Total", "Value #B": "Received", "Value #C": "Sent"},
                                                                  "indexByName": {"site": 0, "to_site": 1, "peer": 2, "Value #A": 3, "Value #B": 4, "Value #C": 5}}}],
               "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                               "overrides": [ov(c, [{"id": "unit", "value": "bytes"}, {"id": "decimals", "value": 1}]) for c in ("Total", "Received", "Sent")]},
               "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Total", "desc": True}]}}
    return [series, tbl, talkers]


def legend():
    md = ("### How to read the map\n"
          "**Lines** are the tailnet links between sites, coloured by health (worst of the two directions):\n\n"
          "- <span style='color:#22C55E'>**green**</span> normal\n"
          "- <span style='color:#FFA726'>**orange**</span> loss above 2%, or latency more than twice its own 1-day norm (both 5-minute averages)\n"
          "- <span style='color:#EF4444'>**red**</span> loss above 10%, or the link is down\n\n"
          "**Markers** are the sites: green = nothing wrong there, orange = failing checks or firing alerts at that site.\n\n"
          "The **wide translucent blue band** under each line is how much traffic the link is carrying right now (both directions, 5-minute average): thin = quiet (under 5 kbit/s), medium = moderate (5 to 25 kbit/s), wide = busy (over 25 kbit/s). The numbers at the middle of each link are its latency (white) and traffic (blue).\n\n"
          "Hover a line or marker for numbers. Latency is compared with each link's *own* normal, so a long link (Portland to Florida, ~90 ms) is not flagged for being long.\n\n"
          "Underlying data: ping mesh (10 pings a minute from every site), plus the Network dashboard for the full matrix.")
    return {"id": 3, "type": "text", "title": "", "gridPos": {"x": 15, "y": 17, "w": 9, "h": 9}, "options": {"mode": "markdown", "content": md}}


NAV = [("rl-overview", "Overview"), ("rl-map", "Site map"), ("rl-network", "Network"), ("rl-servers", "Servers & storage"), ("rl-services", "Services")]


def nav_for(uid):
    return [{"title": t, "type": "link", "url": f"/d/{u}", "icon": "dashboard", "targetBlank": False} for u, t in NAV if u != uid]


def main():
    d = {"uid": "rl-map", "title": "Site map", "tags": ["rainbowlink", "network"], "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
         "graphTooltip": 1, "refresh": "1m", "time": {"from": "now-6h", "to": "now"}, "links": nav_for("rl-map"),
         "panels": [geomap(), link_table(), legend(), *traffic_panels()], "templating": {"list": []}, "annotations": {"list": []}}
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
