#!/usr/bin/env python3
"""Backup freshness for Prometheus (node_exporter textfile collector).

Needs only a READ-ONLY Proxmox token (PVEAuditor). Listing backup files over the API would
need VM.Backup (a write-capable privilege), so instead this reads the vzdump *task logs*,
which name every guest, the storage it was written to and the outcome. Finished tasks never
change, so they are cached and each run only fetches new ones.

The set of guests that SHOULD be backed up comes from the cluster backup jobs themselves
(all=1 minus excludes, or an explicit vmid list): adding/removing guests or editing a job
needs no change here.

Emits:
  rl_pve_backup_expected{vmid,name,storage,cadence}               1 for every (guest, storage) a job should cover
  rl_pve_backup_last_success_timestamp_seconds{vmid,name,storage} when that guest last backed up OK to that storage
  rl_pve_backup_expected_since_timestamp_seconds{vmid,name,storage} when this script first saw the guest as expected on that storage
                                                                  (a NEW guest only counts as 'never backed up' after one full backup cycle)
  rl_pve_backup_max_age_seconds{vmid,name,storage,cadence}        how old the newest backup of that guest may be before alerting
                                                                  (30 h for a nightly job, 9 days weekly, 17 days 1st+15th; from the job's schedule)
  rl_pve_vzdump_last_ok{storage,status}                           outcome of the most recent backup run per storage
"""
import json, os, re, ssl, sys, time, urllib.request

API = os.environ.get("PVE_API", "https://100.64.0.14:8006/api2/json")
TOKEN_FILE = os.environ.get("PVE_TOKEN_FILE") or os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", "/opt/monitoring/secrets"), "pve_api_token")
OUT = os.environ.get("OUT", "/var/lib/prometheus/node-exporter/rl_pve_backup.prom")
STATE = os.environ.get("STATE", os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/rl-monitoring"), "pve_backup_tasks.json"))
NODE = os.environ.get("PVE_NODE", "pve0")
# How old (seconds) the newest backup of a guest may get before alerting, by how often its job runs. The cadence is read from each
# backup job's schedule, so the nightly job, a weekly job and a 1st/15th job can all write to the same storage.
MAX_AGE = {"nightly": 30 * 3600, "weekly": 9 * 86400, "semimonthly": 17 * 86400, "monthly": 35 * 86400}


def cadence(schedule):
    """nightly | weekly | semimonthly | monthly, from a Proxmox calendar event such as '20:00', 'sun 17:00' or '*-*-01,15 00:00:00'."""
    s = str(schedule).lower().strip()
    first = s.split()[0] if s.split() else ""
    days = re.findall(r"mon|tue|wed|thu|fri|sat|sun", first)
    if days:
        return "weekly" if len(set(days)) == 1 and ".." not in first else "nightly"
    m = re.match(r"\*-\*-([\d,]+)", s)
    if m:
        return "semimonthly" if "," in m.group(1) else "monthly"
    return "nightly"


HISTORY_DAYS = 45

TOKEN = open(TOKEN_FILE).read().strip()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE   # self-signed PVE cert; the path is the authenticated+encrypted tailnet


def get(path, timeout=30):
    req = urllib.request.Request(API + path, headers={"Authorization": "PVEAPIToken=" + TOKEN})
    return json.load(urllib.request.urlopen(req, timeout=timeout, context=ctx))["data"]


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


# ---- what should be backed up, from the jobs themselves -------------------------------
guests = {int(g["vmid"]): g for g in get("/cluster/resources?type=vm") if not g.get("template")}
expected = set()
pair_cadence = {}
for j in get("/cluster/backup"):
    if not j.get("enabled", 1) or not j.get("storage"):
        continue
    if j.get("all"):
        excl = {int(x) for x in str(j.get("exclude", "")).split(",") if x.strip()}
        ids = [v for v in guests if v not in excl]
    else:
        ids = [int(x) for x in str(j.get("vmid", "")).split(",") if x.strip()]
    cad = cadence(j.get("schedule", ""))
    for v in ids:
        if v in guests:
            expected.add((v, j["storage"]))
            prev = pair_cadence.get((v, j["storage"]))
            if prev is None or MAX_AGE[cad] < MAX_AGE[prev]:   # a guest in two jobs on one storage is held to the stricter one
                pair_cadence[(v, j["storage"])] = cad

# ---- when each (guest, storage) was first expected (so brand-new guests get one backup cycle of grace) ----
SEEN = os.path.join(os.path.dirname(STATE), "pve_backup_first_seen.json")
try:
    seen = json.load(open(SEEN))
except Exception:
    seen = {}
now = time.time()
for v, st in expected:
    seen.setdefault(f"{v}|{st}", now)
seen = {k: t for k, t in seen.items() if tuple([int(k.split("|")[0]), k.split("|", 1)[1]]) in expected}
os.makedirs(os.path.dirname(SEEN), exist_ok=True)
json.dump(seen, open(SEEN + ".tmp", "w")); os.replace(SEEN + ".tmp", SEEN)

# ---- what actually happened, from task logs (cached) ----------------------------------
try:
    cache = json.load(open(STATE))
except Exception:
    cache = {}
cutoff = time.time() - HISTORY_DAYS * 86400
tasks = get(f"/nodes/{NODE}/tasks?typefilter=vzdump&limit=500", timeout=60)
for t in tasks:
    upid = t["upid"]
    if upid in cache or "endtime" not in t or t["endtime"] < cutoff:
        continue
    try:
        log = get(f"/nodes/{NODE}/tasks/{upid}/log?start=0&limit=20000", timeout=60)
    except Exception as e:
        print(f"warning: cannot read log {upid}: {e}", file=sys.stderr)
        continue
    res, current = {}, None
    for line in (l["t"] for l in log):
        m = re.search(r"Starting Backup of VM (\d+)", line)
        if m:
            current = int(m.group(1)); res.setdefault(current, {"storage": None, "ok": False})
        m = re.search(r"creating vzdump archive '/mnt/pve/([^/']+)/", line) or re.search(r"creating (?:Proxmox Backup Server|vzdump) archive .*?storage '([^']+)'", line)
        if m and current is not None:
            res[current]["storage"] = m.group(1)
        m = re.search(r"Finished Backup of VM (\d+)", line)
        if m:
            res.setdefault(int(m.group(1)), {"storage": None, "ok": False})["ok"] = True
        m = re.search(r"ERROR: Backup of VM (\d+) failed", line)
        if m:
            res.setdefault(int(m.group(1)), {"storage": None, "ok": False})["ok"] = False
    cache[upid] = {"end": t["endtime"], "status": str(t.get("status", "")), "res": {str(k): v for k, v in res.items()}}
cache = {k: v for k, v in cache.items() if v["end"] >= cutoff}
os.makedirs(os.path.dirname(STATE), exist_ok=True)
json.dump(cache, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)

last_ok, last_run = {}, {}
for upid, c in sorted(cache.items(), key=lambda kv: kv[1]["end"]):
    storages_in_task = set()
    for v, r in c["res"].items():
        if r["storage"]:
            storages_in_task.add(r["storage"])
            if r["ok"]:
                k = (int(v), r["storage"]); last_ok[k] = max(last_ok.get(k, 0), c["end"])
    for st in storages_in_task:
        last_run[st] = (c["end"], c["status"])

fam = {k: [] for k in ("expected", "since", "last", "maxage", "ok")}
for (v, st) in sorted(expected):
    lab = f'vmid="{v}",name="{esc(guests[v].get("name", v))}",storage="{esc(st)}"'
    cad = pair_cadence[(v, st)]
    fam["expected"].append(f'rl_pve_backup_expected{{{lab},cadence="{cad}"}} 1')
    fam["maxage"].append(f'rl_pve_backup_max_age_seconds{{{lab},cadence="{cad}"}} {MAX_AGE[cad]}')
    fam["since"].append(f"rl_pve_backup_expected_since_timestamp_seconds{{{lab}}} {int(seen[f'{v}|{st}'])}")
    if (v, st) in last_ok:
        fam["last"].append(f"rl_pve_backup_last_success_timestamp_seconds{{{lab}}} {last_ok[(v, st)]}")
for st, (end, status) in sorted(last_run.items()):
    ok = 1 if status == "OK" or status.startswith("WARNINGS") else 0
    fam["ok"].append(f'rl_pve_vzdump_last_ok{{storage="{esc(st)}",status="{esc(status)[:60]}"}} {ok}')
lines = []
for name, key in (("rl_pve_backup_expected", "expected"), ("rl_pve_backup_last_success_timestamp_seconds", "last"),
                  ("rl_pve_backup_max_age_seconds", "maxage"), ("rl_pve_vzdump_last_ok", "ok"),
                  ("rl_pve_backup_expected_since_timestamp_seconds", "since")):
    lines.append(f"# TYPE {name} gauge")
    lines += fam[key]
lines.append("# TYPE rl_pve_backup_last_run_timestamp_seconds gauge")
lines.append(f"rl_pve_backup_last_run_timestamp_seconds {int(time.time())}")

with open(OUT + ".tmp", "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(OUT + ".tmp", OUT)
