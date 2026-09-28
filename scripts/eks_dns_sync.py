#!/usr/bin/env python3
"""Sync the public CNAMEs to the live load-balancer hostnames.

Upserts, in the Route53 hosted zone named after --domain:

    console.<domain>  CNAME  <prahari-web NLB hostname>
    streams.<domain>  CNAME  <prahari-mediamtx-public NLB hostname>

Why this exists: the NLBs are created by the AWS cloud provider when the
LoadBalancer Services apply — Terraform cannot see their hostnames, so the
records cannot live in .tf files. The LB hostname only changes if the LB is
recreated; re-running this keeps DNS self-healing.

boto3 (not the aws CLI) earns its place here for two things the CLI does
awkwardly: polling the Services until the cloud provider publishes LB
hostnames, and building the UPSERT change batch programmatically.

Run:  uv run --no-project --with boto3 scripts/eks_dns_sync.py --domain eks.example.com
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import boto3

POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 600
TTL_S = 300


def lb_hostname(namespace: str, service: str) -> str | None:
    out = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            service,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() or None


def wait_for_hostname(namespace: str, service: str) -> str:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        host = lb_hostname(namespace, service)
        if host:
            return host
        if time.monotonic() > deadline:
            sys.exit(f"timed out waiting for {service} LB hostname")
        time.sleep(POLL_INTERVAL_S)


def find_zone_id(route53, domain: str) -> str:
    name = domain.rstrip(".") + "."
    zones = route53.list_hosted_zones_by_name(DNSName=name, MaxItems="1")
    for zone in zones.get("HostedZones", []):
        if zone["Name"] == name:
            return zone["Id"].rsplit("/", 1)[-1]
    sys.exit(
        f"no Route53 hosted zone named {name} — set domain_name in tfvars, "
        "terraform apply, then delegate the NS records at the registrar"
    )


def upsert_cname(route53, zone_id: str, fqdn: str, target: str) -> None:
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": fqdn,
                        "Type": "CNAME",
                        "TTL": TTL_S,
                        "ResourceRecords": [{"Value": target}],
                    },
                }
            ]
        },
    )
    print(f"{fqdn}  ->  {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", required=True, help="delegated zone, e.g. eks.example.com")
    ap.add_argument("--namespace", default="prahari")
    args = ap.parse_args()

    domain = args.domain.rstrip(".")
    route53 = boto3.client("route53")
    zone_id = find_zone_id(route53, domain)

    console_host = wait_for_hostname(args.namespace, "prahari-web")
    streams_host = wait_for_hostname(args.namespace, "prahari-mediamtx-public")

    upsert_cname(route53, zone_id, f"console.{domain}", console_host)
    upsert_cname(route53, zone_id, f"streams.{domain}", streams_host)

    print(
        json.dumps(
            {
                "console": f"https://console.{domain}",
                "whep": f"https://streams.{domain}",
            }
        )
    )


if __name__ == "__main__":
    main()
