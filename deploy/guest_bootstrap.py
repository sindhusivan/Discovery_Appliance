"""
TAO Discovery Appliance — first-boot guest setup via VMware Tools

For build VMs on networks without DHCP: sets a static IP, hostname and DNS,
installs an SSH public key for root (key-only login) and starts sshd — all
through vSphere guest operations, so nothing has to be typed in the console.

The root password is read from a file (never from the command line or chat);
the file is not modified or printed.

Usage:
  set DISCOVERY_VC_PASSWORD=...
  python deploy/guest_bootstrap.py --vcenter m1-vc.taodigital.lab --user sindhu@taodigital.lab \
      --vm DiscoveryAppliance-Build-v1 --root-password-file %USERPROFILE%\\.tao_appliance_root \
      --ip 10.1.107.51/24 --gateway 10.1.107.1 --dns 172.16.1.100 \
      --pubkey %USERPROFILE%\\.ssh\\tao_discovery_ed25519.pub
"""

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si
from create_vm import find_by_name


def parse_args():
    p = argparse.ArgumentParser(description="Configure network + SSH inside the appliance via VMware Tools")
    p.add_argument("--vcenter", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--vm", required=True)
    p.add_argument("--root-password-file", required=True)
    p.add_argument("--ip", required=True, help="CIDR, e.g. 10.1.107.51/24")
    p.add_argument("--gateway", required=True)
    p.add_argument("--dns", required=True)
    p.add_argument("--hostname", default="discovery-appliance")
    p.add_argument("--pubkey", required=True)
    return p.parse_args()


def guest_script(a, pubkey):
    return f"""exec > /tmp/guest_bootstrap.log 2>&1
set -e
hostnamectl set-hostname {shlex.quote(a.hostname)}
cat > /etc/systemd/network/10-static-en.network <<'EOF'
[Match]
Name=e*

[Network]
Address={a.ip}
Gateway={a.gateway}
DNS={a.dns}
Domains=taodigital.lab
EOF
chmod 644 /etc/systemd/network/10-static-en.network
mkdir -p /root/.ssh && chmod 700 /root/.ssh
grep -qxF {shlex.quote(pubkey)} /root/.ssh/authorized_keys 2>/dev/null || echo {shlex.quote(pubkey)} >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
systemctl enable sshd
systemctl restart sshd
systemctl restart systemd-networkd
sleep 3
ip -4 -o addr show
"""


def main():
    a = parse_args()
    vc_pw = os.environ.get("DISCOVERY_VC_PASSWORD") or sys.exit("Set DISCOVERY_VC_PASSWORD")
    root_pw = Path(a.root_password_file).read_text(encoding="utf-8-sig").strip()
    pubkey = Path(a.pubkey).read_text().strip()

    si = get_service_instance(a.vcenter, a.user, vc_pw, True)
    try:
        content = si.RetrieveContent()
        vm = find_by_name(content, vim.VirtualMachine, a.vm) or sys.exit(f"VM {a.vm!r} not found")
        if vm.guest.toolsRunningStatus != "guestToolsRunning":
            sys.exit("VMware Tools not running in the guest")

        auth = vim.vm.guest.NamePasswordAuthentication(username="root", password=root_pw, interactiveSession=False)
        gom = content.guestOperationsManager
        pm = gom.processManager

        cmd = guest_script(a, pubkey)
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath="/bin/bash",
            arguments="-c " + shlex.quote(cmd))
        print(f"Running setup in {vm.name} as root ...")
        pid = pm.StartProgramInGuest(vm, auth, spec)
        for _ in range(60):
            info = pm.ListProcessesInGuest(vm, auth, [pid])[0]
            if info.endTime:
                break
            time.sleep(2)
        print(f"  exit code: {info.exitCode}")

        # Read the log back (guest file transfer goes straight to the ESXi host)
        ft = gom.fileManager.InitiateFileTransferFromGuest(vm, auth, "/tmp/guest_bootstrap.log")
        import ssl, urllib.request
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = ft.url.replace("*", vm.runtime.host.name)
        print(urllib.request.urlopen(url, context=ctx, timeout=60).read().decode(errors="replace"))
        if info.exitCode != 0:
            sys.exit(1)
    finally:
        close_si(si)


if __name__ == "__main__":
    main()
