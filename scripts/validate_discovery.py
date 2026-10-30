"""
TAO Discovery Platform — Lab validation runner

Exercises the discovery engine against a vCenter without going through the
web UI, so the first live scan can be checked step by step.

Read-only by default:
  1. Connects to vCenter
  2. Lists every vDS and its current IPFIX collector, and flags any
     ALLOWED_VDS entries that don't exist in this vCenter
  3. Runs the full inventory scan into SQLite and prints row counts + samples

Only with --enable-netflow does it modify vCenter (IPFIX on ALLOWED_VDS
switches only, prior config backed up). Undo with --rollback-netflow.

Usage:
  python scripts/validate_discovery.py --user svc-discovery@vsphere.local --insecure
  python scripts/validate_discovery.py ... --db data/lab-test.db      # scan into a scratch DB
  python scripts/validate_discovery.py ... --skip-inventory --enable-netflow

Password is read from DISCOVERY_VC_PASSWORD or prompted for.
"""

import argparse
import getpass
import logging
import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def parse_args():
    p = argparse.ArgumentParser(description="Validate discovery against a live vCenter")
    p.add_argument("--host", default="zvcfvc01.taodigital.lab", help="vCenter FQDN or IP (default: lab vCenter)")
    p.add_argument("--user", required=True, help="vCenter username")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification (self-signed lab certs)")
    p.add_argument("--db", help="SQLite path to scan into (default: data/discovery.db)")
    p.add_argument("--skip-inventory", action="store_true", help="Skip the inventory scan")
    p.add_argument("--enable-netflow", action="store_true", help="WRITES to vCenter: enable IPFIX on ALLOWED_VDS")
    p.add_argument("--rollback-netflow", action="store_true", help="WRITES to vCenter: restore backed-up IPFIX config")
    return p.parse_args()


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def main():
    args = parse_args()
    if args.enable_netflow and args.rollback_netflow:
        sys.exit("Choose one of --enable-netflow / --rollback-netflow")

    # Engine modules read DISCOVERY_DB_PATH at import time, so set it first
    if args.db:
        os.environ["DISCOVERY_DB_PATH"] = str(Path(args.db).resolve())
    sys.path.insert(0, str(BASE_DIR / "engine"))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from db_init import init_db, DB_PATH
    from inventory_collector import run_full_discovery
    from netflow_manager import (ALLOWED_VDS, preflight_check_and_backup,
                                 execute_netflow_override, execute_netflow_rollback)

    password = os.environ.get("DISCOVERY_VC_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    init_db(DB_PATH)

    section("vDS pre-flight (read-only)")
    report = preflight_check_and_backup(args.host, args.user, password, args.insecure)
    for r in report:
        mark = "WILL MODIFY" if r["will_be_modified"] else "untouched"
        print(f"  {r['vds_name']:<35} collector={r['current_collector_ip'] or '-':<16} [{mark}]")
    found = {r["vds_name"] for r in report}
    missing = [n for n in ALLOWED_VDS if n not in found]
    if missing:
        print(f"  WARNING: ALLOWED_VDS entries not present in this vCenter: {missing}")
    for r in report:
        if r["will_be_modified"] and r["is_in_use"]:
            print(f"  WARNING: {r['vds_name']} already exports IPFIX to {r['current_collector_ip']} — enabling will redirect it")

    if not args.skip_inventory:
        section("Inventory scan")
        print(f"  Writing to {DB_PATH}")
        run_full_discovery(args.host, args.user, password, args.insecure)

        conn = sqlite3.connect(DB_PATH)
        for table in ("datacenters", "clusters", "esxi_hosts", "virtual_machines", "datastores", "dependencies"):
            print(f"  {table:<18} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")

        section("Sample VMs")
        rows = conn.execute("""SELECT vm_name, power_state, vcpu, ram_gb, ip_address, cluster_name
                               FROM virtual_machines ORDER BY vm_name LIMIT 10""").fetchall()
        for name, state, cpu, ram, ip, cluster in rows:
            print(f"  {name:<30} {state:<12} {cpu} vCPU {ram:>6} GB  {ip or '-':<16} {cluster or '-'}")

        section("Data-quality checks")
        checks = {
            "VMs with no IP (tools off / powered off?)": "SELECT COUNT(*) FROM virtual_machines WHERE COALESCE(ip_address,'')=''",
            "VMs with no cluster":                      "SELECT COUNT(*) FROM virtual_machines WHERE COALESCE(cluster_name,'')=''",
            "Hosts with no management IP":              "SELECT COUNT(*) FROM esxi_hosts WHERE COALESCE(ip_address,'')=''",
        }
        for label, sql in checks.items():
            print(f"  {label:<45} {conn.execute(sql).fetchone()[0]}")
        conn.close()

    if args.enable_netflow:
        section("Enabling IPFIX (WRITES to vCenter)")
        execute_netflow_override(args.host, args.user, password, args.insecure)
    elif args.rollback_netflow:
        section("Rolling back IPFIX (WRITES to vCenter)")
        execute_netflow_rollback(args.host, args.user, password, args.insecure)


if __name__ == "__main__":
    main()
