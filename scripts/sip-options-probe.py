#!/usr/bin/env python3
"""SIP health probe: sends a real SIP OPTIONS request and checks that the PBX answers.
Proves Asterisk's SIP stack is alive and answering (not just that a port is open).
Any SIP answer below 500 counts as alive. Writes node_exporter textfile metrics.   usage: sip-options-probe.py name=host:port[/udp|tcp] ...
"""
import os, random, socket, string, sys, time

OUT = os.environ.get("OUT", "/var/lib/prometheus/node-exporter/rl_sip.prom")


def probe(host, port, proto="udp", timeout=4.0):
    cid = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    local_ip = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((host, port))
        local_ip, lport = s.getsockname()
        req = (f"OPTIONS sip:{host} SIP/2.0\r\n"
               f"Via: SIP/2.0/{proto.upper()} {local_ip}:{lport};branch=z9hG4bK{cid}\r\n"
               f"Max-Forwards: 70\r\nFrom: <sip:monitor@{local_ip}>;tag={cid[:8]}\r\nTo: <sip:{host}>\r\n"
               f"Call-ID: {cid}@{local_ip}\r\nCSeq: 1 OPTIONS\r\nUser-Agent: rainbowlink-monitor\r\n"
               f"Accept: application/sdp\r\nContent-Length: 0\r\n\r\n")
        s.send(req.encode())
        data = s.recv(2048).decode(errors="replace")
        rtt = time.time() - t0
        first = data.split("\r\n", 1)[0]
        # Any final answer below 500 means Asterisk's SIP stack is alive (it replies 401 to
        # OPTIONS from unauthenticated senders). 5xx or silence is a failure.
        parts = first.split(" ")
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (1 if 200 <= code < 500 else 0), rtt, first
    except Exception as e:
        return 0, time.time() - t0, f"{type(e).__name__}"
    finally:
        s.close()


lines = ["# TYPE rl_sip_options_up gauge", "# TYPE rl_sip_options_rtt_seconds gauge"]
for spec in sys.argv[1:]:
    name, rest = spec.split("=", 1)
    proto = "udp"
    if "/" in rest:
        rest, proto = rest.split("/", 1)
    host, port = rest.rsplit(":", 1)
    up, rtt, first = probe(host, int(port), proto)
    if not up:  # one retry: UDP can drop a packet
        up, rtt, first = probe(host, int(port), proto)
    lines.append(f'rl_sip_options_up{{target="{name}",proto="{proto}"}} {up}')
    lines.append(f'rl_sip_options_rtt_seconds{{target="{name}",proto="{proto}"}} {rtt:.4f}')
    print(f"{name}: {first!r} rtt={rtt*1000:.0f}ms", file=sys.stderr)
lines.append(f"rl_sip_probe_last_run_timestamp_seconds {int(time.time())}")
with open(OUT + ".tmp", "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(OUT + ".tmp", OUT)
