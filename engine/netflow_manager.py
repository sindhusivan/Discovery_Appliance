"""
TAO Discovery Platform — vDS NetFlow (IPFIX) Manager
Configures IPFIX export on approved vSphere Distributed Switches only.

Safety model: this module will NEVER modify a vDS switch that is not
explicitly named in ALLOWED_VDS below. Leave ALLOWED_VDS empty to make
every write operation a safe no-op — this is the default until an
engagement reaches its NetFlow validation phase, so an accidental
"Initiate Discovery" click cannot touch a client's production network.
"""

import json
import logging
import os
import socket
import time

from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si

logger = logging.getLogger("netflow_manager")

# Only vDS switches listed here (by name) will ever be modified.
# Add the name of a dedicated, isolated test/lab vDS before the NetFlow
# validation phase. Never add a production distributed switch.
ALLOWED_VDS = []

COLLECTOR_PORT = 4739
BACKUP_PATH = "/opt/discovery-appliance/data/netflow_backup.json"


def _get_local_ip():
    """Best-effort discovery of this appliance's own IP, used as the IPFIX collector address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def _find_vds(content, name):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.dvs.VmwareDistributedVirtualSwitch], True)
    try:
        for vds in container.view:
            if vds.name == name:
                return vds
        return None
    finally:
        container.Destroy()


def preflight_check_and_backup(vcenter_ip, username, password, disable_ssl=False):
    """Read-only check: list every vDS in the environment with its current
    NetFlow/IPFIX collector config and whether our safelist will touch it.
    Makes no changes. Returns a list of {vds_name, is_in_use,
    current_collector_ip, will_be_modified} — consumed directly by the
    Configuration page's pre-flight table."""
    si = get_service_instance(vcenter_ip, username, password, disable_ssl)
    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.dvs.VmwareDistributedVirtualSwitch], True)
        report = []
        try:
            for vds in container.view:
                ipfix = getattr(vds.config, "ipfixConfig", None)
                collector_ip = ipfix.collectorIpAddress if ipfix else ""
                report.append({
                    "vds_name": vds.name,
                    "is_in_use": bool(collector_ip),
                    "current_collector_ip": collector_ip,
                    "will_be_modified": vds.name in ALLOWED_VDS,
                })
        finally:
            container.Destroy()
        logger.info(f"Pre-flight check complete: {len(report)} vDS switch(es) found")
        return report
    finally:
        close_si(si)


def execute_netflow_override(vcenter_ip, username, password, disable_ssl=False):
    """Enable IPFIX export on switches in ALLOWED_VDS, backing up prior config first.
    No-op (logs and returns) if ALLOWED_VDS is empty."""
    if not ALLOWED_VDS:
        logger.info("ALLOWED_VDS is empty — skipping NetFlow configuration (safe no-op).")
        return {"configured": [], "skipped": True}

    collector_ip = _get_local_ip()
    si = get_service_instance(vcenter_ip, username, password, disable_ssl)
    backups = {}
    configured = []
    try:
        content = si.RetrieveContent()
        for name in ALLOWED_VDS:
            vds = _find_vds(content, name)
            if not vds:
                logger.warning(f"ALLOWED_VDS entry '{name}' not found in vCenter — skipping")
                continue

            existing = vds.config.ipfixConfig
            backups[name] = {
                "collectorIpAddress": existing.collectorIpAddress if existing else "",
                "collectorPort": existing.collectorPort if existing else 0,
                "activeFlowTimeout": existing.activeFlowTimeout if existing else 60,
                "idleFlowTimeout": existing.idleFlowTimeout if existing else 15,
                "samplingRate": existing.samplingRate if existing else 0,
            }

            spec = vim.dvs.VmwareDistributedVirtualSwitch.VmwareConfigSpec()
            spec.configVersion = vds.config.configVersion
            spec.ipfixConfig = vim.dvs.VmwareDistributedVirtualSwitch.IpfixConfig()
            spec.ipfixConfig.collectorIpAddress = collector_ip
            spec.ipfixConfig.collectorPort = COLLECTOR_PORT
            spec.ipfixConfig.activeFlowTimeout = 60
            spec.ipfixConfig.idleFlowTimeout = 15
            spec.ipfixConfig.samplingRate = 0

            task = vds.ReconfigureDvs_Task(spec)
            _wait_for_task(task)
            configured.append(name)
            logger.info(f"NetFlow enabled on vDS '{name}' -> collector {collector_ip}:{COLLECTOR_PORT}")

        os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
        with open(BACKUP_PATH, "w") as f:
            json.dump(backups, f, indent=2)

        return {"configured": configured, "skipped": False}
    finally:
        close_si(si)


def execute_netflow_rollback(vcenter_ip, username, password, disable_ssl=False):
    """Restore each ALLOWED_VDS switch's IPFIX config from the pre-change backup."""
    if not os.path.exists(BACKUP_PATH):
        logger.info("No NetFlow backup file found — nothing to roll back.")
        return {"restored": []}

    with open(BACKUP_PATH) as f:
        backups = json.load(f)

    si = get_service_instance(vcenter_ip, username, password, disable_ssl)
    restored = []
    try:
        content = si.RetrieveContent()
        for name, cfg in backups.items():
            vds = _find_vds(content, name)
            if not vds:
                continue
            spec = vim.dvs.VmwareDistributedVirtualSwitch.VmwareConfigSpec()
            spec.configVersion = vds.config.configVersion
            spec.ipfixConfig = vim.dvs.VmwareDistributedVirtualSwitch.IpfixConfig()
            spec.ipfixConfig.collectorIpAddress = cfg["collectorIpAddress"]
            spec.ipfixConfig.collectorPort = cfg["collectorPort"]
            spec.ipfixConfig.activeFlowTimeout = cfg["activeFlowTimeout"]
            spec.ipfixConfig.idleFlowTimeout = cfg["idleFlowTimeout"]
            spec.ipfixConfig.samplingRate = cfg["samplingRate"]
            task = vds.ReconfigureDvs_Task(spec)
            _wait_for_task(task)
            restored.append(name)
            logger.info(f"NetFlow config restored on vDS '{name}'")
        return {"restored": restored}
    finally:
        close_si(si)


def _wait_for_task(task):
    while task.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        time.sleep(0.5)
    if task.info.state == vim.TaskInfo.State.error:
        raise Exception(str(task.info.error))
