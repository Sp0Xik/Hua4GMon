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
    """Prefer the longest matching route; reject equally specific interfaces."""
    address = ipaddress.IPv4Address(host)
    matches = []
    best = -1
    for network, subnets in candidates:
        routes = (ipaddress.ip_network(subnet, strict=False) for subnet in subnets)
        prefix = max((r.prefixlen for r in routes if address in r and (r.prefixlen or address.is_private)), default=-1)
        if prefix > best:
            best, matches = prefix, [network]
        elif prefix == best and prefix >= 0:
            matches.append(network)
    if len(matches) != 1:
        raise OSError('No unique local network for router')
    return matches[0]


def bind_android_socket(sock: socket.socket, host: str) -> None:
    """Route only this socket over Wi-Fi/Ethernet, without INTERNET validation.

    Hardware validation required: Android with mobile data + offline router Wi-Fi.
    """
    _bind_network_socket(sock, android_router_network(host))


def android_router_network(host: str):
    """Select by the router address, not by the external probe destination."""

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
        for route in links.getRoutes().toArray():
            dest = route.getDestination()
            if dest is not None and ':' not in str(dest.getAddress().getHostAddress()):
                subnets.append(str(dest.getAddress().getHostAddress()) + '/' + str(dest.getPrefixLength()))
        candidates.append((network, subnets))
    return select_local_network(host, candidates)


def _bind_network_socket(sock: socket.socket, network) -> None:
    import ctypes

    lib = ctypes.CDLL('libandroid.so', use_errno=True)
    bind = lib.android_setsocknetwork
    bind.argtypes = [ctypes.c_uint64, ctypes.c_int]
    bind.restype = ctypes.c_int
    if bind(network.getNetworkHandle(), sock.fileno()) != 0:
        raise OSError(ctypes.get_errno(), 'Could not bind router socket to local network')


def android_probe_connection(router_ip: str):
    """DNS and each probe socket use the same router network, never cellular fallback.

    Hardware validation required: offline Wi-Fi alongside cellular data and VPN.
    """
    network = android_router_network(router_ip)

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        import time

        host, port = address
        deadline = time.monotonic() + timeout
        try:
            addresses = network.getAllByName(host)
        except Exception as exc:
            raise socket.gaierror('Router network DNS failed') from exc
        last_error: OSError = OSError('No addresses on router network')
        for resolved in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Router network probe timed out')
            ip = str(resolved.getHostAddress())
            sock = socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(remaining)
                _bind_network_socket(sock, network)
                sock.connect((ip, port))
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()
            except Exception:
                sock.close()
                raise
        raise last_error

    return connect
