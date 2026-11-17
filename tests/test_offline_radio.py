"""Offline router access, truthful radio details, independent neighbour sources."""
import socket
from functools import partial

import pytest

from core.carriers import parse_lte_carriers
from core.local_transport import router_adapter, select_local_network
from core.models import build_snapshot
from core.router import RouterSession, huawei_client_factory
from core.scanner import Survey
from tests.fake_huawei import PASSWORD, FakeRouter


def test_router_socket_binding_precedes_connect_and_does_not_affect_other_requests():
    import requests
    from urllib3.connectionpool import HTTPConnectionPool
    calls = []

    def bind(sock, host):
        with pytest.raises(OSError):
            sock.getpeername()
        calls.append(host)

    with FakeRouter() as router:
        session = RouterSession(router.address, PASSWORD,
                                factory=partial(huawei_client_factory, socket_binder=bind))
        try:
            assert session.open()['DeviceName'] == 'B636-336'
            assert session.fetch()['rsrp']
            assert calls and set(calls) == {'127.0.0.1'}
            other = requests.Session()
            assert other.adapters['http://'].poolmanager.pool_classes_by_scheme['http'] is HTTPConnectionPool
            other.close()
        finally:
            session.close()


def test_network_selection_never_requires_internet_or_chooses_an_unrelated_network():
    assert select_local_network('192.168.8.1', [('wifi', ['192.168.8.2/24']),
                                               ('other', ['192.168.1.2/24'])]) == 'wifi'
    for networks in ([], [('other', ['192.168.1.2/24'])],
                     [('a', ['192.168.8.2/24']), ('b', ['192.168.8.3/24'])]):
        with pytest.raises(OSError):
            select_local_network('192.168.8.1', networks)


def test_binding_failure_closes_socket_without_fallback(monkeypatch):
    import requests
    from urllib3.exceptions import NewConnectionError
    made = []
    original = socket.socket

    def make(*args):
        sock = original(*args)
        made.append(sock)
        return sock

    def reject(*args):
        raise OSError('No local network')

    monkeypatch.setattr(socket, 'socket', make)
    adapter = router_adapter(reject)
    conn = adapter.poolmanager.connection_from_url('http://127.0.0.1').ConnectionCls('127.0.0.1', timeout=1)
    with pytest.raises(NewConnectionError):
        conn._new_conn()
    assert made[0].fileno() == -1
    adapter.close()
    assert requests.Session().trust_env is True


def test_capability_failure_does_not_block_offline_login():
    with FakeRouter() as router:
        router.state.http_errors['GET', '/api/net/net-mode'] = 503
        session = RouterSession(router.address, PASSWORD)
        try:
            assert session.open()['DeviceName'] == 'B636-336'
            assert session.fetch()['rsrp']
            assert not session.caps_known
        finally:
            session.close()


@pytest.mark.parametrize('value, expected', [('1', 1), ('2', 2), ('8', 8),
                                           ('0', None), ('9', None), ('2x2', None),
                                           (None, None), ('2.5', None)])
def test_rank_requires_explicit_valid_nr_field(value, expected):
    snap = build_snapshot({'nrrank': value, 'transmode': 'TM[4]', 'cqi0': '12', 'cqi1': '0'})
    assert snap.nr_rank == expected
    assert snap.display().nr_rank is None


def test_same_band_carriers_are_not_deduplicated_or_inferred_from_neighbours():
    raw = {'ServiceStatus': '2', 'rsrp': '-90', 'earfcn': 'DL:2850 UL:20850',
           'band': '20MHz@2850(B7) + 20MHz@3048(B7)'}
    snap = build_snapshot(raw)
    assert [c.earfcn for c in snap.lte_carriers] == [2850, 3048]
    assert snap.ca is True and snap.band_label == 'CA: B7+B7'
    assert len(build_snapshot({**raw, 'CurrentNetworkTypeEx': '101'}).lte_carriers) == 1
    assert not build_snapshot({**raw, 'earfcn': '1300'}).lte_carriers
    for bad in ('B7+B7', '20MHz@2850(B3)', '20MHz@2850(B7)+20MHz@2850(B7)',
                '100MHz@2850(B7)', '20MHz@2850(B7)+unknown'):
        assert not parse_lte_carriers(bad)


def test_extra_cell_sources_are_read_only_and_unsupported_is_not_retried():
    with FakeRouter() as router:
        session = RouterSession(router.address, PASSWORD)
        session.open()
        try:
            get = session._connection.get
            calls = []

            def read(endpoint):
                calls.append(endpoint)
                return get(endpoint)

            session._connection.get = read
            timeout = session._connection.timeout
            session._extra_cell_sources()
            assert session._connection.timeout == timeout
            previous = list(calls)
            session._extra_cell_sources()
            assert calls == previous
            assert set(calls) == {'device/nbrcellinfo', 'device/seccellinfo'}
        finally:
            session.close()


def test_neighbours_keep_their_source_and_never_become_registered():
    survey = Survey()
    survey.start()
    survey.accept_cells({'earfcn': '2850', 'pci': '177', 'rsrp': '-90'}, 'device/nbrcellinfo')
    row = survey.ranked()[0]
    assert row.source == 'device/nbrcellinfo' and not row.snap.registered
    assert row.snap.plmn == ''


def test_discovery_uses_local_binding():
    from core.discovery import discover_router
    calls = []
    def bind(sock, host):
        with pytest.raises(OSError):
            sock.getpeername()
        calls.append(host)
    with FakeRouter() as router:
        from urllib.parse import urlsplit
        address = urlsplit("http://" + router.address)
        found = discover_router([address.hostname], port=address.port, socket_binder=bind)
        assert found is not None and found.ip == address.hostname
        assert calls

@pytest.mark.parametrize('result', [0, -1])
def test_android_binding_uses_selected_network_handle(monkeypatch, result):
    import ctypes
    import sys
    from types import SimpleNamespace as NS

    from core.local_transport import bind_android_socket
    calls = []
    class Bind:
        def __call__(self, handle, fd):
            calls.append((handle, fd))
            return result
    bind = Bind()
    link = NS(getAddress=lambda: NS(getHostAddress=lambda: '192.168.8.2'),
              getPrefixLength=lambda: 24)
    network = NS(getNetworkHandle=lambda: 12345678901)
    manager = NS(getAllNetworks=lambda: [network],
                 getNetworkCapabilities=lambda n: NS(hasTransport=lambda t: t == 1),
                 getLinkProperties=lambda n: NS(getLinkAddresses=lambda: NS(toArray=lambda: [link])))
    activity = NS(getSystemService=lambda name: manager)
    monkeypatch.setitem(sys.modules, 'jnius', NS(autoclass=lambda name: NS(mActivity=activity)))
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **kw: NS(android_setsocknetwork=bind))
    sock = NS(fileno=lambda: 7)
    if result:
        with pytest.raises(OSError):
            bind_android_socket(sock, '192.168.8.1')
    else:
        bind_android_socket(sock, '192.168.8.1')
    assert calls == [(12345678901, 7)]
    assert bind.argtypes == [ctypes.c_uint64, ctypes.c_int]
