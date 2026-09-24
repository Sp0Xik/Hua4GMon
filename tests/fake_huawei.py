"""
Поддельный HTTP-роутер Huawei HiLink для интеграционных тестов.

С ним работает настоящая huawei-lte-api: вход по SHA256 с CSRF-токенами,
cookie-сессия, GET/POST эндпоинты в XML, коды ошибок API. Это позволяет
проверить миграцию на huawei-lte-api 2.x без реального роутера.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import xmltodict

PASSWORD = "secret-pass"
USERNAME = "admin"


@dataclass
class FakeRouterState:
    password: str = PASSWORD
    lte_band: str = "7FFFFFFFFFFFFFFF"
    network_band: str = "3FFFFFFF"
    network_mode: str = "00"
    antenna: int = 3
    dataswitch: int = 1
    reboots: int = 0
    logins: int = 0
    logouts: int = 0
    sessions: set[str] = field(default_factory=set)
    posts: list[tuple[str, dict]] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    signal: dict[str, Any] = field(default_factory=lambda: {
        'rsrp': '-88dBm', 'rsrq': '-9.0dB', 'rssi': '-61dBm', 'sinr': '14dB',
        'pci': '287', 'cell_id': str(180123 * 256 + 12),
        'earfcn': 'DL:1300 UL:19300', 'band': '3',
        'dlbandwidth': '20MHz', 'ulbandwidth': '20MHz',
        'transmode': 'TM[3]', 'cqi0': '11', 'cqi1': '10',
        'txpower': 'PPusch:12dBm PPucch:6dBm',
    })


_TOKENS = (f"tok{n:04d}" for n in itertools.count())


def _xml(payload: Any) -> bytes:
    return xmltodict.unparse({'response': payload}).encode()


def _error(code: int) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<error><code>{code}</code><message></message></error>').encode()


def _expected_password(username: str, password: str, token: str) -> str:
    inner = base64.b64encode(hashlib.sha256(password.encode()).hexdigest().encode())
    digest = hashlib.sha256(username.encode() + inner + token.encode()).hexdigest()
    return base64.b64encode(digest.encode()).decode()


class _Handler(BaseHTTPRequestHandler):
    server: _FakeServer  # type: ignore[assignment]

    def log_message(self, *args: Any) -> None:  # тихо в тестах
        pass

    # ---- helpers ----

    @property
    def st(self) -> FakeRouterState:
        return self.server.state

    def _session_id(self) -> str | None:
        cookie = self.headers.get('Cookie') or ''
        for part in cookie.split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'SessionID':
                return value
        return None

    def _authed(self) -> bool:
        sid = self._session_id()
        return sid is not None and sid in self.st.sessions

    def _send(self, body: bytes, *, headers: dict[str, str] | None = None,
              content_type: str = 'text/xml') -> None:
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split('?')[0]
        self.st.gets.append(path)
        if path == '/':
            a, b = next(_TOKENS), next(_TOKENS)
            html = (f'<html><head><meta name="csrf_token" content="{a}"/>'
                    f'<meta name="csrf_token" content="{b}"/></head></html>')
            self._send(html.encode(), content_type='text/html')
            return
        public = {
            '/api/webserver/SesTokInfo': {'SesInfo': 'SessionID=x', 'TokInfo': next(_TOKENS)},
            '/api/device/basic_information': {'devicename': 'B636-336', 'productfamily': 'LTE'},
            '/api/user/state-login': {'State': '-1', 'Username': '', 'password_type': '4'},
        }
        if path in public:
            self._send(_xml(public[path]))
            return
        if not self._authed():
            self._send(_error(100003))
            return
        st = self.st
        data: dict[str, Any] | None = {
            '/api/device/information': {
                'DeviceName': 'B636-336', 'SerialNumber': 'SN1234567890',
                'Imei': '860000000000001', 'Imsi': '250020000000001',
                'Iccid': '8970102000000000001', 'Msisdn': '+79001234567',
                'SoftwareVersion': '11.0.3.1', 'workmode': 'LTE'},
            '/api/device/signal': st.signal,
            '/api/net/current-plmn': {'State': '0', 'FullName': 'MegaFon',
                                      'ShortName': 'MegaFon', 'Numeric': '25002',
                                      'Rat': '7'},
            '/api/monitoring/status': {'CurrentNetworkTypeEx': '101',
                                       'ConnectionStatus': '901', 'SimStatus': '1'},
            '/api/monitoring/traffic-statistics': {
                'CurrentDownloadRate': '125000', 'CurrentUploadRate': '12500',
                'TotalDownload': '1048576', 'TotalUpload': '524288',
                'CurrentConnectTime': '3600'},
            '/api/net/net-mode': {'NetworkMode': st.network_mode,
                                  'NetworkBand': st.network_band,
                                  'LTEBand': st.lte_band},
            '/api/net/net-mode-list': {
                'AccessList': {'Access': ['00', '01', '02', '03']},
                'LTEBandList': {'LTEBand': [
                    {'Name': 'LTE BC1/LTE BC3/LTE BC7', 'Value': '45'},
                    {'Name': 'LTE BC20', 'Value': '80000'},
                    {'Name': 'LTE ALL', 'Value': '7FFFFFFFFFFFFFFF'}]}},
            '/api/device/antenna_settings': {'antenna_type': str(st.antenna)},
            '/api/dialup/mobile-dataswitch': {'dataswitch': str(st.dataswitch)},
        }.get(path)
        if data is None:
            self._send(_error(100002))       # month_statistics и прочее
            return
        self._send(_xml(data))

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        req = (xmltodict.parse(raw) or {}).get('request') if raw else {}
        req = req or {}
        self.st.posts.append((path, dict(req)))
        token_headers = {'__RequestVerificationToken': next(_TOKENS)}

        if path == '/api/user/login':
            token = self.headers.get('__RequestVerificationToken', '')
            want = _expected_password(req.get('Username', ''), self.st.password, token)
            if req.get('Password') != want:
                self._send(_error(108006))
                return
            sid = f"sid{next(_TOKENS)}"
            self.st.sessions.add(sid)
            self.st.logins += 1
            self._send(_xml('OK'), headers={
                'Set-Cookie': f'SessionID={sid}; path=/',
                '__RequestVerificationTokenone': next(_TOKENS),
                '__RequestVerificationTokentwo': next(_TOKENS)})
            return
        if not self._authed():
            self._send(_error(100003))
            return
        st = self.st
        if path == '/api/user/logout':
            st.sessions.discard(self._session_id() or '')
            st.logouts += 1
        elif path == '/api/net/net-mode':
            st.network_mode = str(req.get('NetworkMode'))
            st.network_band = str(req.get('NetworkBand'))
            st.lte_band = str(req.get('LTEBand')).upper()
        elif path == '/api/device/antenna_settings':
            st.antenna = int(req.get('antenna_type'))
        elif path == '/api/device/control':
            if str(req.get('Control')) == '1':
                st.reboots += 1
                st.sessions.clear()
        elif path == '/api/dialup/mobile-dataswitch':
            st.dataswitch = int(req.get('dataswitch'))
        else:
            self._send(_error(100002))
            return
        self._send(_xml('OK'), headers=token_headers)


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: FakeRouterState) -> None:
        super().__init__(('127.0.0.1', 0), _Handler)
        self.state = state


class FakeRouter:
    """Контекстный менеджер: поднимает поддельный роутер на 127.0.0.1."""

    def __init__(self, state: FakeRouterState | None = None) -> None:
        self.state = state or FakeRouterState()
        self._server = _FakeServer(self.state)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.05},
                                        daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def __enter__(self) -> FakeRouter:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
