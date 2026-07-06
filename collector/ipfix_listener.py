"""
TAO Discovery Platform — IPFIX Flow Listener
UDP listener on port 4739 decoding IPFIX (RFC 7011) packets exported by
vSphere Distributed Switches and recording VM-to-VM flow summaries.

Only decodes the field types vDS actually exports for this use case:
sourceIPv4Address(8), destinationIPv4Address(12), sourceTransportPort(7),
destinationTransportPort(11), protocolIdentifier(4). Anything else in a
template is skipped (length still consumed correctly).
"""

import socket
import struct
import sqlite3
import logging
import os
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ipfix-listener")

LISTEN_PORT = 4739
DB_PATH = "/opt/discovery-appliance/data/discovery.db"
LOG_PATH = "/opt/discovery-appliance/logs/ipfix-listener.log"

# IPFIX Information Element IDs we care about (RFC 5102 / IANA IPFIX registry)
IE_SRC_IPV4 = 8
IE_DST_IPV4 = 12
IE_SRC_PORT = 7
IE_DST_PORT = 11
IE_PROTOCOL = 4

PROTO_NAMES = {6: "TCP", 17: "UDP", 1: "ICMP"}

# templateId -> list of (informationElementId, fieldLength)
_templates = {}


def _setup_file_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)


def _parse_template_set(data):
    """Parse a Template Set (Set ID 2) and cache field layouts."""
    offset = 0
    while offset + 4 <= len(data):
        template_id, field_count = struct.unpack_from("!HH", data, offset)
        offset += 4
        fields = []
        for _ in range(field_count):
            if offset + 4 > len(data):
                break
            ie_id, ie_len = struct.unpack_from("!HH", data, offset)
            offset += 4
            if ie_id & 0x8000:
                offset += 4  # enterprise-specific field, skip enterprise number
            fields.append((ie_id & 0x7FFF, ie_len))
        _templates[template_id] = fields
        logger.debug(f"Template {template_id} cached with {len(fields)} fields")


def _parse_data_set(set_id, data, conn):
    fields = _templates.get(set_id)
    if not fields:
        return  # data set arrived before its template — drop silently

    record_len = sum(length for _, length in fields)
    if record_len == 0:
        return

    offset = 0
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    while offset + record_len <= len(data):
        record = {}
        pos = offset
        for ie_id, length in fields:
            raw = data[pos:pos + length]
            pos += length
            if ie_id == IE_SRC_IPV4 and length == 4:
                record["src_ip"] = socket.inet_ntoa(raw)
            elif ie_id == IE_DST_IPV4 and length == 4:
                record["dst_ip"] = socket.inet_ntoa(raw)
            elif ie_id == IE_SRC_PORT and length == 2:
                record["src_port"] = struct.unpack("!H", raw)[0]
            elif ie_id == IE_DST_PORT and length == 2:
                record["dst_port"] = struct.unpack("!H", raw)[0]
            elif ie_id == IE_PROTOCOL and length == 1:
                record["protocol"] = PROTO_NAMES.get(raw[0], str(raw[0]))
        offset += record_len

        if "src_ip" in record and "dst_ip" in record:
            _store_flow(conn, record, now)


def _store_flow(conn, record, ts):
    conn.execute("""
        INSERT INTO dependencies (src_ip, dst_ip, dst_port, protocol, flow_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(src_ip, dst_ip, dst_port, protocol)
        DO UPDATE SET flow_count = flow_count + 1, last_seen = excluded.last_seen
    """, (
        record["src_ip"], record["dst_ip"],
        record.get("dst_port", 0), record.get("protocol", "TCP"),
        ts, ts,
    ))
    conn.commit()


def _handle_packet(data, addr, conn):
    if len(data) < 16:
        return
    version, length, export_time, seq, domain_id = struct.unpack_from("!HHIII", data, 0)
    if version != 10:
        return  # not IPFIX (v9/v5 not needed for vDS)

    offset = 16
    while offset + 4 <= len(data) and offset < length:
        set_id, set_len = struct.unpack_from("!HH", data, offset)
        if set_len < 4:
            break
        set_body = data[offset + 4: offset + set_len]
        if set_id == 2:  # Template Set
            _parse_template_set(set_body)
        elif set_id >= 256:  # Data Set referencing a template ID
            _parse_data_set(set_id, set_body, conn)
        offset += set_len

    logger.info(f"Packet from {addr[0]} version={version} size={len(data)}")


def run():
    _setup_file_logging()
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    logger.info(f"IPFIX listener bound to UDP {LISTEN_PORT}")

    try:
        while True:
            data, addr = sock.recvfrom(65535)
            try:
                _handle_packet(data, addr, conn)
            except Exception as e:
                logger.warning(f"Failed to decode packet from {addr[0]}: {e}")
    finally:
        sock.close()
        conn.close()


if __name__ == "__main__":
    run()
