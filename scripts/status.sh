#!/bin/sh
# "What is broken right now?" in the terminal.  ./scripts/status.sh
docker exec prometheus wget -qO- http://localhost:9090/api/v1/alerts | python3 -c '
import sys,json
a=[x for x in json.load(sys.stdin)["data"]["alerts"] if x["labels"].get("severity")!="none"]
if not a: print("all quiet: no firing or pending alerts")
for x in sorted(a,key=lambda x:(x["state"]!="firing",x["labels"].get("severity")!="critical")):
    l=x["labels"]; print("%-8s %-9s %-26s %s"%(x["state"],l.get("severity"),l["alertname"],x["annotations"].get("summary","")[:100]))
'
