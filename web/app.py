import sys, os, secrets, hmac, logging, threading, time, subprocess, sqlite3
from datetime import timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, session

BASE_DIR = Path(__file__).resolve().parent.parent
ENGINE_DIR = BASE_DIR / "engine"
COLLECTOR_DIR = BASE_DIR / "collector"
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
LOG_DIR = BASE_DIR / "logs"

sys.path.insert(0, str(ENGINE_DIR))
from inventory_collector import run_full_discovery
from report_generator import generate_enterprise_excel
from netflow_manager import preflight_check_and_backup, execute_netflow_override, execute_netflow_rollback
from db_init import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("discovery-app")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("DISCOVERY_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=8)
DB_PATH = os.environ.get("DISCOVERY_DB_PATH", str(DATA_DIR / "discovery.db"))
init_db(DB_PATH)

ADMIN_USERNAME = os.environ.get("DISCOVERY_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("DISCOVERY_ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    # No known default: generate a one-off password so a fresh appliance is never
    # reachable with "changeme". deploy/install.sh sets a persistent one.
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    logger.warning(f"DISCOVERY_ADMIN_PASSWORD not set; temporary admin password: {ADMIN_PASSWORD}")

discovery_state = {
    "status": "Idle", "progress": 0,
    "running": False, "error": None
}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def vcenter_required(f):
    """Gates anything that shows discovered data behind an actual vCenter
    connection made THIS session (set in api_preflight on success) — not just
    whether the appliance has ever discovered anything historically."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        if not session.get("vcenter_connected"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "No vCenter connection established this session"}), 403
            return redirect("/config")
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        user_ok = hmac.compare_digest(request.form.get("username", "").encode(), ADMIN_USERNAME.encode())
        pw_ok = hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode())
        if user_ok and pw_ok:
            session.permanent = True
            session["logged_in"] = True
            return redirect("/")
        error = "Invalid username or password"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ─────────────────────────────────────────────────────────────
#  PAGE ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def root():
    return redirect('/dashboard' if session.get('vcenter_connected') else '/config')

@app.route("/config")
@login_required
def config_page(): return render_template("config.html")

@app.route("/dashboard")
@vcenter_required
def dashboard_page(): return render_template("dashboard.html")

@app.route("/dependencies")
@vcenter_required
def dependencies_page(): return render_template("dependencies.html")

@app.route("/status")
@vcenter_required
def status_page(): return render_template("status.html")

@app.route("/export")
@vcenter_required
def export_page(): return render_template("export.html")

# ─────────────────────────────────────────────────────────────
#  API: SYSTEM
# ─────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status(): return jsonify(discovery_state)

@app.route("/api/db_counts")
@vcenter_required
def api_db_counts():
    try:
        conn = sqlite3.connect(DB_PATH)
        vms   = conn.execute("SELECT COUNT(DISTINCT vm_name) FROM virtual_machines").fetchone()[0]
        deps  = conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0]
        hosts = conn.execute("SELECT COUNT(*) FROM esxi_hosts").fetchone()[0]
        ds    = conn.execute("SELECT COUNT(*) FROM datastores").fetchone()[0]
        conn.close()
        return jsonify({"vms": vms, "deps": deps, "hosts": hosts, "datastores": ds})
    except Exception as e:
        return jsonify({"vms": 0, "deps": 0, "hosts": 0, "datastores": 0})

# ─────────────────────────────────────────────────────────────
#  API: ENVIRONMENT TREE
# ─────────────────────────────────────────────────────────────

@app.route("/api/tree")
@vcenter_required
def api_tree():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        dcs = conn.execute("SELECT DISTINCT name FROM datacenters ORDER BY name").fetchall()

        clusters = conn.execute("""
            SELECT cluster_name, datacenter_name, drs_enabled, drs_behavior,
                   ha_enabled, total_hosts
            FROM clusters ORDER BY cluster_name
        """).fetchall()

        hosts = conn.execute("""
            SELECT hostname, cluster_name, ip_address, vendor, model, cpu_model,
                   cpu_sockets, cpu_cores_per_socket, total_cpu_cores, ram_gb,
                   esxi_version, esxi_build, connection_state, power_state,
                   num_nics, num_hbas
            FROM esxi_hosts ORDER BY hostname
        """).fetchall()

        # Use actual column names from DB
        vms = conn.execute("""
            SELECT vm_name, vm_uuid, power_state,
                   configured_os AS guest_os, running_os, vcpu, ram_gb,
                   num_disks, disk_sizes_gb, provisioned_space_gb, used_space_gb,
                   ip_address, mac_address, portgroup, host_name, cluster_name,
                   vcenter_tags, vmware_tools_status, vmware_tools_version
            FROM virtual_machines ORDER BY vm_name
        """).fetchall()

        conn.close()

        vm_by_host = {}
        for vm in vms:
            v = dict(vm)
            vm_by_host.setdefault(v['host_name'], []).append(v)

        host_by_cluster = {}
        for h in hosts:
            hd = dict(h)
            hd['vms'] = vm_by_host.get(hd['hostname'], [])
            host_by_cluster.setdefault(hd['cluster_name'], []).append(hd)

        cluster_by_dc = {}
        for cl in clusters:
            cd = dict(cl)
            cd['drs_enabled'] = bool(cd['drs_enabled'])
            cd['ha_enabled']  = bool(cd['ha_enabled'])
            cd['hosts']       = host_by_cluster.get(cd['cluster_name'], [])
            cluster_by_dc.setdefault(cd['datacenter_name'], []).append(cd)

        # Fallback: if no DCs in table but clusters exist, synthesise DC from cluster data
        if not dcs and cluster_by_dc:
            tree = {"datacenters": [{"name": dc_name, "clusters": cls}
                                    for dc_name, cls in cluster_by_dc.items()]}
        else:
            tree = {"datacenters": [{"name": dc['name'],
                                     "clusters": cluster_by_dc.get(dc['name'], [])}
                                    for dc in dcs]}

        return jsonify(tree)

    except Exception as e:
        logger.error(f"Tree API error: {e}")
        import traceback
        return jsonify({"datacenters": [], "error": str(e), "trace": traceback.format_exc()})

# ─────────────────────────────────────────────────────────────
#  API: DEPENDENCY GRAPH
# ─────────────────────────────────────────────────────────────

@app.route("/api/graph")
@vcenter_required
def api_graph():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        deps = conn.execute("""
            SELECT src_ip, src_vm_name, dst_ip, dst_vm_name,
                   dst_port, protocol, flow_count, first_seen, last_seen
            FROM dependencies ORDER BY flow_count DESC
        """).fetchall()

        # Use correct column names
        vms = conn.execute("""
            SELECT vm_name, ip_address, power_state, configured_os, running_os,
                   vcpu, ram_gb, num_disks, disk_sizes_gb, provisioned_space_gb,
                   used_space_gb, mac_address, portgroup, host_name, cluster_name,
                   vcenter_tags, vmware_tools_status, hw_version
            FROM virtual_machines
        """).fetchall()

        conn.close()

        vm_map = {}
        for vm in vms:
            v = dict(vm)
            vm_map[v['vm_name']] = v
            if v['ip_address']:
                for ip in v['ip_address'].split(','):
                    ip = ip.strip()
                    if ip:
                        vm_map[ip] = v

        node_ids = set()
        nodes_raw = {}
        links = []

        for dep in deps:
            d = dict(dep)
            src_name = d['src_vm_name'] or d['src_ip']
            dst_name = d['dst_vm_name'] or d['dst_ip']
            src_info = vm_map.get(src_name) or vm_map.get(d['src_ip']) or {}
            dst_info = vm_map.get(dst_name) or vm_map.get(d['dst_ip']) or {}

            for name, ip, info in [(src_name, d['src_ip'], src_info),
                                   (dst_name, d['dst_ip'], dst_info)]:
                if name not in node_ids:
                    node_ids.add(name)
                    nodes_raw[name] = {
                        "id": name, "name": name, "ip": ip,
                        "power_state": info.get("power_state", "unknown"),
                        "guest_os": info.get("configured_os", ""),
                        "configured_os": info.get("configured_os", ""),
                        "vcpu": info.get("vcpu"),
                        "ram_gb": info.get("ram_gb"),
                        "host_name": info.get("host_name", ""),
                        "cluster_name": info.get("cluster_name", ""),
                    }

            links.append({
                "source": src_name, "target": dst_name,
                "source_id": src_name, "target_id": dst_name,
                "port": d['dst_port'], "protocol": d['protocol'] or 'TCP',
                "flow_count": d['flow_count'] or 1,
                "first_seen": d['first_seen'], "last_seen": d['last_seen']
            })

        return jsonify({"nodes": list(nodes_raw.values()), "links": links})

    except Exception as e:
        logger.error(f"Graph API error: {e}")
        return jsonify({"nodes": [], "links": [], "error": str(e)})

# ─────────────────────────────────────────────────────────────
#  API: DEPENDENCIES LIST
# ─────────────────────────────────────────────────────────────

@app.route("/api/dependencies")
@vcenter_required
def api_dependencies():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT src_ip, src_vm_name, dst_ip, dst_vm_name,
                   dst_port, protocol, flow_count, first_seen, last_seen
            FROM dependencies ORDER BY flow_count DESC LIMIT 100
        """).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0]
        conn.close()
        deps = [{"src_ip": r[0], "src_vm_name": r[1], "dst_ip": r[2],
                 "dst_vm_name": r[3], "dst_port": r[4], "protocol": r[5],
                 "flow_count": r[6], "first_seen": r[7], "last_seen": r[8]}
                for r in rows]
        return jsonify({"deps": deps, "total": total})
    except Exception as e:
        return jsonify({"deps": [], "total": 0, "error": str(e)})

# ─────────────────────────────────────────────────────────────
#  API: VM PREVIEW
# ─────────────────────────────────────────────────────────────

@app.route("/api/vm_preview")
@vcenter_required
def api_vm_preview():
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT vm_name, power_state, configured_os AS guest_os, vcpu, ram_gb, ip_address
            FROM virtual_machines ORDER BY power_state DESC, vm_name LIMIT 100
        """).fetchall()
        conn.close()
        return jsonify({"vms": [{"vm_name": r[0], "power_state": r[1], "guest_os": r[2],
                                  "vcpu": r[3], "ram_gb": r[4], "ip_address": r[5]}
                                 for r in rows]})
    except Exception as e:
        return jsonify({"vms": [], "error": str(e)})

# ─────────────────────────────────────────────────────────────
#  API: PRE-FLIGHT & DISCOVERY
# ─────────────────────────────────────────────────────────────

@app.route("/api/preflight", methods=["POST"])
@login_required
def api_preflight():
    data = request.json
    try:
        report = preflight_check_and_backup(
            data.get("vcenter_ip"), data.get("username"),
            data.get("password"), data.get("disable_ssl", False))
        session["vcenter_connected"] = True
        return jsonify({"status": "success", "report": report})
    except Exception as e:
        logger.error(f"Preflight error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/start_discovery", methods=["POST"])
@login_required
def api_start_discovery():
    global discovery_state
    if discovery_state["running"]:
        return jsonify({"error": "Discovery already running"}), 400
    data = request.json
    if not data.get("approved", False):
        return jsonify({"error": "Pre-flight approval required"}), 400
    discovery_state = {"status": "Initialising...", "progress": 5,
                       "running": True, "error": None}
    thread = threading.Thread(
        target=discovery_worker,
        args=(data.get("vcenter_ip"), data.get("username"),
              data.get("password"), data.get("disable_ssl", False)))
    thread.daemon = True
    thread.start()
    return jsonify({"message": "Discovery started successfully"})

def discovery_worker(host, user, password, disable_ssl):
    global discovery_state
    listener_proc = None
    try:
        discovery_state.update({"status": "Warming up NetFlow listener...", "progress": 15})
        try:
            listener_proc = subprocess.Popen(
                [sys.executable, str(COLLECTOR_DIR / "ipfix_listener.py")])
            time.sleep(3)
        except Exception as e:
            logger.warning(f"Inline listener warning: {e}")

        discovery_state.update({"status": "Configuring network flows...", "progress": 25})
        try:
            execute_netflow_override(host, user, password, disable_ssl)
        except Exception as e:
            logger.warning(f"NetFlow override: {e}")

        discovery_state.update({"status": "Collecting VM inventory & tags...", "progress": 45})
        run_full_discovery(host, user, password, disable_ssl)

        discovery_state.update({"status": "Analysing application flows...", "progress": 70})
        time.sleep(30)

        discovery_state.update({"status": "Finalising...", "progress": 90})
        time.sleep(5)

        discovery_state.update({"status": "Completed!", "progress": 100, "running": False})
        logger.info("Discovery completed successfully")

    except Exception as e:
        logger.error(f"Discovery failed: {e}")
        discovery_state.update({"status": "Failed", "progress": 0,
                                 "running": False, "error": str(e)})
    finally:
        if listener_proc:
            try: listener_proc.terminate()
            except: pass

# ─────────────────────────────────────────────────────────────
#  API: REPORTS
# ─────────────────────────────────────────────────────────────

@app.route("/api/generate_reports", methods=["POST"])
@vcenter_required
def api_generate_reports():
    try:
        excel_path = generate_enterprise_excel()
        filename = os.path.basename(excel_path)
        return jsonify({"download_url": f"/download/{filename}",
                        "message": "Success", "filename": filename})
    except Exception as e:
        logger.error(f"Report error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/download/<filename>")
@vcenter_required
def download_file(filename):
    return send_from_directory(str(REPORTS_DIR), filename, as_attachment=True)

# ─────────────────────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=False)
