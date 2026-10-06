#!/usr/bin/env python3
"""Cloudflare tunnel health for Prometheus (node_exporter textfile collector).
Read-only account API token. Emits rl_cloudflare_tunnel_healthy{tunnel} (1 healthy, 0 degraded/down/inactive)
and rl_cloudflare_tunnel_connections{tunnel}. A tunnel that is down takes every public hostname behind it offline."""
import json, os, sys, time, urllib.request

ACCOUNT = os.environ.get("CF_ACCOUNT", "1e7e1297cee95b94d667560f52a158c6")
TOKEN = open(os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", "/opt/monitoring/secrets"), "cloudflare_token")).read().strip()
OUT = os.environ.get("OUT", "/var/lib/prometheus/node-exporter/rl_cloudflare.prom")

req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/cfd_tunnel?is_deleted=false",
                             headers={"Authorization": "Bearer " + TOKEN})
data = json.load(urllib.request.urlopen(req, timeout=20))
if not data.get("success"):
    sys.exit("cloudflare api error: %s" % data.get("errors"))
lines = ["# TYPE rl_cloudflare_tunnel_healthy gauge", "# TYPE rl_cloudflare_tunnel_connections gauge"]
for t in data["result"]:
    name = t["name"].replace('"', "")
    lines.append(f'rl_cloudflare_tunnel_healthy{{tunnel="{name}",status="{t["status"]}"}} {1 if t["status"] == "healthy" else 0}')
    lines.append(f'rl_cloudflare_tunnel_connections{{tunnel="{name}"}} {len(t.get("connections", []))}')
lines.append(f"rl_cloudflare_last_run_timestamp_seconds {int(time.time())}")
with open(OUT + ".tmp", "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(OUT + ".tmp", OUT)
