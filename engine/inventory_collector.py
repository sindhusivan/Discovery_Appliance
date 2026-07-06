"""
TAO Discovery Platform — Inventory Collector
Connects to vCenter via pyVmomi (read-only) and collects full environment inventory.
Key fix: reads vmk0 IP from host.config.network.vnic — never uses managementServerIp.
"""

import sqlite3, os, logging
from datetime import datetime
from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si

logger = logging.getLogger("inventory_collector")
DB_PATH = "/opt/discovery-appliance/data/discovery.db"

def get_host_management_ip(host):
    """
    Read the true ESXi management IP from vmk0.
    Falls back to first non-link-local vnic, then empty string.
    Never uses summary.managementServerIp which returns the vCenter IP.
    """
    try:
        vnics = host.config.network.vnic
        # Prefer vmk0
        for vnic in vnics:
            if vnic.device == 'vmk0':
                ip = vnic.spec.ip.ipAddress
                if ip and not ip.startswith('169.254') and not ip.startswith('0.'):
                    return ip
        # Fall back to first non-link-local
        for vnic in vnics:
            ip = vnic.spec.ip.ipAddress
            if ip and not ip.startswith('169.254') and not ip.startswith('0.'):
                return ip
    except Exception as e:
        logger.warning(f"Could not read vmk0 IP: {e}")
    return ''

# ─────────────────────────────────────────────────────────────
#  MAIN DISCOVERY ENTRY POINT
# ─────────────────────────────────────────────────────────────

def run_full_discovery(vcenter_host, username, password, disable_ssl=True):
    logger.info(f"Starting discovery against {vcenter_host}")
    si = get_service_instance(vcenter_host, username, password, disable_ssl)
    try:
        content = si.RetrieveContent()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        _collect_datacenters(content, conn, ts)
        _collect_clusters(content, conn, ts)
        _collect_hosts(content, conn, ts)
        _collect_vms(content, conn, ts)
        _collect_datastores(content, conn, ts)

        conn.commit()
        conn.close()
        logger.info("Discovery completed successfully")
    finally:
        close_si(si)

# ─────────────────────────────────────────────────────────────
#  COLLECTORS
# ─────────────────────────────────────────────────────────────

def _collect_datacenters(content, conn, ts):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datacenter], True)
    conn.execute("DELETE FROM datacenters")
    for dc in container.view:
        conn.execute("INSERT OR IGNORE INTO datacenters (name) VALUES (?)", (dc.name,))
        logger.info(f"  DC: {dc.name}")
    container.Destroy()

def _collect_clusters(content, conn, ts):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True)
    conn.execute("DELETE FROM clusters")
    for cl in container.view:
        try:
            dc_name = _get_dc_name(cl)
            drs = cl.configuration.drsConfig.enabled if cl.configuration.drsConfig else False
            drs_beh = str(cl.configuration.drsConfig.defaultVmBehavior) if drs else ''
            ha  = cl.configuration.dasConfig.enabled if cl.configuration.dasConfig else False
            conn.execute("""
                INSERT OR REPLACE INTO clusters
                  (cluster_name, datacenter_name, drs_enabled, drs_behavior, ha_enabled, total_hosts)
                VALUES (?,?,?,?,?,?)
            """, (cl.name, dc_name, int(drs), drs_beh, int(ha), len(cl.host)))
            logger.info(f"  Cluster: {cl.name} (DC: {dc_name})")
        except Exception as e:
            logger.warning(f"  Cluster error {cl.name}: {e}")
    container.Destroy()

def _collect_hosts(content, conn, ts):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True)
    conn.execute("DELETE FROM esxi_hosts")
    for host in container.view:
        try:
            summary  = host.summary
            hw       = host.hardware
            cluster_name = host.parent.name if hasattr(host.parent, 'name') else ''

            # Use vmk0 IP — never vCenter's managementServerIp
            ip_address = get_host_management_ip(host)

            cpu_model = ''
            try:
                cpu_model = hw.cpuPkg[0].description if hw.cpuPkg else ''
            except: pass

            conn.execute("""
                INSERT OR REPLACE INTO esxi_hosts
                  (hostname, cluster_name, ip_address, vendor, model, cpu_model,
                   cpu_sockets, cpu_cores_per_socket, total_cpu_cores, ram_gb,
                   esxi_version, esxi_build, connection_state, power_state,
                   num_nics, num_hbas, scan_timestamp)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                host.name, cluster_name, ip_address,
                hw.systemInfo.vendor or '',
                hw.systemInfo.model or '',
                cpu_model,
                hw.cpuInfo.numCpuPackages or 0,
                hw.cpuInfo.numCpuCores // hw.cpuInfo.numCpuPackages
                    if hw.cpuInfo.numCpuPackages else 0,
                hw.cpuInfo.numCpuCores or 0,
                round(hw.memorySize / (1024**3), 2) if hw.memorySize else 0,
                summary.config.product.version if summary.config else '',
                summary.config.product.build if summary.config else '',
                str(summary.runtime.connectionState) if summary.runtime else '',
                str(summary.runtime.powerState) if summary.runtime else '',
                len(host.config.network.pnic) if host.config and host.config.network else 0,
                len(host.config.storageDevice.hostBusAdapter)
                    if host.config and host.config.storageDevice else 0,
                ts
            ))
            logger.info(f"  Host: {host.name} → {ip_address}")
        except Exception as e:
            logger.warning(f"  Host error {host.name}: {e}")
    container.Destroy()

def _collect_vms(content, conn, ts):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True)
    conn.execute("DELETE FROM virtual_machines")
    for vm in container.view:
        try:
            summary  = vm.summary
            config   = vm.config
            guest    = vm.guest
            runtime  = vm.runtime
            if not config:
                continue

            # IPs — collect all from guest NICs
            all_ips = []
            primary_ip = ''
            mac_address = ''
            portgroup   = ''

            if guest and guest.net:
                for nic in guest.net:
                    for ip in (nic.ipAddress or []):
                        if ':' not in ip and not ip.startswith('169.254'):
                            all_ips.append(ip)
                    if not mac_address and nic.macAddress:
                        mac_address = nic.macAddress

            if not all_ips and summary.guest and summary.guest.ipAddress:
                all_ips.append(summary.guest.ipAddress)

            primary_ip = all_ips[0] if all_ips else ''
            ip_str     = ', '.join(all_ips)

            # Port group
            try:
                for dev in config.hardware.device:
                    if hasattr(dev, 'backing') and hasattr(dev.backing, 'network'):
                        portgroup = dev.deviceInfo.summary or ''
                        if not mac_address and hasattr(dev, 'macAddress'):
                            mac_address = dev.macAddress
                        break
                    elif hasattr(dev, 'backing') and hasattr(dev.backing, 'port'):
                        portgroup = getattr(dev.backing.port, 'portgroupKey', '') or \
                                    getattr(dev.deviceInfo, 'summary', '') or ''
                        if not mac_address and hasattr(dev, 'macAddress'):
                            mac_address = dev.macAddress
                        break
            except: pass

            # Disk info
            disk_sizes = []
            try:
                for dev in config.hardware.device:
                    if isinstance(dev, vim.vm.device.VirtualDisk):
                        disk_sizes.append(round(dev.capacityInBytes / (1024**3), 2))
            except: pass
            disk_sizes_str = ', '.join(str(s) for s in disk_sizes)
            provisioned_gb = round(summary.storage.committed / (1024**3), 2) \
                if summary.storage and summary.storage.committed else 0
            used_gb = round((summary.storage.committed - summary.storage.uncommitted) / (1024**3), 2) \
                if summary.storage and summary.storage.uncommitted else 0

            # Host & cluster
            host_name    = runtime.host.name if runtime and runtime.host else ''
            cluster_name = runtime.host.parent.name \
                if runtime and runtime.host and hasattr(runtime.host.parent, 'name') else ''

            # VMware Tools
            tools_status  = str(guest.toolsStatus) if guest else ''
            tools_version = str(guest.toolsVersion) if guest and guest.toolsVersion else ''

            # Tags (vCenter tags require separate API call — skip for now, use annotation)
            tags = config.annotation or ''

            conn.execute("""
                INSERT OR REPLACE INTO virtual_machines
                  (vm_name, vm_uuid, power_state, configured_os, running_os,
                   guest_hostname, vcpu, ram_gb, cpu_usage_mhz, mem_usage_mb,
                   hw_version, num_disks, disk_sizes_gb, provisioned_space_gb,
                   used_space_gb, ip_address, mac_address, portgroup,
                   host_name, cluster_name, vcenter_tags,
                   vmware_tools_status, vmware_tools_version, scan_timestamp)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                vm.name,
                config.uuid,
                str(runtime.powerState) if runtime else 'unknown',
                config.guestFullName or '',
                guest.guestFullName if guest else '',
                guest.hostName if guest else '',
                config.hardware.numCPU or 0,
                round(config.hardware.memoryMB / 1024, 2) if config.hardware.memoryMB else 0,
                summary.quickStats.overallCpuUsage or 0,
                summary.quickStats.guestMemoryUsage or 0,
                config.version or '',
                len(disk_sizes),
                disk_sizes_str,
                provisioned_gb,
                used_gb,
                ip_str,
                mac_address,
                portgroup,
                host_name,
                cluster_name,
                tags,
                tools_status,
                tools_version,
                ts
            ))
        except Exception as e:
            logger.warning(f"  VM error {vm.name}: {e}")
    container.Destroy()
    logger.info(f"  VMs collected")

def _collect_datastores(content, conn, ts):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True)
    conn.execute("DELETE FROM datastores")
    for ds in container.view:
        try:
            info = ds.info
            summary = ds.summary
            conn.execute("""
                INSERT OR REPLACE INTO datastores
                  (name, capacity_gb, free_gb, datastore_type, accessible, scan_timestamp)
                VALUES (?,?,?,?,?,?)
            """, (
                ds.name,
                round(summary.capacity / (1024**3), 2) if summary.capacity else 0,
                round(summary.freeSpace / (1024**3), 2) if summary.freeSpace else 0,
                summary.type or '',
                int(summary.accessible),
                ts
            ))
        except Exception as e:
            logger.warning(f"  Datastore error {ds.name}: {e}")
    container.Destroy()

def _get_dc_name(obj):
    """Walk up the vSphere object tree to find the parent datacenter name."""
    parent = obj.parent
    while parent:
        if isinstance(parent, vim.Datacenter):
            return parent.name
        parent = getattr(parent, 'parent', None)
    return 'Unknown'
