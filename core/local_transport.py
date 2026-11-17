"""Per-socket routing for a local IPv4 router; no process-wide network changes."""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable


def router_adapter(bind_socket: Callable[[socket.socket, str], None]):
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection
    from urllib3.connectionpool import HTTPConnectionPool
    from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

    class LocalConnection(HTTPConnection):
        def _new_conn(self):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Router addresses are numeric: never require external DNS.
                host = str(ipaddress.IPv4Address(self.host))
                sock.settimeout(self.timeout)
                for option in self.socket_options or []:
                    sock.setsockopt(*option)
                bind_socket(sock, host)
                sock.connect((host, self.port))
                return sock
            except TimeoutError as exc:
                sock.close()
                raise ConnectTimeoutError(self, 'Router connection timed out') from exc
            except Exception as exc:
                sock.close()
                raise NewConnectionError(self, str(exc)) from exc

    class LocalPool(HTTPConnectionPool):
        ConnectionCls = LocalConnection

    class LocalAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {
                **self.poolmanager.pool_classes_by_scheme, 'http': LocalPool}

    return LocalAdapter(max_retries=0)


def select_local_network(host, candidates):
    """Only directly connected subnets; ambiguous interfaces require user choice."""
    address = ipaddress.IPv4Address(host)
    matches = []
    for network, subnets in candidates:
        if any(address in ipaddress.ip_network(subnet, strict=False) for subnet in subnets):
            matches.append(network)
    if len(matches) != 1:
        raise OSError('No unique local network for router')
    return matches[0]


def bind_android_socket(sock: socket.socket, host: str) -> None:
    """Route only this socket over Wi-Fi/Ethernet, without INTERNET validation.

    Hardware validation required: Android with mobile data + offline router Wi-Fi.
    """
    import ctypes

    from jnius import autoclass

    activity = autoclass('org.kivy.android.PythonActivity').mActivity
    manager = activity.getSystemService('connectivity')
    candidates = []
    for network in manager.getAllNetworks():
        caps = manager.getNetworkCapabilities(network)
        links = manager.getLinkProperties(network)
        if caps is None or links is None or not (caps.hasTransport(1) or caps.hasTransport(3)):
            continue
        subnets = [str(link.getAddress().getHostAddress()) + '/' + str(link.getPrefixLength())
                   for link in links.getLinkAddresses().toArray()
                   if ':' not in str(link.getAddress().getHostAddress())]
        candidates.append((network, subnets))
    network = select_local_network(host, candidates)
    lib = ctypes.CDLL('libandroid.so', use_errno=True)
    bind = lib.android_setsocknetwork
    bind.argtypes = [ctypes.c_uint64, ctypes.c_int]
    bind.restype = ctypes.c_int
    if bind(network.getNetworkHandle(), sock.fileno()) != 0:
        raise OSError(ctypes.get_errno(), 'Could not bind router socket to local network')
