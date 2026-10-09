#!/usr/bin/env python3
"""08:00 Pushover summary: every alert that fired in the last 24 hours and whether it is still firing.
Reads the history from Prometheus (ALERTS), so an outage that cleared overnight is still reported. Sent every day, including
"all quiet" ones, so a missing 08:00 message means the monitor itself is in trouble.
Mirrors Alertmanager's inhibit rules in simple form: while a site is down, the alerts about hosts at that site are not listed separately.
  daily-summary.py            send it       daily-summary.py --dry-run     print it"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime

PROM = os.environ.get("PROM", "http://127.0.0.1:9090")
CREDS = os.environ.get("CREDENTIALS_DIRECTORY", "/opt/monitoring/secrets")
DASH = "https://grafana.rainbowlink.org/d/rl-status"
HOURS, STEP = 24, 60
# Which label names the alert (first two present are shown). Order matters.
SUBJECT = ["server", "service", "trunk", "tunnel", "repo", "zone", "name", "storage", "ext", "to", "host", "id", "site"]
LIMIT = 1000      # Pushover allows 1024 characters


def prom(path, **params):
    url = PROM + path + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(url, timeout=60))["data"]["result"]


now = time.time()
series = prom("/api/v1/query_range", query='ALERTS{alertstate="firing", severity=~"warning|critical"}', start=now - HOURS * 3600, end=now, step=STEP)

# Collapse series into incidents: (alertname, subject) -> [start, end, still_firing, severity, labels]
inc = {}
for s in series:
    m = s["metric"]
    subject = " ".join([m[k] for k in SUBJECT if k in m][:2])
    ts = [float(t) for t, _ in s["values"]]
    key = (m["alertname"], subject)
    start, end = min(ts), max(ts)
    cur = inc.setdefault(key, {"start": start, "end": end, "sev": m.get("severity", "warning"), "labels": []})
    cur["start"], cur["end"] = min(cur["start"], start), max(cur["end"], end)
    if m.get("severity") == "critical":
        cur["sev"] = "critical"
    cur["labels"].append(m)
for v in inc.values():
    v["firing"] = now - v["end"] < 3 * STEP


def overlap(a, b, pad=900):
    return a["start"] - pad <= b["end"] and b["start"] - pad <= a["end"]


# Cascades: drop alerts about a site/host that was itself down at the time (they say nothing new).
site_down = [(l.get("to") or l.get("site"), v) for (n, _), v in inc.items() if n == "SiteDown" for l in v["labels"]]
host_down = [(l.get("host"), v) for (n, _), v in inc.items() if n.startswith("HostDown") for l in v["labels"]]


def suppressed(name, v):
    if name == "SiteDown" or name.startswith("HostDown"):
        return False
    for l in v["labels"]:
        for site, sd in site_down:
            if site and site in (l.get("site"), l.get("to"), l.get("server")) and overlap(v, sd):
                return True
        for host, hd in host_down:
            if host and host in (l.get("host"), l.get("server")) and overlap(v, hd):
                return True
    return False


items = sorted(((n, subj, v) for (n, subj), v in inc.items() if not suppressed(n, v)), key=lambda x: (not x[2]["firing"], x[2]["sev"] != "critical", x[2]["start"]))


def when(ts):
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%a %H:%M")


def line(n, subj, v):
    who = f"{n} {subj}".strip()
    if v["firing"]:
        return f"• {who} since {when(v['start'])}"
    return f"• {who} {when(v['start'])}-{when(v['end'])}"


still = [(n, s, v) for n, s, v in items if v["firing"]]
done = [(n, s, v) for n, s, v in items if not v["firing"]]

out = []
if still:
    out.append(f"STILL FIRING ({len(still)}):")
    out += [line(*i) for i in still]
if done:
    out.append(f"RESOLVED ({len(done)}):")
    out += [line(*i) for i in done]
if not out:
    out.append("Nothing fired in the last 24 hours.")
msg, extra = "", 0
for i, l in enumerate(out):
    if len(msg) + len(l) + 1 > LIMIT - 40:
        extra = sum(1 for x in out[i:] if x.startswith("•"))
        break
    msg += l + "\n"
if extra:
    msg += f"...and {extra} more (see the Status dashboard)"
title = ("☀️ All quiet for 24 h" if not items else f"☀️ 24 h: {len(still)} still firing, {len(done)} resolved")

if "--dry-run" in sys.argv:
    print(title + "\n" + msg)
    sys.exit(0)
rd = lambda n: open(os.path.join(CREDS, n)).read().strip()
data = urllib.parse.urlencode({"token": rd("pushover_token"), "user": rd("pushover_user"), "title": title, "message": msg.strip(),
                               "priority": 0, "url": DASH, "url_title": "Open the Status board"}).encode()
r = json.load(urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data), timeout=30))
if r.get("status") != 1:
    sys.exit("pushover error: %s" % r)
print("sent:", title)
