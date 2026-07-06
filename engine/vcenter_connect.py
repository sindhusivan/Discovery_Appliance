"""
TAO Discovery Platform — vCenter Connection Helper
SSL-aware SmartConnect wrapper shared by the inventory collector and NetFlow manager.
"""

import ssl
import logging
from pyVim.connect import SmartConnect, Disconnect

logger = logging.getLogger("vcenter_connect")


def get_service_instance(host, user, password, disable_ssl=True):
    """Open a pyVmomi ServiceInstance connection to vCenter.

    disable_ssl=True skips certificate verification, needed for the
    self-signed certs common in lab/POC vCenter deployments.
    """
    if disable_ssl:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = None
    logger.info(f"Connecting to vCenter {host} (ssl_verify={not disable_ssl})")
    return SmartConnect(host=host, user=user, pwd=password, sslContext=context)


def close(service_instance):
    Disconnect(service_instance)
