#!/usr/bin/env python3
"""DNS replication check: are all resolvers serving the same version of each zone?
Asks every server for the zone's SOA and compares the serial (it only changes when the zone changes, and secondaries
must catch up to it). The existing dns_internal probe only proves ONE record resolves, not that the zone is current.

Emits (node_exporter textfile):
  rl_dns_zone_serial{zone,server,sync}   SOA serial served by that server (absent if it did not answer)
  rl_dns_zone_present{zone,server,sync}  1 if the server serves OUR copy of the zone, else 0 (e.g. forwards it upstream)
sync="all"      zones that must be identical on every server -> alerts
sync="primary"  zones that exist only on the primary today    -> informational (dashboard), no alert
ADD A RESOLVER: add it to SERVERS (and targets/dns.yml). ADD A ZONE: add it to ZONES.
"""
import os, random, socket, struct, sys, time

OUT = os.environ.get("OUT", "/var/lib/prometheus/node-exporter/rl_dns_sync.prom")
PRIMARY_NAME = "dns1.rainbowlink.org"          # SOA MNAME of our zones; anything else is someone else's zone
SERVERS = {"dns1": "172.20.2.221", "pi-pdx": "172.20.1.37", "pi-pcb": "100.64.0.11"}
ZONES = {
    "rainbowlink.org": "all",
    "dns.rainbowlink.org": "all",
    "207.249.173.in-addr.arpa": "all",
    "e.f.3.5.0.0.8.0.0.0.0.9.7.0.6.2.ip6.arpa": "all",
}


def qname(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.strip(".").split(".")) + b"\0"


def read_name(d, o):
    labels, end, jumped = [], o, False
    while True:
        l = d[o]
        if l == 0:
            o += 1
            break
        if l & 0xC0 == 0xC0:
            if not jumped:
                end = o + 2
            jumped = True
            o = ((l & 0x3F) << 8) | d[o + 1]
            continue
        labels.append(d[o + 1:o + 1 + l].decode())
        o += 1 + l
    return ".".join(labels), (end if jumped else o)


def soa(server, zone):
    """-> (serial, mname) or None if no SOA answer."""
    pkt = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0) + qname(zone) + struct.pack(">HH", 6, 1)
    for _ in range(2):  # one retry: UDP can drop
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(4)
        try:
            s.sendto(pkt, (server, 53))
            d, _ = s.recvfrom(4096)
        except OSError:
            continue
        finally:
            s.close()
        an = struct.unpack(">H", d[6:8])[0]
        if d[3] & 0xF != 0 or an == 0:
            return None
        _, o = read_name(d, 12)
        o += 4
        for _ in range(an):
            _, o = read_name(d, o)
            typ, _, _, rl = struct.unpack(">HHIH", d[o:o + 10])
            o += 10
            if typ == 6:
                mname, p = read_name(d, o)
                _, p = read_name(d, p)
                return struct.unpack(">I", d[p:p + 4])[0], mname
            o += rl
        return None
    return None


lines = ["# TYPE rl_dns_zone_serial gauge", "# TYPE rl_dns_zone_present gauge"]
for zone, sync in ZONES.items():
    for name, ip in SERVERS.items():
        lab = f'zone="{zone}",server="{name}",sync="{sync}"'
        r = soa(ip, zone)
        if r is None:
            lines.append(f"rl_dns_zone_present{{{lab}}} 0")
        elif r[1].lower() == PRIMARY_NAME:
            lines.append(f"rl_dns_zone_serial{{{lab}}} {r[0]}")
            lines.append(f"rl_dns_zone_present{{{lab}}} 1")
        else:
            lines.append(f"rl_dns_zone_present{{{lab}}} 0")     # answered, but with someone else's SOA: not our copy
lines.append(f"rl_dns_sync_last_run_timestamp_seconds {int(time.time())}")
with open(OUT + ".tmp", "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(OUT + ".tmp", OUT)
