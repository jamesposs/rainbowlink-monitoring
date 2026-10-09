#!/usr/bin/env python3
"""Transit arrivals from Home Assistant for Prometheus (node_exporter textfile collector).
Home Assistant's own Prometheus endpoint only exports an entity's state, not its attributes, and the TriMet sensors keep the
useful part (line, destination, every upcoming arrival, delays) in attributes. This reads /api/states and turns every
sensor that has an `arrivals` attribute into metrics, so a new stop added in Home Assistant appears with no change here.
Emits (all keyed by entity):
  rl_ha_transit_info{entity,name,stop,stop_id,line,destination,vehicle,service,summary} 1
  rl_ha_transit_next_minutes / _next_catchable_minutes / _next_delay_minutes / _approach_minutes / _service_active
  rl_ha_transit_arrival_minutes{entity,idx,line,destination,direction,catchable,status}  one series per upcoming arrival"""
import json, os, ssl, sys, time, urllib.request

HA = os.environ.get("HA_URL", "https://172.20.1.13")           # reached by IP; the certificate is for ha.rainbowlink.org
TOKEN = open(os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", "/opt/monitoring/secrets"), "ha_token")).read().strip()
OUT = os.environ.get("OUT", "/var/lib/prometheus/node-exporter/rl_ha_transit.prom")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(HA + "/api/states", headers={"Authorization": "Bearer " + TOKEN})
states = json.load(urllib.request.urlopen(req, timeout=20, context=ctx))


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def labels(**kw):
    return ",".join(f'{k}="{esc(v)}"' for k, v in kw.items())


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


metrics = {}   # name -> list of "labels value"


def emit(name, lab, value):
    if value is not None:
        metrics.setdefault(name, []).append(f"{name}{{{lab}}} {value:g}")


found = 0
for s in states:
    a = s.get("attributes", {})
    if not s["entity_id"].startswith("sensor.") or not isinstance(a.get("arrivals"), list):
        continue
    found += 1
    ent = s["entity_id"]
    e = labels(entity=ent)
    emit("rl_ha_transit_info", labels(entity=ent, name=a.get("friendly_name", ent), stop=a.get("stop_name", ""), stop_id=a.get("stop_id", ""),
                                      line=a.get("line", ""), destination=a.get("destination", ""), vehicle=a.get("vehicle_type", ""),
                                      service="yes" if a.get("service_active") else "no", summary=a.get("summary", "")), 1)
    emit("rl_ha_transit_service_active", e, 1 if a.get("service_active") else 0)
    emit("rl_ha_transit_approach_minutes", e, num(a.get("approach_time_minutes")))
    emit("rl_ha_transit_next_minutes", e, num(a.get("next_arrival_minutes")))
    emit("rl_ha_transit_next_catchable_minutes", e, num(a.get("next_catchable_arrival_minutes")))
    emit("rl_ha_transit_next_delay_minutes", e, num(a.get("next_delay_minutes")))
    for i, r in enumerate(a["arrivals"], 1):
        status = "canceled" if r.get("canceled") else ("+%d min" % r["delay_minutes"] if r.get("delayed") and r.get("delay_minutes") else "on time")
        emit("rl_ha_transit_arrival_minutes", labels(entity=ent, idx=i, line=r.get("line", ""), destination=r.get("destination", ""),
                                                     direction=r.get("direction", ""), catchable="yes" if r.get("catchable") else "no", status=status),
             num(r.get("minutes")))

lines = []
for name, rows in metrics.items():
    lines.append(f"# TYPE {name} gauge")
    lines += rows
lines += ["# TYPE rl_ha_transit_entities gauge", f"rl_ha_transit_entities {found}", f"rl_ha_transit_last_run_timestamp_seconds {int(time.time())}"]
with open(OUT + ".tmp", "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(OUT + ".tmp", OUT)
