"""Тесты проверки белых списков (TCP/TLS), автопоиска и экспорта."""
import datetime
import http.server
import json
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading

import pytest

import core
from core.export import mask_value

# =========================================================
# Локальные серверы
# =========================================================


class _PlainTCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _PlainHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.recv(1024)
            self.request.sendall(b"not tls\r\n")
        except OSError:
            pass


@pytest.fixture
def plain_tcp():
    srv = _PlainTCP(('127.0.0.1', 0), _PlainHandler)
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05},
                     daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl недоступен")
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "k.pem", d / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(crt), "-subj", "/CN=localhost",
                    "-days", "1"], check=True, capture_output=True)
    return str(crt), str(key)


@pytest.fixture
def tls_server(tls_cert):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*tls_cert)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True):
                    pass
            except OSError:
                pass
    threading.Thread(target=serve, daemon=True).start()
    yield sock.getsockname()[1]
    stop.set()
    sock.close()


# =========================================================
# probe_host / run_whitelist_check
# =========================================================

def test_probe_closed_port():
    r = core.probe_host('127.0.0.1', 1, timeout=0.5)
    assert (r.tcp_ok, r.tls_ok, r.ok) == (False, False, False)
    assert r.detail


def test_probe_tcp_without_tls(plain_tcp):
    r = core.probe_host('127.0.0.1', plain_tcp, timeout=1.0)
    assert r.tcp_ok and not r.tls_ok
    assert "TLS" in r.detail


def test_probe_tls_ok(tls_server):
    r = core.probe_host('127.0.0.1', tls_server, timeout=2.0)
    assert r.tcp_ok and r.tls_ok and r.detail == "OK"


def test_whitelist_check_detects_filtering(tls_server, plain_tcp):
    rep = core.run_whitelist_check(
        white=[('127.0.0.1', tls_server)],
        neutral=[('127.0.0.1', plain_tcp), ('127.0.0.1', 1)], timeout=1.0)
    assert "фильтрация" in rep.title.lower()
    assert "SNI" in rep.detail
    assert [r.ok for r in rep.white] == [True]
    assert [r.tcp_ok for r in rep.neutral] == [True, False]


def test_whitelist_check_open_internet(tls_server):
    rep = core.run_whitelist_check(white=[('127.0.0.1', tls_server)],
                                   neutral=[('127.0.0.1', tls_server)], timeout=2.0)
    assert rep.title == "Белые списки ВЫКЛЮЧЕНЫ"
    assert rep.color == "#00b894"


def test_whitelist_check_is_parallel(monkeypatch):
    """Шесть проб по 0.3 с должны уложиться примерно в одну."""
    import time

    import core.whitelist as wl

    def slow(host, port, timeout):
        time.sleep(0.3)
        return wl.ProbeResult(host, port, True, True, "OK")
    monkeypatch.setattr(wl, "probe_host", slow)
    start = time.monotonic()
    rep = wl.run_whitelist_check(white=[('a', 1)] * 3, neutral=[('b', 1)] * 3)
    assert time.monotonic() - start < 1.5          # последовательно было бы 1.8 с
    assert len(rep.white) == 3 and len(rep.neutral) == 3


@pytest.mark.parametrize("exc, text", [
    (TimeoutError(), "таймаут"), (socket.gaierror(), "DNS не отвечает"),
    (ConnectionRefusedError(), "соединение отклонено"),
    (ConnectionResetError(), "соединение сброшено"),
    (OSError(101, "x"), "ошибка (101)"), (OSError(), "ошибка (?)"),
])
def test_os_error_descriptions(exc, text):
    from core.whitelist import _describe_os_error
    assert _describe_os_error(exc) == text


# =========================================================
# discovery: не-Huawei и закрытые порты
# =========================================================

class _HtmlHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = b"<html><body>Some other router</body></html>"
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_probe_rejects_non_huawei_http():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _HtmlHandler)
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05},
                     daemon=True).start()
    try:
        assert core.probe_huawei('127.0.0.1', srv.server_address[1], timeout=1) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_closed_port_returns_none():
    assert core.probe_huawei('127.0.0.1', 1, timeout=0.5) is None


# =========================================================
# export
# =========================================================

ROWS = [
    {'ts': '2026-09-24T12:00:00', 'rsrp': -95.0, 'sinr': 12.5, 'rsrq': -9.0,
     'rssi': -61.0, 'plmn': '25002', 'pci': 287, 'earfcn': 1300, 'bands': 'B3+B1',
     'rat': 'lte_ca', 'enodeb': 180123, 'sector': 12},
    {'ts': '2026-09-24T12:00:01', 'rsrp': None, 'sinr': None},
]


def test_csv_ru_excel(tmp_path):
    p = tmp_path / "s.csv"
    assert core.write_session_csv(str(p), ROWS, lang='ru') == 2
    data = p.read_bytes()
    assert data.startswith(b'\xef\xbb\xbf')
    lines = data.decode('utf-8-sig').splitlines()
    assert lines[0].startswith("ts;rsrp;rssi;sinr;rsrq")
    assert ";-95;" in lines[1] and ";12,5;" in lines[1]
    assert lines[1].endswith(";B3+B1;lte_ca")
    assert lines[2].startswith("2026-09-24T12:00:01;;")


def test_csv_en(tmp_path):
    p = tmp_path / "s.csv"
    core.write_session_csv(str(p), ROWS[:1], lang='en')
    lines = p.read_text(encoding='utf-8-sig').splitlines()
    assert lines[0].startswith("ts,rsrp,rssi,sinr,rsrq")
    assert ",12.5," in lines[1]


def test_csv_dialect_and_name():
    assert core.csv_dialect('ru') == (';', ',')
    assert core.csv_dialect('en') == (',', '.')
    assert core.default_csv_name(datetime.datetime(2026, 9, 24, 7, 5, 9)) == \
        "hua4gmon-20260924-070509.csv"


def test_mask_value():
    assert mask_value('860000000000001') == '***********0001'
    assert mask_value('123') == '***'


def test_mask_value_full():
    assert mask_value('10.1.2.3', keep=0) == '********'


def test_mask_network_addresses_fully():
    """MAC Wi-Fi (BSSID), IP и DNS — полностью: по ним находят место установки."""
    info = {'WifiMacAddrWl0': 'AC:DE:48:00:11:22', 'WifiMacAddrWl1': 'AC:DE:48:00:11:23',
            'MacAddress1': 'AC:DE:48:00:11:24', 'WanIPAddress': '10.1.2.3',
            'WanIPv6Address': '2001:db8::1', 'wan_dns_address': '10.0.0.1,10.0.0.2',
            'wan_ipv6_dns_address': '2001:db8::53', 'PrimaryDns': '8.8.8.8',
            'ImeiSvn': '12', 'SerialNumber': 'ABCDEF123456',
            'DeviceName': 'B636-336', 'workmode': 'LTE', 'Mccmnc': '25002'}
    out = core.mask_sensitive(info)
    for key in ('WifiMacAddrWl0', 'WifiMacAddrWl1', 'MacAddress1', 'WanIPAddress',
                'WanIPv6Address', 'wan_dns_address', 'wan_ipv6_dns_address', 'PrimaryDns'):
        assert set(out[key]) == {'*'}, key
    assert out['SerialNumber'] == '********3456' and out['ImeiSvn'] == '**'
    assert (out['DeviceName'], out['workmode'], out['Mccmnc']) == ('B636-336', 'LTE', '25002')
    # Вложенные структуры под «секретным» ключом разбираются, а не затираются целиком.
    assert core.mask_sensitive({'MacList': [{'Mac': 'AA:BB'}]}) == {'MacList': [{'Mac': '*****'}]}


def test_mask_address_lists_and_spellings():
    """Список адресов под секретным ключом и другие написания имён полей."""
    out = core.mask_sensitive({'MacAddress': ['AC:DE:48:00:11:22', ''],
                               'WanIpAddr': '10.1.2.3', 'IPv4Address': '10.1.2.4',
                               'Imsi': ['250020000000001'], 'Bands': ['3', '7']})
    assert out['MacAddress'] == ['*' * 17, '']
    assert set(out['WanIpAddr']) == set(out['IPv4Address']) == {'*'}
    assert out['Imsi'] == ['***********0001'] and out['Bands'] == ['3', '7']


def test_mask_sensitive_nested():
    data = {'Imei': '860000000000001', 'DeviceName': 'B636',
            'list': [{'imsi': '250020000000001'}], 'Msisdn': ''}
    out = core.mask_sensitive(data)
    assert out['Imei'].endswith('0001') and out['Imei'].startswith('*')
    assert out['DeviceName'] == 'B636'
    assert out['list'][0]['imsi'].startswith('*')
    assert out['Msisdn'] == ''


def test_diagnostics_report():
    rep = core.build_diagnostics(
        app_version='1.4.0', library_version='2.0.1', platform='test',
        dumps={'device_information': {'Iccid': '8970102000000000001'}},
        errors={'net_cell_info': 'Err: 100002'},
        now=datetime.datetime(2026, 9, 24, 12, 0, 0))
    text = core.diagnostics_json(rep)
    back = json.loads(text)
    assert back['app_version'] == '1.4.0' and back['huawei_lte_api'] == '2.0.1'
    assert '8970102000000000001' not in text
    assert back['errors']['net_cell_info'] == 'Err: 100002'
    assert back['generated'] == '2026-09-24T12:00:00'


# ---------- вердикт белых списков: подсказка про SNI ----------

def _fake_probes(monkeypatch, results):
    import core.whitelist as wl
    monkeypatch.setattr(wl, "probe_host", lambda host, port, timeout: results[host])


def test_sni_hint_only_when_neutral_sites_fail(monkeypatch):
    ok = lambda h: core.ProbeResult(h, 443, True, True, "OK")          # noqa: E731
    sni = lambda h: core.ProbeResult(h, 443, True, False, "TLS")       # noqa: E731
    white, neutral = [("gosuslugi.ru", 443)], [("example.com", 443), ("duckduckgo.com", 443)]
    _fake_probes(monkeypatch, {"gosuslugi.ru": ok("gosuslugi.ru"),
                               "example.com": sni("example.com"),
                               "duckduckgo.com": ok("duckduckgo.com")})
    report = core.run_whitelist_check(white, neutral, timeout=0.1)
    assert "SNI" not in report.detail          # один сайт заблокирован — это не фильтр
    _fake_probes(monkeypatch, {"gosuslugi.ru": ok("gosuslugi.ru"),
                               "example.com": sni("example.com"),
                               "duckduckgo.com": sni("duckduckgo.com")})
    report = core.run_whitelist_check(white, neutral, timeout=0.1)
    assert "SNI" in report.detail
