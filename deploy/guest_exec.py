"""
Run a shell command inside the appliance through VMware Tools and print its output.
Break-glass access for when SSH is unavailable (no network, broken sshd).

Usage:
  set DISCOVERY_VC_PASSWORD=...
  python deploy/guest_exec.py --vcenter m1-vc.taodigital.lab --user sindhu@taodigital.lab \
      --vm DiscoveryAppliance-Build-v1 --root-password-file %USERPROFILE%\\.tao_appliance_root \
      "journalctl -u sshd -n 30 --no-pager"
"""

import argparse
import os
import shlex
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from pyVmomi import vim
from vcenter_connect import get_service_instance, close as close_si
from create_vm import find_by_name


def run_in_guest(content, vm, auth, command, timeout=300):
    """Run `command` with bash as root; returns (exit_code, combined output)."""
    gom = content.guestOperationsManager
    pm = gom.processManager
    log = f"/tmp/guest_exec_{int(time.time())}.log"
    script = f"exec > {log} 2>&1\n{command}"
    pid = pm.StartProgramInGuest(vm, auth, vim.vm.guest.ProcessManager.ProgramSpec(
        programPath="/bin/bash", arguments="-c " + shlex.quote(script)))
    deadline = time.time() + timeout
    while True:
        info = pm.ListProcessesInGuest(vm, auth, [pid])[0]
        if info.endTime or time.time() > deadline:
            break
        time.sleep(2)
    ft = gom.fileManager.InitiateFileTransferFromGuest(vm, auth, log)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    out = urllib.request.urlopen(ft.url.replace("*", vm.runtime.host.name), context=ctx, timeout=60).read()
    gom.fileManager.DeleteFileInGuest(vm, auth, log)
    return info.exitCode, out.decode(errors="replace")


def main():
    p = argparse.ArgumentParser(description="Run a command in the appliance via VMware Tools")
    p.add_argument("--vcenter", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--vm", required=True)
    p.add_argument("--root-password-file", required=True)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("command")
    a = p.parse_args()

    vc_pw = os.environ.get("DISCOVERY_VC_PASSWORD") or sys.exit("Set DISCOVERY_VC_PASSWORD")
    root_pw = Path(a.root_password_file).read_text(encoding="utf-8-sig").strip()
    si = get_service_instance(a.vcenter, a.user, vc_pw, True)
    try:
        content = si.RetrieveContent()
        vm = find_by_name(content, vim.VirtualMachine, a.vm) or sys.exit(f"VM {a.vm!r} not found")
        auth = vim.vm.guest.NamePasswordAuthentication(username="root", password=root_pw, interactiveSession=False)
        code, out = run_in_guest(content, vm, auth, a.command, a.timeout)
        print(out, end="")
        print(f"[exit {code}]")
        sys.exit(0 if code == 0 else 1)
    finally:
        close_si(si)


if __name__ == "__main__":
    main()
