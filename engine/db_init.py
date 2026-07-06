"""
TAO Discovery Platform — Database Schema Initialiser
Creates the SQLite schema used by the inventory collector, IPFIX listener,
Flask API and report generator. Safe to re-run (CREATE TABLE IF NOT EXISTS).
"""

import sqlite3
import os
import logging

logger = logging.getLogger("db_init")
DB_PATH = "/opt/discovery-appliance/data/discovery.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS datacenters (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_name     TEXT PRIMARY KEY,
    datacenter_name  TEXT,
    drs_enabled      INTEGER DEFAULT 0,
    drs_behavior     TEXT,
    ha_enabled       INTEGER DEFAULT 0,
    total_hosts      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS esxi_hosts (
    hostname              TEXT PRIMARY KEY,
    cluster_name          TEXT,
    ip_address            TEXT,
    vendor                TEXT,
    model                 TEXT,
    cpu_model             TEXT,
    cpu_sockets           INTEGER,
    cpu_cores_per_socket  INTEGER,
    total_cpu_cores       INTEGER,
    ram_gb                REAL,
    esxi_version          TEXT,
    esxi_build            TEXT,
    connection_state      TEXT,
    power_state           TEXT,
    num_nics              INTEGER,
    num_hbas              INTEGER,
    scan_timestamp        TEXT
);

CREATE TABLE IF NOT EXISTS virtual_machines (
    vm_uuid               TEXT PRIMARY KEY,
    vm_name               TEXT,
    power_state           TEXT,
    configured_os         TEXT,
    running_os            TEXT,
    guest_hostname        TEXT,
    vcpu                  INTEGER,
    ram_gb                REAL,
    cpu_usage_mhz         INTEGER,
    mem_usage_mb          INTEGER,
    hw_version            TEXT,
    num_disks             INTEGER,
    disk_sizes_gb         TEXT,
    provisioned_space_gb  REAL,
    used_space_gb         REAL,
    ip_address            TEXT,
    mac_address           TEXT,
    portgroup             TEXT,
    host_name             TEXT,
    cluster_name          TEXT,
    vcenter_tags          TEXT,
    vmware_tools_status   TEXT,
    vmware_tools_version  TEXT,
    scan_timestamp        TEXT
);
CREATE INDEX IF NOT EXISTS idx_vm_name ON virtual_machines(vm_name);
CREATE INDEX IF NOT EXISTS idx_vm_ip ON virtual_machines(ip_address);

CREATE TABLE IF NOT EXISTS datastores (
    name             TEXT PRIMARY KEY,
    capacity_gb      REAL,
    free_gb          REAL,
    datastore_type   TEXT,
    accessible       INTEGER,
    scan_timestamp   TEXT
);

CREATE TABLE IF NOT EXISTS dependencies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_ip        TEXT,
    src_vm_name   TEXT,
    dst_ip        TEXT,
    dst_vm_name   TEXT,
    dst_port      INTEGER,
    protocol      TEXT,
    flow_count    INTEGER DEFAULT 1,
    first_seen    TEXT,
    last_seen     TEXT,
    UNIQUE(src_ip, dst_ip, dst_port, protocol)
);
CREATE INDEX IF NOT EXISTS idx_dep_flowcount ON dependencies(flow_count DESC);
"""


def init_db(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    logger.info(f"Database schema initialised at {path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
