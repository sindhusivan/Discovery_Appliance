"""
TAO Discovery Appliance — deploy the build VM from VMware's Photon OS OVA

Alternative to create_vm.py for vCenters where the account cannot upload files
to a datastore (no ISO). OVA import streams the disk straight to the ESXi host
under an HttpNfcLease, which only needs the vApp-import privilege on the
resource pool and VM folder.

After import, the VM is resized to the SOP-DISC-001 spec:
  4 vCPU (2 sockets x 2 cores), 8 GB RAM, disk grown to 60 GB, VMXNET3 on --network.
Photon's root filesystem must then be grown inside the guest (see deploy/README).

Usage:
  set DISCOVERY_VC_PASSWORD=...
  python deploy/import_ova.py --vcenter m1-vc.taodigital.lab --user sindhu@taodigital.lab \
      --host m1-esx02.taodigital.lab --datastore tao-vcf01-mgmt-cl01-ds-vsan01 \
      --pool "AIM - Migration" --folder Innovation --network "VLAN_107_10.1.107.0/24" \
      --ova photon-hw15-5.0-dde71ec57.x86_64.ova --name DiscoveryAppliance-Build-v1
"""

import argparse
import http.client
import os
import ssl
import sys
import tarfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si
from create_vm import find_by_name, wait, datacenter_of


def parse_args():
    p = argparse.ArgumentParser(description="Deploy the Discovery Appliance build VM from an OVA")
    p.add_argument("--vcenter", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--datastore", required=True)
    p.add_argument("--pool", required=True)
    p.add_argument("--folder", help="VM folder path under the datacenter, e.g. Innovation")
    p.add_argument("--network", required=True)
    p.add_argument("--ova", required=True)
    p.add_argument("--name", default="DiscoveryAppliance-Build-v1")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--cores-per-socket", type=int, default=2)
    p.add_argument("--memory-mb", type=int, default=8192)
    p.add_argument("--disk-gb", type=int, default=60)
    p.add_argument("--power-on", action="store_true")
    return p.parse_args()


def resolve_folder(dc, path):
    folder = dc.vmFolder
    for part in (path or "").strip("/").split("/"):
        if part:
            folder = next((f for f in folder.childEntity if isinstance(f, vim.Folder) and f.name == part), None)
            if folder is None:
                sys.exit(f"VM folder {path!r} not found")
    return folder


class LeaseKeepAlive(threading.Thread):
    """vCenter aborts an idle lease after ~5 min; report progress every 20 s."""
    def __init__(self, lease, total):
        super().__init__(daemon=True)
        self.lease, self.total, self.sent, self.stop = lease, total, 0, threading.Event()

    def run(self):
        while not self.stop.wait(20):
            pct = int(self.sent * 100 / self.total) if self.total else 0
            try:
                self.lease.HttpNfcLeaseProgress(min(pct, 99))
            except Exception:
                return


def upload_disk(url, member_file, size, keepalive, create):
    u = urllib.parse.urlparse(url)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, context=ctx, timeout=600)
    conn.putrequest("PUT" if create else "POST", u.path + (f"?{u.query}" if u.query else ""))
    conn.putheader("Content-Type", "application/x-vnd.vmware-streamVmdk")
    conn.putheader("Content-Length", str(size))
    conn.putheader("Connection", "Keep-Alive")
    conn.endheaders()
    last = 0
    while True:
        chunk = member_file.read(1024 * 1024)
        if not chunk:
            break
        conn.send(chunk)
        keepalive.sent += len(chunk)
        pct = keepalive.sent * 100 // keepalive.total
        if pct >= last + 10:
            print(f"    {pct}%")
            last = pct
    resp = conn.getresponse()
    if resp.status not in (200, 201):
        raise RuntimeError(f"disk upload HTTP {resp.status}: {resp.read(300)!r}")


def main():
    a = parse_args()
    password = os.environ.get("DISCOVERY_VC_PASSWORD") or sys.exit("Set DISCOVERY_VC_PASSWORD")
    ova = tarfile.open(a.ova)
    ovf_member = next(m for m in ova.getmembers() if m.name.endswith(".ovf"))
    ovf_xml = ova.extractfile(ovf_member).read().decode()

    si = get_service_instance(a.vcenter, a.user, password, True)
    try:
        content = si.RetrieveContent()
        host = find_by_name(content, vim.HostSystem, a.host)
        datastore = find_by_name(content, vim.Datastore, a.datastore)
        pool = find_by_name(content, vim.ResourcePool, a.pool)
        net_name = a.network.replace("/", "%2f")
        network = next((n for n in host.network if n.name in (a.network, net_name)), None) if host else None
        missing = [k for k, v in {"host": host, "datastore": datastore, "pool": pool, "network": network}.items() if not v]
        if missing:
            sys.exit(f"Not found: {missing}")
        if find_by_name(content, vim.VirtualMachine, a.name):
            sys.exit(f"A VM named {a.name!r} already exists — refusing to overwrite")
        folder = resolve_folder(datacenter_of(host), a.folder)

        # Map every network the OVF declares onto the target port group
        desc = content.ovfManager.ParseDescriptor(ovf_xml, vim.OvfManager.ParseDescriptorParams())
        mapping = [vim.OvfManager.NetworkMapping(name=n.name, network=network) for n in (desc.network or [])]
        params = vim.OvfManager.CreateImportSpecParams(
            entityName=a.name, diskProvisioning="thin", hostSystem=host, networkMapping=mapping)
        spec = content.ovfManager.CreateImportSpec(ovf_xml, pool, datastore, params)
        if spec.error:
            sys.exit("Import spec errors: " + "; ".join(e.msg for e in spec.error))
        for w in spec.warning or []:
            print("  warning:", w.msg)

        print(f"Importing {Path(a.ova).name} as {a.name} -> {host.name} / {pool.name} / {a.folder or '(root)'}")
        lease = pool.ImportVApp(spec.importSpec, folder, host)
        while lease.state == vim.HttpNfcLease.State.initializing:
            time.sleep(1)
        if lease.state == vim.HttpNfcLease.State.error:
            sys.exit(f"Lease error: {lease.error.msg}")

        files = {fi.deviceId: fi for fi in spec.fileItem}
        total = sum(ova.getmember(fi.path).size for fi in spec.fileItem)
        keepalive = LeaseKeepAlive(lease, total)
        keepalive.start()
        try:
            for dev in lease.info.deviceUrl:
                fi = files.get(dev.importKey)
                if not fi:
                    continue
                member = ova.getmember(fi.path)
                url = dev.url.replace("*", host.name)
                print(f"  uploading {fi.path} ({member.size / 2**20:.0f} MB)")
                upload_disk(url, ova.extractfile(member), member.size, keepalive, fi.create)
            lease.HttpNfcLeaseComplete()
        except Exception as e:
            lease.HttpNfcLeaseAbort(vim.LocalizedMethodFault(localizedMessage=str(e)))
            raise
        finally:
            keepalive.stop.set()

        vm = lease.info.entity
        print(f"  imported: {vm.name}")

        # Resize to the SOP spec
        edit = vim.vm.device.VirtualDeviceSpec.Operation.edit
        changes = []
        for d in vm.config.hardware.device:
            if isinstance(d, vim.vm.device.VirtualDisk) and d.capacityInKB < a.disk_gb * 1024 * 1024:
                d.capacityInKB = a.disk_gb * 1024 * 1024
                changes.append(vim.vm.device.VirtualDeviceSpec(operation=edit, device=d))
                break
        wait(vm.ReconfigVM_Task(vim.vm.ConfigSpec(
            numCPUs=a.cpus, numCoresPerSocket=a.cores_per_socket, memoryMB=a.memory_mb,
            deviceChange=changes, annotation="TAO Discovery Appliance build VM (SOP-DISC-001, from Photon OVA)")))
        print(f"  resized: {a.cpus} vCPU ({a.cores_per_socket}/socket), {a.memory_mb} MB, disk {a.disk_gb} GB")

        if a.power_on:
            wait(vm.PowerOnVM_Task())
            print("  powered on")
    finally:
        close_si(si)


if __name__ == "__main__":
    main()
