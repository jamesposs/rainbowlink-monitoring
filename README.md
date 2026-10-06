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
(any live answer), `tcp_connect`, `dns_internal`, `dns_recursive`. Certificate expiry alerts come for free on HTTPS.
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
* Dashboards: edit `tools/gen_dashboards.py`, run `python3 tools/gen_dashboards.py`, then `apply.sh`.

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
