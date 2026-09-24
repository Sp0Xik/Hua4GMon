"""
Автопоиск роутера Huawei в локальной сети.

Проверяются типовые адреса параллельно. Роутер Huawei HiLink отвечает на
GET /api/webserver/SesTokInfo (или /api/webserver/token) без
авторизации XML-ответом <response>…</response> либо <error><code>…</code>.
Используется только стандартная библиотека.
"""
from __future__ import annotations

import http.client
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.constants import DISCOVERY_CANDIDATES, DISCOVERY_TIMEOUT

_PROBE_PATHS = ("/api/webserver/SesTokInfo", "/api/webserver/token")
_HUAWEI_MARKERS = (b"<SesInfo>", b"<TokInfo>", b"<token>")
_ERROR_RE = re.compile(rb"<error>\s*<code>\d+</code>", re.IGNORECASE)
_NAME_RE = re.compile(rb"<devicename>([^<]{1,64})</devicename>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FoundRouter:
    ip: str
    model: str = ''


def _get(ip: str, port: int, path: str, timeout: float) -> bytes | None:
    conn = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/xml"})
        resp = conn.getresponse()
        return resp.read(8192)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def probe_huawei(ip: str, port: int = 80,
                 timeout: float = DISCOVERY_TIMEOUT) -> FoundRouter | None:
    """Отвечает ли по адресу веб-API Huawei. Возвращает FoundRouter или None.

    Hardware validation required: ответ SesTokInfo/basic_information без входа.
    """
    for path in _PROBE_PATHS:
        body = _get(ip, port, path, timeout)
        if body is None:
            return None          # хост не ответил — второй путь не поможет
        if any(m in body for m in _HUAWEI_MARKERS) or _ERROR_RE.search(body):
            model = ''
            info = _get(ip, port, "/api/device/basic_information", timeout)
            if info:
                m = _NAME_RE.search(info)
                if m:
                    model = m.group(1).decode('utf-8', 'replace').strip()
            return FoundRouter(ip, model)
    return None


def discover_router(candidates: Iterable[str] = DISCOVERY_CANDIDATES,
                    port: int = 80,
                    timeout: float = DISCOVERY_TIMEOUT) -> FoundRouter | None:
    """Первый найденный роутер в порядке кандидатов (опрос параллельный)."""
    ordered = list(dict.fromkeys(c for c in candidates if c))
    if not ordered:
        return None
    with ThreadPoolExecutor(max_workers=min(8, len(ordered))) as pool:
        results = list(pool.map(lambda ip: probe_huawei(ip, port, timeout), ordered))
    for found in results:
        if found is not None:
            return found
    return None
