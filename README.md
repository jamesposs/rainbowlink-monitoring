# Rainbowlink monitoring

Prometheus + Alertmanager + Grafana on the `grafana` LXC (Proxmox 128). Everything is files in this
directory (`/opt/monitoring`, tracked in git). Normal state is **quiet**: you are only contacted for
things worth investigating.

```
 every Linux host ── node_exporter (apt, unprivileged, tailnet address only) ──┐
 pve0 ── read-only API token ── pve-exporter ───────────────────────────────────┤
 NAS / UniFi gateway ── SNMP ── snmp-exporter ──────────────────────────────────┤
 Home Assistant ── its own /api/prometheus ─────────────────────────────────────┼─► Prometheus ─► Alertmanager ─► Pushover
 blackbox: ping / HTTP / DNS / TCP (home) + public view from vps-sjc ───────────┤        │               │
 vantage hosts (pdx, sjc, bos, pcb): ping mesh + PBX + Docker + apt facts ──────┘        ▼               └─► healthchecks.io
                                                                                      Grafana             (dead-man's switch)
```

## Where to look
| Question | Look at |
|---|---|
| Is everything OK? What is broken, since when? | Grafana **Overview** (`https://grafana.rainbowlink.org`) |
| Internet, site-to-site latency/loss, DNS | **Network** |
| Hosts, Proxmox guests, NAS, backups | **Servers & storage** |
| Every service check, certificates, PBX, HA, Docker | **Services** |
| Quick terminal view | `sudo ./scripts/status.sh` |

## What reaches you
| Severity | Meaning | Delivery |
|---|---|---|
| `critical` | crippling, worth waking up | Pushover **emergency** priority: repeats every 5 min until you acknowledge (max 1 h) |
| `warning` | look at it tomorrow (missed backup, cert expiring, disk 85%, service down at home...) | **one** Pushover message 08:00-09:00 PT, then daily while still true |

Critical today: **home DNS outage** (dns1+dns2 both failing) · **pve0 down** · **PBX not answering SIP** ·
**both carrier trunks unregistered** · **Boston/PBX site unreachable** · **NAS RAID crashed**.
Internet loss cannot page you (the internet is what is broken): the **dead-man's switch** does that.

Causes silence their symptoms (`alertmanager/alertmanager.yml`, `inhibit_rules`): site down → everything at that
site; home internet down → all remote/public alerts; gateway down → everything at home; pve0 down → all guests;
host down → its own services; tailnet trouble → agent-down alerts.

## Backups covered
* **Proxmox guests** (nightly to the NAS, 1st/15th to pi-pcb): freshness per guest per target, expected set derived from the backup jobs.
* **NAS Hyper Backup** (`nas_hq_set2`, daily ~09:35 PT, destination = pi-pcb over rsync): read from the *destination* on pi-pcb, so the
  NAS needs no credentials or SSH. Each committed backup writes `Config/version_info.db.<N>`; the newest N and its mtime are exported by
  `rl-hyperbackup-metrics.sh`. `HyperBackupStale` fires after 28 h without a new version, `HyperBackupStuck` after 12 h of continuous running.
  Other Hyper Backup tasks that write elsewhere (USB, cloud, another host) are NOT visible this way.
* Alert rule unit tests live in `prometheus/tests/` and run on every `apply.sh`.

## DNS replication
`scripts/dns-sync-metrics.py` asks every resolver for each zone's SOA serial every 2 minutes (the existing `dns_internal` probe only proves one
record resolves, not that the zone is current). Zones marked `sync: all` (today: `rainbowlink.org`) must have the same serial everywhere:
`DnsZoneOutOfSync` after 15 minutes of disagreement, `DnsZoneMissing` if a server stops serving it. All four resolvers serve every zone (Technitium cluster: dns1 is primary, the rest are secondaries via `cluster-catalog.rainbowlink.org`). A NEW primary zone on dns1 must be given that catalog under zone Options, or it stays dns1-only; add it to ZONES here so drift alerts.
Add a resolver to `SERVERS` (and `targets/dns.yml`); add a zone to `ZONES`; set `all` once it should be everywhere.

## Split-horizon DNS (tailnet vs home LAN)
The rule is only the **source address of the query**: from the tailnet (100.64.0.0/10, fd7a:115c:a1e0::/48) -> tailnet addresses; from anywhere else
(home LAN, guest VLAN) -> LAN addresses. Technitium **Split Horizon app, Address Translation mode**, on all four resolvers. The zone stores the
home LAN address of every host; for tailnet sources the app rewrites each LAN address to that host's tailnet address (29 pairs). Names with no
LAN twin (pbx2, dns-pcb, hs, sjc, pcb, ...) are never touched.
- **Consequence:** a device at home that uses Tailscale DNS (laptop, phone, any server with Tailscale DNS) is a tailnet source and gets tailnet
  addresses; one using the LAN resolvers gets LAN addresses. The short MagicDNS names (`ha`, `ha.ts.rainbowlink.org`) are Tailscale's own and always tailnet.
- **Monitoring:** the monitor host uses Tailscale DNS, so its blackbox container is pinned to the home resolvers (`dns:` in compose.yml) and keeps
  testing LAN paths. `dns_internal` (via LAN) expects `ha` = 172.20.1.13; `dns_internal_tailnet` (pi-pcb, vps-sjc, asked over the tailnet) expects 100.64.0.27;
  `DnsSplitHorizonBroken` probes from vps-sjc. A resolver that lost the app would trip one of these within 10 minutes.
- **Why not APP records:** `rainbowlink.org` is DNSSEC-signed on Technitium and signed zones cannot hold APP records.
- **Add a host:** give it a normal A record with its LAN IP, then add `LAN-ip: tailnet-ip` under Technitium > Apps > Split Horizon > Config on **each** node
  (config does not replicate; the app install does). `/opt/monitoring/dns/split-horizon.config.json` is the reference copy (no secrets).
- **Roaming devices** get the answer for the resolver they use; split names have a 300 s TTL so a change of network is picked up within 5 minutes.
- **Undo:** set `enableAddressTranslation` to false on each node (instant). Records changed: `dns1` (had extra tailnet A/AAAA), `channels` and `claudeproxy`
  (were tailnet-only, now LAN + translation). Pre-change zone backup: `/root/dns-zones-pre-split-20261007.tgz` on dns1.

## Site map and inter-site traffic
`Site map` (Grafana): a US map of the sites with every link coloured by health (green normal; orange loss >2% or latency >2x its own
1-day norm; red loss >10%/down; 5-minute averages) over Grafana's bundled US outline, so it needs no tile server or API key (Carto's free
tiles now require a key). Below it: link detail, traffic per link, and the busiest hosts. Traffic comes from `rl-tailnet-metrics.py`
(Ansible role, remote-site hosts): Tailscale's per-peer byte counters, summed per site; it covers tailnet traffic only, not internet traffic.
On the map: line colour = link health; the blue ribbon under a line = traffic band (quiet < 5 kbit/s, moderate 5-25, busy > 25; change
`TRAFFIC_BANDS`); at each link's midpoint: latency (white) and traffic (blue). Edit sites/links in `tools/gen_map.py`; add the new node's tailnet IP
to `monitoring_tailnet_site_of` in the role.
Grafana Geomap quirks (so you do not rediscover them): route layers take ONE query each and show no tooltip; number fields are named `Value #<refId>`
when a panel has several queries; a `displayName` override renames every field of the frame; click (not hover) opens marker popups.

## Disk health
* **Servers & storage → Disk health:** NAS drives (SMART over SNMP: health, temperature, age, reallocated/pending/uncorrectable sectors, CRC errors), `pve0` drives (SATA SMART, NVMe wear/spare/media errors), LVM thin-pool data+metadata.
* Alerts are early warnings (next-day digest): any pending/uncorrectable sector, *growth* of reallocated sectors/CRC errors over 7 days, NVMe critical flag / media errors / wear > 80% / spare < 20%, drive temperature, thin pool data > 85% or metadata > 75%.
* Unit tests for these rules: `prometheus/tests/alerts_test.yml`.

## Add a Linux host (2 steps)
1. Install the agent (from the Ansible host, or `ansible-playbook playbooks/monitoring.yml -l <host>`).
   It installs unprivileged `prometheus-node-exporter` on the tailnet address only.
2. Add one line to `prometheus/targets/hosts.yml`. Prometheus picks it up within ~30 s (no restart):
   ```yaml
   - {targets: [newhost], labels: {addr: 172.20.2.50, ts_ip: 100.64.0.40, site: pdx, kind: lxc, parent: pve0, role: app}}
   ```
   You immediately get: ping, CPU/memory/disk/updates/failed-service/Docker alerts, and a row in the dashboards.
   Backups of a new Proxmox guest are picked up automatically (derived from the backup jobs).
   Remote host? `site: <site>`, `addr` = its tailnet IP, no `parent`.

## Add a service check
Append a line to `targets/http.yml` (web), `tcp.yml` (port) or `dns.yml` (resolver). Public checks, probed from
outside, go in `public.yml`.
```yaml
- {targets: ['https://app.rainbowlink.org/'], labels: {module: http_2xx, host: web, site: pdx, service: My app}}
```
Modules: `http_2xx` (answers 2xx, certificate verified), `http_2xx_insecure` (self-signed), `http_responds`
(any live answer), `http_health_ok` (health endpoint must return `{"status":"OK"}`), `http_redirect_sso` (must 302 to our SSO, not followed),
`tcp_connect`, `dns_internal`, `dns_recursive`. Prefer a health endpoint over a page behind a login redirect: a followed redirect ends on the SSO
server's page and tests the SSO server, not your app. Public checks run on vps-sjc, whose modules come from the Ansible role (`blackbox.yml.j2`). Certificate expiry alerts come for free on HTTPS.
`host:` matters: if that host is down its checks stay silent.

## Change thresholds / what is critical
* Thresholds live in `prometheus/rules/*.yml` (one alert per block, `expr:` and `for:`). Edit, then `sudo ./scripts/apply.sh` (validates first).
* A host's down-alert becomes critical with `critical: "true"` in `hosts.yml`.
* Promote/demote any alert: change its `severity:` label to `critical` or `warning`.
* Known-benign failed systemd units are excluded in `rules/10-hosts.yml` (`SystemdUnitFailed`).
* Backup allowed age per target: `MAX_AGE` in `scripts/pve-backup-metrics.py`.
* Maintenance window: `sudo ./scripts/silence.sh <host> 2h "reason"`.
* Test the notification path: `sudo ./scripts/test-notify.sh critical` (it will keep buzzing until you acknowledge).

## Secrets (never in git)
`/opt/monitoring/.env` (Grafana admin) and `secrets/` (0640 root:nogroup):
`pushover_user`, `pushover_token`, `healthchecks_url`, `ha_token`, `pve.yml` + `pve_api_token` (read-only PVEAuditor token `monitoring@pve!prometheus`),
`snmp-auth.yml` (SNMP community), `unifi*` if UniFi polling is enabled. Rotate by replacing the file, then `docker compose restart <service>`.

## Update / upgrade
* Containers are **pinned** in `compose.yml`. To upgrade: read the release notes, change the tag, `sudo ./scripts/apply.sh`. Roll back by restoring the old tag.
* Hosts' `node_exporter` is the distro package (security updates arrive with normal apt updates).
* Dashboards: edit the JSON in `grafana/dashboards/` directly (there is no generator any more), then `apply.sh`; Grafana picks it up in 1-2 minutes. UI edits are disabled on purpose.
  Exception: the **Site map** (`map.json`) is generated. To add a site or move a marker, edit `SITES` in `tools/gen_map.py`, run `python3 tools/gen_map.py`, then `apply.sh`.
  Also generated: the **Transit** board (`transit.json`, from `tools/gen_transit.py`; run it, then `gen_map.py` to sync the navigation links, then `apply.sh`).
  Its data comes from `scripts/ha-transit-metrics.py` (timer `rl-ha-transit`, every minute): Home Assistant's Prometheus endpoint exports only entity *state*, so this reads `/api/states` and
  turns every sensor with an `arrivals` attribute into `rl_ha_transit_*` metrics. A new stop added in Home Assistant appears with no change here.

## Backup, restore, rebuild
* The whole stack (config, git history, secrets, Prometheus + Grafana data volumes) is inside LXC 128, which Proxmox
  backs up **nightly to the NAS** and **1st/15th to pi-pcb** (offsite). Check freshness on Servers & storage.
* **Restore:** `pct restore 128 <backup> --force` on pve0 (or restore the 128 backup from the Proxmox UI), start it. Docker brings the stack back (`restart: unless-stopped`).
* **Rebuild from nothing:** new Debian LXC + Docker + tailscale (join Headscale) → copy `/opt/monitoring` (git repo) → recreate `secrets/` + `.env` (Pushover keys, healthchecks URL, new PVE token via `pveum user token add monitoring@pve prometheus --privsep 0`, HA token, SNMP community) → `docker compose up -d`. Historical metrics are lost, nothing else.
* Want a second copy of the config? `git remote add origin <url> && git push` from `/opt/monitoring` (secrets are git-ignored).

## Monitoring the monitor
* `Watchdog` always fires; Alertmanager pings **healthchecks.io** every 2 min. If pings stop (Prometheus, Alertmanager, this LXC, pve0 or the home internet is down) healthchecks.io notifies you through its own Pushover integration. Configure the check as period 2 min, grace 5 min.
* Prometheus/Grafana/exporters have healthchecks and `restart: unless-stopped`; stopped or unhealthy containers on any Docker host raise `DockerContainerDown` (this would have caught the Prometheus container that sat stopped for two days).
* Scrape failures of SNMP/PVE/HA raise `MonitoringTargetDown`; failing Pushover delivery raises `AlertDeliveryFailing`.

## Security
* Agents are unprivileged (`prometheus` user), listen only on the tailnet address, and (ufw hosts) accept only the monitoring server. No root daemons; the small timer scripts that need root only read state (`docker ps`, `apt-get -s`).
* Prometheus/Alertmanager bind to `127.0.0.1`. Only Grafana is reachable (LAN :3000 / existing SSO URL).
* Proxmox access is a read-only API token. The PBX is queried with a SIP OPTIONS request and via the `asterisk` user's CLI.
* Checkmk/Zabbix agents (open on all interfaces) were removed from every host.

## Caveats
* Inside an LXC the kernel shows the hypervisor's CPU, so LXC CPU is taken from Proxmox (`GuestCpuSaturated`), not from the guest.
* `eth4` is the gateway's WAN port (`GatewayWanPortDown`). If you re-cable, edit `rules/50-pbx-nas-gateway.yml`.
* `pi-pcb` and `vps-bos` do not answer ICMP on their public IPs; their public path is checked over TCP 443 / SIP instead.
