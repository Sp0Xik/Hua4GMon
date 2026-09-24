"""
Проверка режима «белых списков» БС в России.

Подход:
  1. Для каждой цели — TCP-соединение на 443 и TLS-рукопожатие с SNI.
     Фильтрация бывает по IP (не проходит TCP) и по SNI (TCP проходит,
     рвётся ClientHello). Поэтому «доступен» = рукопожатие TLS завершено.
  2. Сравниваем две группы:
      * WHITELIST_HOSTS_RU  — точно в белых списках всех ОпСоС РФ;
      * CONTROL_HOSTS_NEUTRAL — не блокированы РКН, не в белых списках.
  3. Таблица истинности:
      white = ✔, neutral = ✔   →  фильтр ВЫКЛ (обычный режим);
      white = ✔, neutral = ✘   →  фильтр ВКЛ (только белые!);
      white = ✘, neutral = ✔   →  Wi-Fi/VPN не через 4G (странно);
      white = ✘, neutral = ✘   →  нет интернета вообще / DNS лежит.

Сертификат не проверяется намеренно: данные не передаются, важно лишь,
пропустил ли оператор ClientHello с этим именем. Так проба не зависит
от хранилища сертификатов (на Android у python-for-android его нет).
Цели опрашиваются параллельно — общий срок равен самой долгой пробе.
"""
from __future__ import annotations

import socket
import ssl
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.constants import (
    CONTROL_HOSTS_NEUTRAL,
    WHITELIST_HOSTS_RU,
    WL_CHECK_TIMEOUT,
)
from core.i18n import t


def _describe_os_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return t("таймаут")
    if isinstance(exc, socket.gaierror):
        return t("DNS не отвечает")
    if isinstance(exc, ConnectionRefusedError):
        return t("соединение отклонено")
    if isinstance(exc, ConnectionResetError):
        return t("соединение сброшено")
    errno = getattr(exc, 'errno', None)
    return t("ошибка ({code})").format(code=errno if errno is not None else '?')


def tcp_reachable(host: str, port: int,
                  timeout: float = WL_CHECK_TIMEOUT) -> tuple[bool, str]:
    """Пытается открыть TCP-соединение. Возвращает (доступен, описание)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "OK"
    except OSError as e:
        return False, _describe_os_error(e)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    host: str
    port: int
    tcp_ok: bool
    tls_ok: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.tls_ok


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe_host(host: str, port: int = 443,
               timeout: float = WL_CHECK_TIMEOUT) -> ProbeResult:
    """TCP + TLS с SNI. Различает блокировку по IP и по SNI.

    Hardware validation required: поведение на SIM с включёнными белыми списками.
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return ProbeResult(host, port, False, False, _describe_os_error(e))
    try:
        with _tls_context().wrap_socket(raw, server_hostname=host):
            return ProbeResult(host, port, True, True, "OK")
    except OSError as e:          # ssl.SSLError — тоже OSError
        reason = "TLS" if isinstance(e, ssl.SSLError) else _describe_os_error(e)
        return ProbeResult(host, port, True, False,
                           t("TCP есть, TLS оборван ({reason})").format(reason=reason))
    finally:
        raw.close()


@dataclass(frozen=True, slots=True)
class WhitelistReport:
    white: list[ProbeResult]
    neutral: list[ProbeResult]
    title: str
    detail: str
    color: str


def run_whitelist_check(white: Sequence[tuple[str, int]] = WHITELIST_HOSTS_RU,
                        neutral: Sequence[tuple[str, int]] = CONTROL_HOSTS_NEUTRAL,
                        timeout: float = WL_CHECK_TIMEOUT) -> WhitelistReport:
    """Параллельно проверяет все цели и выносит вердикт."""
    targets = list(white) + list(neutral)
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(targets)))) as pool:
        results = list(pool.map(lambda hp: probe_host(hp[0], hp[1], timeout), targets))
    white_res, neutral_res = results[:len(white)], results[len(white):]
    title, detail, color = analyze_whitelist_results(
        [(r.host, r.ok) for r in white_res],
        [(r.host, r.ok) for r in neutral_res])
    sni_blocked = [r.host for r in neutral_res if r.tcp_ok and not r.tls_ok]
    if sni_blocked and any(r.ok for r in white_res):
        detail += " " + t("Похоже на фильтрацию по SNI: TCP проходит, TLS — нет.")
    return WhitelistReport(white_res, neutral_res, title, detail, color)


def analyze_whitelist_results(
        white_results: list[tuple[str, bool]],
        neutral_results: list[tuple[str, bool]]) -> tuple[str, str, str]:
    """По таблице истинности возвращает (заголовок, описание, цвет).

    Строки переводятся на текущий язык (RU/EN) через core.i18n.
    """
    white_ok = sum(1 for _, ok in white_results if ok)
    neutral_ok = sum(1 for _, ok in neutral_results if ok)
    wt, nt = len(white_results), len(neutral_results)
    white_any = white_ok > 0
    neutral_any = neutral_ok > 0

    if white_any and neutral_any:
        return (t("Белые списки ВЫКЛЮЧЕНЫ"),
                t("Обычный режим — открыт весь интернет "
                  "(белых: {w}/{wt}, нейтральных: {n}/{nt}).").format(
                      w=white_ok, wt=wt, n=neutral_ok, nt=nt),
                "#00b894")
    if white_any and not neutral_any:
        return (t("⚠ Вероятна фильтрация (белые списки)"),
                t("Разрешённые сайты отвечают, а нейтральные — нет "
                  "(белых: {w}/{wt}, нейтральных: 0/{nt}). Похоже на режим "
                  "белых списков оператора. Для точности проверьте "
                  "открытие обычного сайта в браузере.").format(
                      w=white_ok, wt=wt, nt=nt),
                "#d63031")
    if not white_any and neutral_any:
        return (t("Аномалия"),
                t("Нейтральные сайты доступны, но «белые» не отвечают. "
                  "Скорее всего, вы вышли в интернет не через 4G "
                  "(другой Wi-Fi, провод, VPN). Подключитесь к Wi-Fi роутера "
                  "и повторите."),
                "#fdcb6e")
    return (t("Нет интернета"),
            t("Ни одна цель не отвечает. Либо у роутера нет связи с БС, "
              "либо проблема с DNS/маршрутом. Проверьте RSRP и трафик."),
            "#636e72")
