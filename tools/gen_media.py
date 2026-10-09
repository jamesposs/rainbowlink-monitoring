#!/usr/bin/env python3
"""Generate grafana/dashboards/media.json: the media-automation board (request portal, Radarr/Sonarr/Prowlarr, qBittorrent behind Mullvad).
Run, then gen_map.py (keeps navigation links in sync), then apply.sh."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen_map import DS, OUT, nav_for, tgt   # noqa: E402

GREEN, ORANGE, RED, GREY = "#22C55E", "#FFA726", "#EF4444", "#9CA3AF"
_id = [0]

def pid():
    _id[0] += 1
    return _id[0]

def thresholds(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}

def row(title, y):
    return {"id": pid(), "type": "row", "title": title, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False, "panels": []}

def stat(title, expr, x, y, w=4, h=4, mappings=None, th=None, unit=None, desc="", nodata="No data"):
    d = {"mappings": mappings or [], "color": {"mode": "thresholds"}, "thresholds": th or thresholds((GREEN, None)), "noValue": nodata}
    if unit: d["unit"] = unit
    return {"id": pid(), "type": "stat", "title": title, "description": desc, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{**tgt(expr, "A"), "format": "time_series"}],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "textMode": "value", "justifyMode": "center",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}

def ts(title, exprs, x, y, w=8, h=7, unit=None, desc="", mn=None):
    d = {"custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never", "stacking": {"mode": "none"}}}
    if unit: d["unit"] = unit
    if mn is not None: d["min"] = mn
    return {"id": pid(), "type": "timeseries", "title": title, "description": desc, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{**tgt(e, chr(65 + i)), "range": True, "instant": False, "legendFormat": lg} for i, (e, lg) in enumerate(exprs)],
            "fieldConfig": {"defaults": d, "overrides": []}, "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}

def table(title, exprs, x, y, w=12, h=8, desc="", rename=None, hide=None):
    tr = [{"id": "merge", "options": {}}]
    if hide: tr.append({"id": "organize", "options": {"excludeByName": {k: True for k in hide}, "renameByName": rename or {}}})
    return {"id": pid(), "type": "table", "title": title, "description": desc, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [{**tgt(e, chr(65 + i)), "instant": True, "range": False, "format": "table"} for i, e in enumerate(exprs)],
            "transformations": tr, "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True}}

VPN_MAP = [{"type": "value", "options": {"0": {"text": "HEALTHY", "color": GREEN, "index": 0}, "1": {"text": "DEGRADED", "color": ORANGE, "index": 1}, "2": {"text": "CRITICAL", "color": RED, "index": 2}}}]
YESNO = lambda good_is_one=True: [{"type": "value", "options": {"1": {"text": "Yes", "color": GREEN if good_is_one else RED, "index": 0}, "0": {"text": "No", "color": RED if good_is_one else GREEN, "index": 1}, "-1": {"text": "Unknown", "color": GREY, "index": 2}}}]
UPDOWN = [{"type": "value", "options": {"1": {"text": "Up", "color": GREEN, "index": 0}, "0": {"text": "Down", "color": RED, "index": 1}}}]
TH_VPN = thresholds((GREEN, None), (ORANGE, 1), (RED, 2))
TH_UP = thresholds((RED, None), (GREEN, 1))

def panels():
    P = []
    P.append(row("Download client & Mullvad VPN", 0))
    P.append(stat("VPN protection", "max(rl_vpn_state_code)", 0, 1, 5, mappings=VPN_MAP, th=TH_VPN,
                  desc="HEALTHY: tunnel up, egress verified as a Mullvad exit, DNS via Mullvad, kill switch proven. DEGRADED: nothing can leak but the tunnel/provider is not working (downloads paused). CRITICAL: possible non-Mullvad path - qBittorrent is stopped and an alert is paged.", nodata="No data"))
    P.append(stat("Egress is Mullvad", "max(rl_vpn_egress_mullvad)", 5, 1, 3, mappings=YESNO(), th=thresholds((RED, None), (GREEN, 1))))
    P.append(stat("Kill switch proven", "max(rl_vpn_killswitch_effective)", 8, 1, 3, mappings=YESNO(), th=thresholds((RED, None), (GREEN, 1)),
                  desc="nftables default-drop present AND direct (non-tunnel) IPv4, IPv6 and LAN-DNS probes were all blocked on the last check."))
    P.append(stat("DNS via Mullvad", "max(rl_vpn_dns_ok)", 11, 1, 3, mappings=YESNO(), th=thresholds((RED, None), (GREEN, 1))))
    P.append(stat("qBittorrent", "max(rl_vpn_qbit_running)", 14, 1, 3, mappings=UPDOWN, th=TH_UP))
    P.append(stat("Handshake age", "max(rl_vpn_handshake_age_seconds)", 17, 1, 3, unit="s", th=thresholds((GREEN, None), (ORANGE, 150), (RED, 600)),
                  desc="-1 means the tunnel has never completed a handshake since boot."))
    P.append(stat("Relay failovers (24h)", "max(increase(rl_vpn_failovers_total[24h]))", 20, 1, 4, th=thresholds((GREEN, None), (ORANGE, 2)), desc="The watchdog rotates to the next Mullvad relay when the handshake goes stale."))
    P.append(ts("VPN state", [("max(rl_vpn_state_code)", "0 healthy / 1 degraded / 2 critical")], 0, 5, 8, 7, mn=0, desc="Anything above 0 means downloads are paused or the client was stopped."))
    P.append(ts("Download / upload speed", [("max(rl_vpn_qbit_download_bytes_per_second)", "download"), ("max(rl_vpn_qbit_upload_bytes_per_second)", "upload")], 8, 5, 8, 7, unit="Bps"))
    P.append(ts("Tunnel traffic", [("rate(rl_vpn_rx_bytes[5m])", "received"), ("rate(rl_vpn_tx_bytes[5m])", "sent")], 16, 5, 8, 7, unit="Bps"))
    P.append(table("Relay and torrents", ["max by (relay, exit) (rl_vpn_relay_info)", "sum by (state) (rl_vpn_qbit_torrents)"], 0, 12, 12, 6, hide=["Time", "Value #A"]))
    P.append(table("Host", ["max by (host) (up{job=\"node\", host=~\"dl|arr\"})", "max by (host) (1 - node_memory_MemAvailable_bytes{host=~\"dl|arr\"} / node_memory_MemTotal_bytes{host=~\"dl|arr\"})",
                            "max by (host) (1 - node_filesystem_avail_bytes{host=~\"dl|arr\", mountpoint=\"/\"} / node_filesystem_size_bytes{host=~\"dl|arr\", mountpoint=\"/\"})", "max by (host) (time() - node_boot_time_seconds{host=~\"dl|arr\"})"],
                   12, 12, 12, 6, rename={"Value #A": "Up", "Value #B": "Memory used", "Value #C": "Disk used", "Value #D": "Uptime (s)"}, hide=["Time"]))
    P.append(row("Request portal & automation", 18))
    P.append(stat("Radarr", "max(up{job=\"arr\", app=\"radarr\"})", 0, 19, 4, mappings=UPDOWN, th=TH_UP))
    P.append(stat("Sonarr", "max(up{job=\"arr\", app=\"sonarr\"})", 4, 19, 4, mappings=UPDOWN, th=TH_UP))
    P.append(stat("Prowlarr", "max(up{job=\"arr\", app=\"prowlarr\"})", 8, 19, 4, mappings=UPDOWN, th=TH_UP))
    P.append(stat("Request portal (web)", "max(probe_success{service=\"Request portal\"})", 12, 19, 4, mappings=UPDOWN, th=TH_UP, desc="Probe of the nginx front door (login redirect to Rainbowlink SSO)."))
    P.append(stat("Indexers working", "max(prowlarr_indexer_enabled_total)", 16, 19, 4, th=thresholds((RED, None), (ORANGE, 1), (GREEN, 2)), desc="Prowlarr disables indexers that keep failing."))
    P.append(stat("Health errors", "sum(({__name__=~\"(radarr|sonarr|prowlarr)_system_health_issues\", type=\"error\"} > bool 0)) or vector(0)", 20, 19, 4, th=thresholds((GREEN, None), (RED, 1)), desc="Errors reported by Radarr/Sonarr/Prowlarr's own health checks."))
    P.append(ts("Download queue", [("max(radarr_queue_total)", "Radarr"), ("max(sonarr_queue_total)", "Sonarr")], 0, 23, 8, 7, desc="Items currently downloading or waiting to import."))
    P.append(ts("Library size", [("max(radarr_movie_total)", "Movies"), ("max(sonarr_series_total)", "Series")], 8, 23, 8, 7))
    P.append(ts("Media share free space", [("min(radarr_rootfolder_freespace_bytes)", "free")], 16, 23, 8, 7, unit="bytes", desc="Free space on the NAS volume that holds Movies, TV Shows and Downloads."))
    P.append(table("Indexers", ["max by (indexer) (prowlarr_indexer_average_response_time_ms)", "max by (indexer) (increase(prowlarr_indexer_failed_queries_total[24h]))", "max by (indexer) (increase(prowlarr_indexer_queries_total[24h]))"],
                   0, 30, 12, 7, rename={"Value #A": "Avg response (ms)", "Value #B": "Failed queries (24h)", "Value #C": "Queries (24h)"}, hide=["Time"]))
    P.append(table("Health messages", ["max by (source, type, message) (radarr_system_health_issues)", "max by (source, type, message) (sonarr_system_health_issues)", "max by (source, type, message) (prowlarr_system_health_issues)"],
                   12, 30, 12, 7, hide=["Time", "Value #A", "Value #B", "Value #C"]))
    return P

def main():
    d = {"uid": "rl-media", "title": "Media automation", "tags": ["rainbowlink", "media"], "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
         "graphTooltip": 1, "refresh": "30s", "time": {"from": "now-6h", "to": "now"}, "links": nav_for("rl-media"),
         "panels": panels(), "templating": {"list": []}, "annotations": {"list": []}}
    with open(os.path.join(OUT, "media.json"), "w") as f:
        json.dump(d, f, indent=1)
    print("wrote media.json")

if __name__ == "__main__":
    main()
