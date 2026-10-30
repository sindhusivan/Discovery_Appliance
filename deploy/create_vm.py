"""
TAO Discovery Appliance — build VM creator (SOP-DISC-001 Phase 1.1, step 3)

Creates the Photon OS build VM with the SOP's exact spec:
  4 vCPU (2 sockets x 2 cores), 8 GB RAM, 60 GB thin disk, VMXNET3 NIC,
  CD-ROM with the Photon ISO connected at power-on.

Also applies the Photon 5 fixes from the README:
  - CD-ROM on a SATA controller (IDE fails: "installation medium is not readable")
  - disk created with fileOperation="create" (else no backing file at power-on)

Optionally uploads the ISO to the datastore first (--upload-iso).

Usage:
  set DISCOVERY_VC_PASSWORD=...
  python deploy/create_vm.py --vcenter m1-vc.taodigital.lab --user sindhu@taodigital.lab \
      --host m1-esx02.taodigital.lab --datastore tao-vcf01-mgmt-cl01-ds-vsan01 \
      --pool "AIM - Migration" --folder Innovation --network "VLAN_107_10.1.107.0/24" \
      --iso-path "ISO/photon-minimal-5.0-dde71ec57.x86_64.iso" \
      --upload-iso C:\\path\\to\\photon-minimal-5.0-dde71ec57.x86_64.iso --power-on
"""

import argparse
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si


def parse_args():
    p = argparse.ArgumentParser(description="Create the Discovery Appliance build VM")
    p.add_argument("--vcenter", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--host", required=True, help="ESXi host to place the VM on")
    p.add_argument("--datastore", required=True)
    p.add_argument("--pool", required=True, help="Resource pool name")
    p.add_argument("--folder", help="VM folder path under the datacenter, e.g. Innovation (default: datacenter root)")
    p.add_argument("--network", required=True, help="Port group name ('/' or '%%2f' both accepted)")
    p.add_argument("--iso-path", required=True, help="ISO path inside the datastore, e.g. ISO/photon.iso")
    p.add_argument("--upload-iso", help="Local ISO to upload to --iso-path first (skipped if already there)")
    p.add_argument("--name", default="DiscoveryAppliance-Build-v1")
    p.add_argument("--cpus", type=int, default=4)
    p.add_argument("--cores-per-socket", type=int, default=2)
    p.add_argument("--memory-mb", type=int, default=8192)
    p.add_argument("--disk-gb", type=int, default=60)
    p.add_argument("--hw-version", default="vmx-14", help="vmx-14 = ESXi 6.7+, for widest OVA compatibility")
    p.add_argument("--power-on", action="store_true")
    return p.parse_args()


def find_by_name(content, vimtype, name):
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        for obj in view.view:
            if obj.name == name:
                return obj
    finally:
        view.Destroy()
    return None


def wait(task):
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        time.sleep(1)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(task.info.error.msg if task.info.error else "task failed")
    return task.info.result


def datacenter_of(obj):
    p = obj.parent
    while p and not isinstance(p, vim.Datacenter):
        p = p.parent
    return p


def ds_file_exists(datastore, ds_path):
    folder, fname = ds_path.rsplit("/", 1) if "/" in ds_path else ("", ds_path)
    spec = vim.host.DatastoreBrowser.SearchSpec(matchPattern=[fname])
    try:
        res = wait(datastore.browser.SearchDatastore_Task(f"[{datastore.name}] {folder}", spec))
        return bool(res.file)
    except Exception:
        return False


def ensure_ds_folder(si, content, datastore, dc, folder):
    """vSAN needs top-level folders created through the namespace manager."""
    if not folder or ds_file_exists(datastore, folder):
        return
    if datastore.summary.type == "vsan":
        content.datastoreNamespaceManager.CreateDirectory(datastore, folder)
    else:
        content.fileManager.MakeDirectory(f"[{datastore.name}] {folder}", dc, True)
    print(f"  created folder [{datastore.name}] {folder}")


def upload_file(si, vcenter, dc, datastore, local_path, ds_path):
    """HTTP PUT to vCenter's /folder endpoint, authenticated with the API session cookie."""
    params = urllib.parse.urlencode({"dcPath": dc.name, "dsName": datastore.name})
    url = f"https://{vcenter}/folder/{urllib.parse.quote(ds_path)}?{params}"
    cookie = si._stub.cookie.split(";")[0]  # name=value only, drop Path/HttpOnly attrs
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    size = os.path.getsize(local_path)
    with open(local_path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT", headers={
            "Cookie": cookie, "Content-Type": "application/octet-stream", "Content-Length": str(size)})
        print(f"  uploading {size / 2**20:.0f} MB -> [{datastore.name}] {ds_path}")
        with urllib.request.urlopen(req, context=ctx, timeout=3600) as resp:
            print(f"  upload HTTP {resp.status}")


def nic_backing(network):
    if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
        b = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
        b.port = vim.dvs.PortConnection(
            portgroupKey=network.key,
            switchUuid=network.config.distributedVirtualSwitch.uuid)
        return b
    if isinstance(network, vim.OpaqueNetwork):
        b = vim.vm.device.VirtualEthernetCard.OpaqueNetworkBackingInfo()
        b.opaqueNetworkId = network.summary.opaqueNetworkId
        b.opaqueNetworkType = network.summary.opaqueNetworkType
        return b
    b = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
    b.deviceName = network.name
    b.network = network
    return b


def build_config_spec(a, datastore, network):
    add = vim.vm.device.VirtualDeviceSpec.Operation.add
    devices = []

    scsi = vim.vm.device.ParaVirtualSCSIController(key=1000, busNumber=0,
             sharedBus=vim.vm.device.VirtualSCSIController.Sharing.noSharing)
    devices.append(vim.vm.device.VirtualDeviceSpec(operation=add, device=scsi))

    disk = vim.vm.device.VirtualDisk(key=2000, controllerKey=1000, unitNumber=0,
             capacityInKB=a.disk_gb * 1024 * 1024)
    disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
        diskMode="persistent", thinProvisioned=True, fileName=f"[{datastore.name}]")
    devices.append(vim.vm.device.VirtualDeviceSpec(operation=add, device=disk,
        fileOperation=vim.vm.device.VirtualDeviceSpec.FileOperation.create))

    sata = vim.vm.device.VirtualAHCIController(key=15000, busNumber=0)
    devices.append(vim.vm.device.VirtualDeviceSpec(operation=add, device=sata))

    cdrom = vim.vm.device.VirtualCdrom(key=3000, controllerKey=15000, unitNumber=0)
    cdrom.backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(
        fileName=f"[{datastore.name}] {a.iso_path}", datastore=datastore)
    cdrom.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
        startConnected=True, connected=True, allowGuestControl=True)
    devices.append(vim.vm.device.VirtualDeviceSpec(operation=add, device=cdrom))

    nic = vim.vm.device.VirtualVmxnet3(key=4000, backing=nic_backing(network), addressType="generated")
    nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(
        startConnected=True, connected=True, allowGuestControl=True)
    devices.append(vim.vm.device.VirtualDeviceSpec(operation=add, device=nic))

    return vim.vm.ConfigSpec(
        name=a.name,
        guestId="vmwarePhoton64Guest",
        version=a.hw_version,
        numCPUs=a.cpus,
        numCoresPerSocket=a.cores_per_socket,
        memoryMB=a.memory_mb,
        files=vim.vm.FileInfo(vmPathName=f"[{datastore.name}]"),
        deviceChange=devices,
        annotation="TAO Discovery Appliance build VM (SOP-DISC-001)",
    )


def main():
    a = parse_args()
    password = os.environ.get("DISCOVERY_VC_PASSWORD") or sys.exit("Set DISCOVERY_VC_PASSWORD")
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
        dc = datacenter_of(host)

        print(f"Datacenter {dc.name} | host {host.name} | pool {pool.name} | ds {datastore.name} | net {network.name}")

        if ds_file_exists(datastore, a.iso_path):
            print(f"  ISO already present: [{datastore.name}] {a.iso_path}")
        elif a.upload_iso:
            ensure_ds_folder(si, content, datastore, dc, a.iso_path.rsplit("/", 1)[0] if "/" in a.iso_path else "")
            upload_file(si, a.vcenter, dc, datastore, a.upload_iso, a.iso_path)
        else:
            print(f"  WARNING: ISO not yet at [{datastore.name}] {a.iso_path} — upload it before powering on")
            if a.power_on:
                sys.exit("Refusing to power on without the ISO")

        folder = dc.vmFolder
        for part in (a.folder or "").strip("/").split("/"):
            if part:
                folder = next((f for f in folder.childEntity if isinstance(f, vim.Folder) and f.name == part), None)
                if folder is None:
                    sys.exit(f"VM folder {a.folder!r} not found")

        print(f"Creating VM {a.name} in folder {a.folder or '(root)'} ...")
        vm = wait(folder.CreateVM_Task(config=build_config_spec(a, datastore, network), pool=pool, host=host))
        print(f"  created: {vm.name}  ({vm.config.instanceUuid})")

        if a.power_on:
            wait(vm.PowerOnVM_Task())
            print("  powered on — open the VM console in vCenter to run the Photon installer")
    finally:
        close_si(si)


if __name__ == "__main__":
    main()
