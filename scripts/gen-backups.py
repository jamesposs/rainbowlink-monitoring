#!/usr/bin/env python3
"""Generate the Backups dashboard and its status recording rules from ONE catalog of backup jobs.

  python3 scripts/gen-backups.py            (run from /opt/monitoring; rewrites the two files below)

  prometheus/rules/45-backup-status.yml     rl:backup_job_age_seconds / rl:backup_job_age_ratio, one series per job in CATALOG
  grafana/dashboards/backups.json           the "Backups" page (status table, coverage, per-guest freshness, alerts, space)

To add a backup job: add one CATALOG entry (and, for files another system writes, one line in the Ansible host_vars
monitoring_backup_artifacts), re-run, apply. The same entry drives the status table and the status tiles, so the page cannot
say "covered" for something nothing is watching.
"""
import copy, json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SENTINEL = "9e9"          # "no data" age: far larger than any real age, shown as NO DATA

def pve(storage):
    return (f'max((time() - rl_pve_backup_last_success_timestamp_seconds{{storage="{storage}"}}) '
            f'and on(vmid, storage) rl_pve_backup_expected == 1)',
            f'max(rl_pve_backup_max_age_seconds{{storage="{storage}"}})')

def hyper(repo, limit_seconds):
    return (f'time() - max(last_over_time(rl_hyperbackup_last_commit_timestamp_seconds{{repo="{repo}"}}[30d]))',
            f"vector({limit_seconds})")

def artifact(name):
    return (f'time() - max(rl_backup_artifact_last_write_timestamp_seconds{{artifact="{name}"}})',
            f'max(rl_backup_artifact_max_age_seconds{{artifact="{name}"}})')

# (job, what it protects, where the copy lives, schedule and retention, (age expr, allowed-age expr))
CATALOG = [
    ("Proxmox guests -> NAS",
     "Every VM and container except the ones listed under 'not covered'",
     "NAS share Backups/proxmox (nas-pdx)", "Nightly 20:00 - keeps 3 daily + 3 weekly", pve("nas-pdx")),
    ("Proxmox guests -> off-site",
     "The same guests, except Home Assistant's VM, ChannelsDVR and dns2",
     "pi-pcb disk (Panama City Beach)", "1st and 15th, 00:00 - keeps the last 2", pve("pi-pcb")),
    ("NAS folders -> off-site (nas_hq)",
     "MemoryVault (photos, videos), the Backups share, Media: Home Videos and Books only",
     "pi-pcb: plain folder copy, no old versions, not encrypted", "Hyper Backup, daily 06:00", hyper("nas_hq", 93600)),
    ("NAS folders -> off-site (nas_hq_set2)",
     "Poss (Paperless documents) and the home folders",
     "pi-pcb: versioned, not encrypted", "Hyper Backup, daily 09:30 - custom retention, integrity check Saturdays", hyper("nas_hq_set2", 93600)),
    ("Home Assistant -> NAS",
     "Home Assistant settings, automations, add-ons (its own backup)",
     "NAS share Backups/habackup", "Nightly about 22:10 - about 14 kept", artifact("ha-native-nas")),
    ("Home Assistant -> off-site",
     "The same Home Assistant backup, second copy",
     "pi-pcb disk, folder homeassistant", "Nightly about 22:30", artifact("ha-native-offsite")),
    ("FreePBX (pbx2) -> NAS",
     "FreePBX phone-system settings on pbx2",
     "NAS share Backups/pbx/pbx2/Daily, then off-site by nas_hq", "Nightly about 03:15", artifact("freepbx-pbx2-nas")),
    ("ChannelsDVR -> NAS",
     "ChannelsDVR settings and database",
     "NAS share Backups/channelsdvr, then off-site by nas_hq", "Nightly about 21:00", artifact("channelsdvr-nas")),
    ("Paperless export",
     "Paperless database and document export (restore aid)",
     "NAS share Poss/paperless/export, off-site with Poss", "Weekly, Sundays 03:00 (systemd timer in the Paperless container) - flagged after 9 days", artifact("paperless-export")),
    ("Node pi-pcb -> NAS",
     "pi-pcb settings, keys and state (age-encrypted); card image monthly from Nov 1",
     "NAS share Backups/pi-pcb", "Nightly 03:10 - keeps 7 daily, 4 weekly, 6 monthly", artifact("rl-backup-pi-pcb-nas")),
    ("Node pi-pdx -> NAS",
     "pi-pdx settings, DNS and router config (age-encrypted)",
     "NAS share Backups/pi-pdx", "Nightly 03:55 - keeps 7 daily, 4 weekly, 6 monthly", artifact("rl-backup-pi-pdx-nas")),
    ("Node vps-sjc -> NAS",
     "Headscale server keys and database (age-encrypted)",
     "NAS share Backups/vps-sjc", "Nightly 03:25 - keeps 7 daily, 4 weekly, 6 monthly", artifact("rl-backup-vps-sjc-nas")),
    ("Node pbx2 (vps-bos) -> NAS",
     "pbx2 extras not in the FreePBX backup (age-encrypted)",
     "NAS share Backups/pbx2", "Nightly 03:40 - keeps 7 daily, 4 weekly, 6 monthly", artifact("rl-backup-pbx2-nas")),
]

# ---------------------------------------------------------------- recording rules
def labelled(expr, i, job, protects, stored, schedule):
    out = expr
    for k, v in (("Order", f"{i:02d}"), ("Job", job), ("Protects", protects), ("Stored_at", stored), ("Schedule", schedule)):
        assert '"' not in v and "\\" not in v, v
        out = f'label_replace({out}, "{k}", "{v}", "", "")'
    return out

def rules_yaml():
    ages, ratios = [], []
    for i, (job, protects, stored, schedule, (age, limit)) in enumerate(CATALOG, 1):
        a = f"(({age}) or vector({SENTINEL}))"
        l = f"(({limit}) or vector(1))"
        ages.append(labelled(a, i, job, protects, stored, schedule))
        ratios.append(labelled(f"({a} / {l})", i, job, protects, stored, schedule))
    def block(name, parts):
        body = "\n            or\n".join("            " + p for p in parts)
        return f"      - record: {name}\n        expr: |\n{body}\n"
    return ("# GENERATED by scripts/gen-backups.py from its CATALOG. Do not edit by hand; change the catalog and re-run.\n"
            "# One series per backup job. age = seconds since the last good copy (9e9 = nothing reported); ratio = age / allowed age\n"
            "# (below 0.7 OK, 0.7-1 AGING, 1 or more OVERDUE). The Backups dashboard and the Overview tile read these.\n"
            "groups:\n  - name: backup_status\n    rules:\n"
            + block("rl:backup_job_age_seconds", ages) + block("rl:backup_job_age_ratio", ratios))

# ---------------------------------------------------------------- dashboard
DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def tgt(expr, ref="A", legend=None, table=False, instant=True):
    t = {"datasource": DS, "expr": expr, "refId": ref, "instant": instant, "range": not instant}
    if table:
        t["format"] = "table"
    if legend:
        t["legendFormat"] = legend
    return t

def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "collapsed": False, "panels": []}

def text(title, md, x, y, w, h):
    return {"id": nid(), "type": "text", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": md}}

def stat(title, expr, x, y, w, h, unit="none", mappings=None, steps=None, desc="", link=None):
    steps = steps or [{"color": "green", "value": None}, {"color": "red", "value": 1}]
    p = {"id": nid(), "type": "stat", "title": title, "description": desc, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "datasource": DS, "targets": [tgt(expr)],
         "fieldConfig": {"defaults": {"unit": unit, "mappings": mappings or [], "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
         "options": {"colorMode": "background", "graphMode": "none", "textMode": "value", "justifyMode": "center",
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}
    return p

def gauge(title, expr, x, y, w, h, desc=""):
    return {"id": nid(), "type": "gauge", "title": title, "description": desc, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "datasource": DS, "targets": [tgt(expr)],
            "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "decimals": 0,
                                         "thresholds": {"mode": "absolute", "steps": [
                                             {"color": "green", "value": None}, {"color": "orange", "value": 0.8}, {"color": "red", "value": 0.9}]}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showThresholdMarkers": True}}

def rng(frm, to, text_, color, idx):
    return {"type": "range", "options": {"from": frm, "to": to, "result": {"text": text_, "color": color, "index": idx}}}

def build():
    panels = []
    # ---- header + tiles
    panels.append(text("How to read this page",
        "**Every backup job in one table: what it protects, where the copy lives, and how fresh it is.** "
        "Status is the age of the newest good copy against what that job is allowed: "
        "**OK** (under 70%) · **AGING** (70-100%) · **OVERDUE** (past its allowed age, an alert is raised) · **NO DATA** (nothing reported at all). "
        "A failed or overdue backup sends a phone notice between 07:00 and 22:00 and nothing overnight (the 08:00 summary lists it too). "
        "Below the table: what is covered by what, which guests are in no job, the live alerts, and space on the backup targets.",
        0, 0, 24, 3))
    y = 3
    problems = 'count(rl:backup_job_age_ratio >= 1) or vector(0)'
    panels.append(stat("Backup status", problems, 0, y, 4, 4, unit="suffix: overdue",
        mappings=[{"type": "value", "options": {"0": {"text": "ALL OK", "color": "green", "index": 0}}}],
        desc="Jobs past their allowed age or with nothing reported. Zero = every tracked job is within its limit."))
    panels.append(stat("Jobs tracked", "count(rl:backup_job_age_ratio)", 4, y, 4, 4, steps=[{"color": "blue", "value": None}],
        desc="Rows in the table below. Add a job by adding a CATALOG entry in scripts/gen-backups.py."))
    panels.append(stat("Backup alerts firing",
        'count(ALERTS{alertname=~"(Backup|HyperBackup|NodeBackup|NodeImageBackup).*", alertstate="firing"}) or vector(0)',
        8, y, 4, 4, unit="suffix: firing", steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
        mappings=[{"type": "value", "options": {"0": {"text": "none", "color": "green", "index": 0}}}]))
    panels.append(stat("Closest to its limit", "max(rl:backup_job_age_ratio < 1000)", 12, y, 4, 4, unit="percentunit",
        steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.7}, {"color": "red", "value": 1}],
        desc="The job nearest to (or past) its own allowed age, as a share of that limit. 100% or more = overdue. Jobs with nothing reported are NO DATA in the table."))
    panels.append(gauge("NAS volume used",
        'pve_disk_usage_bytes{id="storage/pve0/nas-pdx"} / pve_disk_size_bytes{id="storage/pve0/nas-pdx"}', 16, y, 4, 4,
        desc="The NAS volume that holds the first copy of everything (and all Proxmox dumps)."))
    panels.append(gauge("pi-pcb disk used",
        'pve_disk_usage_bytes{id="storage/pve0/pi-pcb"} / pve_disk_size_bytes{id="storage/pve0/pi-pcb"}', 20, y, 4, 4,
        desc="The single off-site disk in Panama City Beach."))
    y += 4

    # ---- the table
    panels.append(row("Every backup job", y)); y += 1
    tbl = {"id": nid(), "type": "table", "title": "Backup jobs: status and coverage",
           "description": "One row per backup job. Source of truth: scripts/gen-backups.py (CATALOG).",
           "gridPos": {"x": 0, "y": y, "w": 24, "h": 14}, "datasource": DS,
           "targets": [tgt("max by (Order, Job, Protects, Stored_at, Schedule) (rl:backup_job_age_seconds)", "A", table=True),
                       tgt("max by (Order, Job, Protects, Stored_at, Schedule) (rl:backup_job_age_ratio)", "B", table=True)],
           "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True}},
             "overrides": [
               {"matcher": {"id": "byName", "options": "Status"}, "properties": [
                   {"id": "mappings", "value": [rng(0, 0.7, "OK", "green", 0), rng(0.7, 1, "AGING", "orange", 1),
                                                rng(1, 1000, "OVERDUE", "red", 2), rng(1000, 1e15, "NO DATA", "purple", 3)]},
                   {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                   {"id": "custom.width", "value": 110}]},
               {"matcher": {"id": "byName", "options": "Last good copy (ago)"}, "properties": [
                   {"id": "unit", "value": "s"},
                   {"id": "mappings", "value": [rng(5e9, 1e15, "nothing reported", "purple", 0)]},
                   {"id": "custom.width", "value": 150}]},
               {"matcher": {"id": "byName", "options": "Job"}, "properties": [{"id": "custom.width", "value": 230}]},
               {"matcher": {"id": "byName", "options": "What it protects"}, "properties": [{"id": "custom.width", "value": 380}]},
               {"matcher": {"id": "byName", "options": "Where the copy lives"}, "properties": [{"id": "custom.width", "value": 330}]},
             ]},
           "options": {"showHeader": True, "cellHeight": "sm"},
           "transformations": [
               {"id": "merge", "options": {}},
               {"id": "sortBy", "options": {"sort": [{"field": "Order"}]}},
               {"id": "organize", "options": {
                   "excludeByName": {"Time": True, "Order": True},
                   "renameByName": {"Value #A": "Last good copy (ago)", "Value #B": "Status", "Protects": "What it protects",
                                    "Stored_at": "Where the copy lives", "Schedule": "Schedule and retention"},
                   "indexByName": {"Value #B": 0, "Job": 1, "Protects": 2, "Stored_at": 3, "Schedule": 4, "Value #A": 5}}}]}
    panels.append(tbl); y += 14

    panels.append({"id": nid(), "type": "timeseries", "title": "How old each backup is, against what it is allowed (1.0 = overdue)",
        "description": "Each line is a job. A healthy job saw-tooths up toward 1 and drops back after each run. A line that keeps climbing past 1 is a failing job.",
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 8}, "datasource": DS,
        "targets": [tgt('rl:backup_job_age_ratio < 1000', "A", legend="{{Job}}", instant=False)],
        "fieldConfig": {"defaults": {"unit": "none", "min": 0, "max": 2, "custom": {"lineWidth": 1, "fillOpacity": 0, "thresholdsStyle": {"mode": "line"}},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]}}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]}, "tooltip": {"mode": "multi", "sort": "desc"}}})
    y += 8

    # ---- coverage
    panels.append(row("What is covered by what", y)); y += 1
    panels.append(text("Coverage map",
        "| What | Copy at home | Copy off-site (pi-pcb) | Notes |\n|---|---|---|---|\n"
        "| **VMs and containers** | NAS dumps, nightly (3 daily + 3 weekly) | Proxmox dumps, 1st and 15th (last 2) | Guests in no job are listed on the right |\n"
        "| **Home Assistant** | Its own backup to the NAS, nightly | Same backup copied to pi-pcb | The VM is also in the nightly NAS dump (not off-site) |\n"
        "| **Paperless documents, home folders** | NAS shares *Poss*, *homes* | Hyper Backup *nas_hq_set2*, daily, versioned | Not encrypted |\n"
        "| **Paperless export** | NAS *Poss/paperless/export* | Included with *Poss* | Weekly, Sundays 03:00. Holds settings and tokens in plaintext |\n"
        "| **MemoryVault** (family photos, videos) | NAS | Hyper Backup *nas_hq*, daily, plain copy | No old versions |\n"
        "| **Home Videos, Books** | NAS *Media* | Hyper Backup *nas_hq* | Rest of *Media* is not copied |\n"
        "| **FreePBX (pbx2), ChannelsDVR** | NAS *Backups* share, nightly | Copied by *nas_hq* with the *Backups* share | |\n"
        "| **Remote nodes** (pi-pcb, pi-pdx, vps-sjc, pbx2) | NAS *Backups/<node>*, nightly, encrypted | *nas_hq* copies the *Backups* share | Folders are new: first off-site copy not yet confirmed |\n"
        "| **Not backed up** | | | *Movies*, *TV Shows*, *Other* (about 7.6 TB, replaceable) · shares *Public*, *Surveillance*, *Time Machine*, *Encypted* (in neither Hyper Backup task) |\n\n"
        "Both off-site copies live on one disk at one site. Nothing is in the cloud, and nothing is immutable.",
        0, y, 16, 12))
    base = ('label_replace(pve_guest_info{template="0"} * on(id) group_left() pve_up, "vmid", "$1", "id", ".*/(.*)") '
            'unless on(vmid) rl_pve_backup_expected{storage="%s"}')
    for k, (title, storage, yy, hh, desc) in enumerate([
            ("Guests in NO nightly NAS job", "nas-pdx", y, 6, "Running ones are a risk: nothing is copying them. Stopped ones may be intentional."),
            ("Guests NOT copied off-site", "pi-pcb", y + 6, 6, "These exist only at home (plus the nightly NAS dump if they are in that job).")]):
        panels.append({"id": nid(), "type": "table", "title": title, "description": desc,
            "gridPos": {"x": 16, "y": yy, "w": 8, "h": hh}, "datasource": DS,
            "targets": [tgt(base % storage, "A", table=True)],
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
              "overrides": [{"matcher": {"id": "byName", "options": "State"}, "properties": [
                  {"id": "mappings", "value": [{"type": "value", "options": {"1": {"text": "running", "color": "red", "index": 0},
                                                                              "0": {"text": "stopped", "color": "text", "index": 1}}}]},
                  {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]},
            "options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "State", "desc": True}]},
            "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "host": True, "id": True, "node": True,
                                    "site": True, "template": True, "tags": True, "instance": True, "job": True, "critical": True},
                                  "renameByName": {"name": "Guest", "vmid": "ID", "type": "Type", "Value": "State"},
                                  "indexByName": {"name": 0, "vmid": 1, "type": 2, "Value": 3}}}]})
    y += 12

    # ---- per guest (moved from Servers & storage)
    panels.append(row("Proxmox guests", y)); y += 1
    return panels, y

def move_from_servers(servers, y):
    """Copy the backup panels that used to live on Servers & storage (freshness table, last run, unprotected, age by target, HB version)."""
    by = {p["id"]: p for p in servers["panels"]}
    out = []
    layout = {19: (0, 0, 14, 11), 20: (14, 0, 5, 4), 21: (19, 0, 5, 4), 22: (14, 4, 10, 7), 24: (14, 11, 5, 4)}
    for old, (x, dy, w, h) in layout.items():
        p = copy.deepcopy(by[old])
        p["id"] = nid()
        p["gridPos"] = {"x": x, "y": y + dy, "w": w, "h": h}
        if old == 19:
            p["description"] = "Allowed age: 30 h for the nightly NAS job, 17 days for the twice-monthly off-site copy. Red = overdue."
        out.append(p)
    return out, y + 15

def tail(y):
    panels = []
    panels.append(row("Alerts and notifications", y)); y += 1
    panels.append({"id": nid(), "type": "table", "title": "Backup alerts right now",
        "description": "Empty = nothing wrong. Pending = noticed, waiting out a short delay before it is raised.",
        "gridPos": {"x": 0, "y": y, "w": 12, "h": 8}, "datasource": DS,
        "targets": [tgt('ALERTS{alertname=~"(Backup|HyperBackup|NodeBackup|NodeImageBackup).*", alertstate=~"firing|pending"}', "A", table=True)],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}}, "overrides": [
            {"matcher": {"id": "byName", "options": "State"}, "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
              {"id": "mappings", "value": [{"type": "value", "options": {"firing": {"color": "red", "index": 0}, "pending": {"color": "orange", "index": 1}}}]}]}]},
        "options": {"showHeader": True, "cellHeight": "sm"},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value": True, "__name__": True, "severity": True, "site": True, "job": True, "instance": True, "alertgroup": True},
            "renameByName": {"alertname": "Alert", "alertstate": "State", "host": "Host", "repo": "Task", "artifact": "Backup file set",
                             "storage": "Target", "name": "Guest", "backup": "Kind"},
            "indexByName": {"alertname": 0, "alertstate": 1}}}]})
    panels.append(text("When you hear about it",
        "A failed or overdue backup sends a phone notice between **07:00 and 22:00**, repeated every 6 hours until fixed. Nothing overnight: "
        "a problem found at night goes out when the window opens at 07:00 (and is in the 08:00 summary).\n\n"
        "| Alert | Raised when |\n|---|---|\n"
        "| *BackupJobFailed* | the last Proxmox backup run reported errors (about 10 minutes after the run) |\n"
        "| *BackupMissing / NeverRan* | a guest has no good copy within the job's allowed age (30 h NAS, 17 d off-site) |\n"
        "| *HyperBackupStale* | no Hyper Backup commit for 26 h (daily tasks) |\n"
        "| *NodeBackupFailed / Stale* | a node's nightly backup failed (10 min) or none for 27 h |\n"
        "| *BackupArtifactStale* | Home Assistant, FreePBX, ChannelsDVR, a node's file on the NAS, or the Paperless export is older than its limit |\n"
        "| *...CollectorStale* | a checker stopped reporting, so its alerts are blind |\n\n"
        "Job limits live next to each backup: Proxmox in `pve-backup-metrics.py`, files in Ansible `monitoring_backup_artifacts`.",
        12, y, 12, 8))
    y += 8
    panels.append(row("Space on the backup targets", y)); y += 1
    panels.append({"id": nid(), "type": "timeseries", "title": "Space used",
        "gridPos": {"x": 0, "y": y, "w": 12, "h": 7}, "datasource": DS,
        "targets": [tgt('pve_disk_usage_bytes{id=~"storage/pve0/(nas-pdx|pi-pcb)"} / pve_disk_size_bytes{id=~"storage/pve0/(nas-pdx|pi-pcb)"}', "A",
                        legend='{{id}}', instant=False)],
        "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "custom": {"lineWidth": 2, "fillOpacity": 5, "thresholdsStyle": {"mode": "line"}},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 0.85}]}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
    panels.append({"id": nid(), "type": "timeseries", "title": "Free space",
        "gridPos": {"x": 12, "y": y, "w": 12, "h": 7}, "datasource": DS,
        "targets": [tgt('pve_disk_size_bytes{id=~"storage/pve0/(nas-pdx|pi-pcb)"} - pve_disk_usage_bytes{id=~"storage/pve0/(nas-pdx|pi-pcb)"}', "A",
                        legend='{{id}}', instant=False)],
        "fieldConfig": {"defaults": {"unit": "bytes", "min": 0, "custom": {"lineWidth": 2, "fillOpacity": 5}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
    return panels

def dashboard(servers):
    _id[0] = 0
    panels, y = build()
    moved, y = move_from_servers(servers, y)
    panels += moved
    panels += tail(y)
    links = [{"title": t, "type": "link", "url": u, "icon": "dashboard", "targetBlank": False} for t, u in
             (("Overview", "/d/rl-overview"), ("Status", "/d/rl-status"), ("Servers & storage", "/d/rl-servers"), ("Site map", "/d/rl-map"),
              ("Network", "/d/rl-network"), ("Services", "/d/rl-services"), ("Transit", "/d/rl-transit"))]
    return {"uid": "rl-backups", "title": "Backups", "tags": ["rainbowlink", "backups"], "timezone": "browser", "schemaVersion": 39,
            "version": 1, "editable": True, "graphTooltip": 1, "refresh": "1m", "time": {"from": "now-7d", "to": "now"},
            "links": links, "templating": {"list": []}, "annotations": {"list": []}, "panels": panels}

if __name__ == "__main__":
    servers = json.load(open(os.path.join(ROOT, "grafana/dashboards/servers.json")))
    # the panels to copy are only present until they are removed from servers.json; keep a copy of them to regenerate later
    keep = os.path.join(ROOT, "scripts/backups-moved-panels.json")
    if any(p["id"] == 19 and p.get("title", "").startswith("Backup freshness") for p in servers["panels"]):
        json.dump({"panels": [p for p in servers["panels"] if p["id"] in (19, 20, 21, 22, 24)]}, open(keep, "w"))
    else:
        servers = json.load(open(keep))
    out_dash = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "grafana/dashboards/backups.json")
    out_rules = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "prometheus/rules/45-backup-status.yml")
    open(out_rules, "w").write(rules_yaml())
    json.dump(dashboard(servers), open(out_dash, "w"), indent=1)
    print("wrote", out_rules, "and", out_dash)
