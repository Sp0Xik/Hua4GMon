"""
Работа с роутером Huawei через huawei-lte-api 2.x.

Это единственный модуль проекта, который импортирует huawei_lte_api:
все особенности библиотеки (enum-аргументы, закрытие соединения,
перечень эндпоинтов) собраны здесь. UI работает только с RouterSession
и SessionWorker.

Импорт библиотеки ленивый — пакет core импортируется и без неё
(например, в тестах чистой логики).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.band_lock import (
    BandLockPlan,
    NetModeSettings,
    locked_bands,
    modem_supports_5g,
    plan_auto,
    plan_lock,
    plan_restore,
    verify,
)
from core.constants import (
    POLL_EVERY,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
    RECONNECT_REOPEN_AFTER,
)
from core.errors import ErrorKind, NetModeUnavailable, classify_error
from core.models import Snapshot, build_snapshot
from core.parsers import (
    parse_access_modes,
    parse_antenna_response,
    parse_supported_bands,
)

logger = logging.getLogger("Hua4GMon.router")

DEFAULT_USERNAME = "admin"
DEFAULT_TIMEOUT = 4.0
MIN_POLL_INTERVAL = 0.2     # защита роутера от опроса «без пауз»

# (url, username, password, timeout) → (connection, client)
ClientFactory = Callable[[str, str, str, float], tuple[Any, Any]]


def configure_library_logging() -> None:
    """huawei-lte-api 2.x пишет пароль в DEBUG-лог — держим её не ниже INFO."""
    logging.getLogger("huawei_lte_api").setLevel(logging.INFO)


def library_version() -> str:
    try:
        from importlib.metadata import version
        return version("huawei-lte-api")
    except Exception:
        return "unknown"


# Модуль RSA-2048, как у ключей роутеров Huawei. Для проверки, что
# шифрование работает, закрытый ключ не нужен — подходит любой нечётный
# модуль такой длины.
_SELF_TEST_RSA_N = "c" + "f" * 511
_SELF_TEST_RSA_E = "10001"


def library_self_test() -> list[str]:
    """Проверяет, что huawei-lte-api со всеми зависимостями работает в этой сборке.

    Windows-сборка содержит только нужные библиотеке нативные модули
    pycryptodomex (tools/bundle_filter.py). Проверка импортирует клиент,
    разбирает XML-ответ так же, как библиотека, и шифрует блок обоими
    способами, которыми библиотека шифрует данные для роутера.
    Возвращает строки отчёта; при неисправности бросает исключение.
    """
    import importlib

    import xmltodict
    for module in ("huawei_lte_api.Client", "huawei_lte_api.Connection"):
        importlib.import_module(module)
    from huawei_lte_api.Tools import Tools

    parsed = xmltodict.parse("<response><SignalIcon>5</SignalIcon></response>")
    if parsed != {"response": {"SignalIcon": "5"}}:
        raise RuntimeError(f"xmltodict: unexpected result {parsed!r}")
    for padding, name in ((0, "PKCS#1 v1.5"), (1, "OAEP")):
        first, second = (Tools.rsa_encrypt(_SELF_TEST_RSA_E, _SELF_TEST_RSA_N,
                                           b"Hua4GMon", padding) for _ in range(2))
        # Один блок RSA-2048 = 512 hex-символов; случайное дополнение
        # делает два шифротекста разными.
        if len(first) != 512 or first == second:
            raise RuntimeError(f"RSA {name}: unexpected ciphertext")
    return [f"huawei-lte-api {library_version()}", "XML: OK",
            "RSA PKCS#1 v1.5 + OAEP: OK"]


def huawei_client_factory(url: str, username: str, password: str,
                          timeout: float) -> tuple[Any, Any]:
    from huawei_lte_api.Client import Client
    from huawei_lte_api.Connection import Connection
    connection = Connection(url, username=username, password=password,
                            timeout=timeout)
    return connection, Client(connection)


def _network_mode_arg(mode: str) -> Any:
    """Режим сети для set_net_mode: enum, если он известен библиотеке.

    Коды 5G-режимов в NetworkModeEnum отсутствуют — для них библиотека
    принимает строку.
    """
    from huawei_lte_api.enums.net import NetworkModeEnum
    try:
        return NetworkModeEnum(mode)
    except ValueError:
        return mode


def _safe_close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        logger.debug("Connection close failed (ignored)", exc_info=True)


def _reraise_if_link_lost(exc: BaseException) -> None:
    if classify_error(exc) in (ErrorKind.NETWORK, ErrorKind.SESSION):
        raise exc


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Настройки модема, прочитанные для экрана «Сеть»."""
    net_mode: NetModeSettings | None
    locked: list[int] | None           # [] = AUTO, None = не прочитано
    antenna: int | None


class RouterSession:
    """Сессия с одним роутером. Все запросы сериализуются одним замком."""

    def __init__(self, ip: str, password: str, *,
                 username: str = DEFAULT_USERNAME,
                 timeout: float = DEFAULT_TIMEOUT,
                 factory: ClientFactory | None = None) -> None:
        self.ip = ip
        self.url = f"http://{ip}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._factory = factory or huawei_client_factory
        self._lock = threading.RLock()
        self._connection: Any = None
        self._client: Any = None
        self._tick = 0
        self._cache: dict[str, dict[str, Any]] = {}
        self._unsupported: set[str] = set()
        self._last_cell: tuple[Any, Any] | None = None
        self._data_off_pending = False
        self.device_info: dict[str, Any] = {}
        self.supported_bands: list[int] = []
        self.access_modes: list[str] = []
        self.caps_known = False         # net-mode-list прочитан (режимы и бэнды)
        self.original_net_mode: NetModeSettings | None = None

    # ---- жизненный цикл ----

    @property
    def is_open(self) -> bool:
        return self._client is not None

    @property
    def supports_5g(self) -> bool:
        return modem_supports_5g(self.access_modes)

    @property
    def locks_lte_only(self) -> bool:
        """Band Lock переводит модем в «только 4G».

        Только если режимы модема прочитаны и среди них нет 5G: пока
        net-mode-list не прочитан, режим сети не меняется — иначе
        5G-модем, занятый при подключении, потерял бы 5G.
        """
        return self.caps_known and not self.supports_5g

    def open(self) -> dict[str, Any]:
        """Вход в роутер (или повторный вход). Старая сессия закрывается."""
        with self._lock:
            self._close_locked()
            connection, client = self._factory(
                self.url, self.username, self.password, self.timeout)
            try:
                info = client.device.information() or {}
            except Exception:
                _safe_close(connection)
                raise
            self._connection, self._client = connection, client
            self.device_info = dict(info)
            self._tick = 0
            self._cache.clear()
            self._last_cell = None
            self._read_capabilities_locked()
            if self._data_off_pending:
                # reattach выключил данные, а старая сессия не успела их включить.
                self._restore_mobile_data_locked()
            return self.device_info

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _restore_mobile_data_locked(self) -> None:
        """Не оставляем мобильные данные выключенными после reattach."""
        try:
            self._require().dial_up.set_mobile_dataswitch(1)
            self._data_off_pending = False
        except Exception:
            logger.warning("Could not re-enable mobile data", exc_info=True)

    def _close_locked(self) -> None:
        if self._data_off_pending and self._client is not None:
            self._restore_mobile_data_locked()
        connection = self._connection
        self._connection = None
        self._client = None
        if connection is not None:
            _safe_close(connection)

    def _require(self) -> Any:
        if self._client is None:
            raise ConnectionError("Нет сессии с роутером")
        return self._client

    def _read_capabilities_locked(self) -> None:
        if self.original_net_mode is None:
            self.original_net_mode = self._read_net_mode_locked()
        try:
            nml = self._require().net.net_mode_list()
        except Exception as exc:
            _reraise_if_link_lost(exc)
            logger.debug("net_mode_list unavailable", exc_info=True)
            return
        self.supported_bands = parse_supported_bands(nml)
        self.access_modes = parse_access_modes(nml)
        self.caps_known = True

    # ---- опрос ----

    def _optional_getters(self, client: Any) -> dict[str, Callable[[], Any]]:
        return {
            'plmn': client.net.current_plmn,
            'status': client.monitoring.status,
            'traffic': client.monitoring.traffic_statistics,
            'month': client.monitoring.month_statistics,
            'dataswitch': client.dial_up.mobile_dataswitch,
        }

    def fetch(self) -> dict[str, Any]:
        """Один тик опроса. Каждый эндпоинт — со своей частотой (POLL_EVERY)."""
        with self._lock:
            client = self._require()
            tick = self._tick
            self._tick += 1
            signal = dict(client.device.signal() or {})
            cell = (signal.get('earfcn'), signal.get('pci'))
            cell_changed = self._last_cell is not None and cell != self._last_cell
            self._last_cell = cell

            for name, getter in self._optional_getters(client).items():
                if name in self._unsupported:
                    continue
                every = POLL_EVERY.get(name, 1)
                due = (tick % every == 0 or name not in self._cache
                       or (name == 'plmn' and cell_changed))
                if not due:
                    continue
                try:
                    self._cache[name] = dict(getter() or {})
                except Exception as exc:
                    kind = classify_error(exc)
                    if kind in (ErrorKind.NETWORK, ErrorKind.SESSION):
                        raise
                    if kind is ErrorKind.NOT_SUPPORTED:
                        self._unsupported.add(name)
                        self._cache.pop(name, None)
                    logger.debug("Optional endpoint %s failed", name, exc_info=True)

            merged: dict[str, Any] = dict(signal)
            for name in ('plmn', 'status', 'traffic', 'month', 'dataswitch'):
                merged.update(self._cache.get(name, {}))
            return merged

    # ---- настройки сети ----

    def _read_net_mode_locked(self) -> NetModeSettings | None:
        try:
            return NetModeSettings.from_response(self._require().net.net_mode())
        except Exception as exc:
            _reraise_if_link_lost(exc)
            logger.debug("net_mode unavailable", exc_info=True)
            return None

    def read_net_mode(self) -> NetModeSettings | None:
        with self._lock:
            return self._read_net_mode_locked()

    def _net_mode_for_write(self) -> NetModeSettings:
        """Текущие настройки сети для read-modify-write перед записью.

        Если их не удалось прочитать (роутер занят и т.п.), берутся
        прочитанные при подключении: программа меняет только LTE-бэнды,
        а NetworkBand (2G/3G) и режим сети 5G-модема не трогает, поэтому
        они совпадают с исходными. Если нет и их — NetModeUnavailable:
        иначе в модем ушли бы значения по умолчанию.
        """
        with self._lock:
            current = self._read_net_mode_locked()
            if current is not None and self.original_net_mode is None:
                # Исходные настройки фиксируются до первой записи программы.
                self.original_net_mode = current
        current = current or self.original_net_mode
        if current is None:
            raise NetModeUnavailable()
        return current

    def plan_band_lock(self, bands: Iterable[int]) -> BandLockPlan:
        """План фиксации. Для 5G-модемов режим сети не меняется."""
        current = self._net_mode_for_write()
        with self._lock:
            if not self.caps_known:
                self._read_capabilities_locked()    # при подключении не прочитались
        return plan_lock(current, bands, force_lte_only=self.locks_lte_only)

    def plan_all_bands(self) -> BandLockPlan:
        return plan_auto(self._net_mode_for_write(), self.original_net_mode)

    def plan_restore_original(self) -> BandLockPlan | None:
        if self.original_net_mode is None:
            return None
        return plan_restore(self.original_net_mode)

    def apply_plan(self, plan: BandLockPlan) -> bool:
        """Записывает план и перечитывает настройки. True — подтверждено."""
        with self._lock:
            client = self._require()
            client.net.set_net_mode(plan.lteband, plan.networkband,
                                    _network_mode_arg(plan.networkmode))
            try:
                read_back = self._read_net_mode_locked()
            except Exception:
                # Запись прошла, а перечитать не удалось — «не подтверждено»,
                # а не «роутер отклонил команду».
                logger.warning("Net mode read-back failed after write", exc_info=True)
                read_back = None
        return verify(plan, read_back)

    # ---- антенна, перезагрузка, связь ----

    def read_antenna(self) -> int | None:
        """Текущий режим антенны: первый геттер, который дал код 0..3.

        Hardware validation required: какой геттер отвечает на B636/B535.
        """
        with self._lock:
            device = self._require().device
            for getter in ('get_antenna_settings', 'antenna_type',
                           'antenna_status', 'antenna_set_type'):
                try:
                    res = getattr(device, getter)()
                except Exception as exc:
                    _reraise_if_link_lost(exc)
                    continue
                logger.info("Antenna %s -> %r", getter, res)
                code = parse_antenna_response(res)
                if code is not None:
                    return code
            return None

    def set_antenna(self, code: int) -> None:
        from huawei_lte_api.enums.device import AntennaTypeEnum
        with self._lock:
            self._require().device.set_antenna_settings(AntennaTypeEnum(code))

    def read_config(self) -> RouterConfig:
        net = self.read_net_mode()
        return RouterConfig(net, locked_bands(net, self.supported_bands),
                            self.read_antenna())

    def reboot(self) -> None:
        from huawei_lte_api.enums.device import ControlModeEnum
        with self._lock:
            self._require().device.set_control(ControlModeEnum.REBOOT)

    def reattach(self, pause: float = 3.0,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
        """Переподключить мобильную связь без перезагрузки роутера.

        Выключает и снова включает мобильные данные: модем отпускает
        соединение и заново выбирает лучшую соту — полезно после поворота
        антенны. Включение повторяется до трёх раз; флаг гарантирует, что
        close() включит данные, даже если приложение закрыли в паузе.
        Hardware validation required.
        """
        with self._lock:
            client = self._require()
            # Флаг — до запроса: если роутер выключил данные, а ответ
            # потерялся, close()/open() всё равно включат их обратно.
            self._data_off_pending = True
            client.dial_up.set_mobile_dataswitch(0)
        sleep(pause)
        for attempt in range(3):
            try:
                with self._lock:
                    self._require().dial_up.set_mobile_dataswitch(1)
                    self._data_off_pending = False
                return True
            except Exception:
                logger.warning("Re-enable mobile data failed (attempt %d)",
                               attempt + 1, exc_info=True)
                sleep(1.0)
        return False

    # ---- диагностика ----

    def collect_diagnostics(self) -> tuple[dict[str, Any], dict[str, str]]:
        """Сырые ответы GET-эндпоинтов для отчёта об ошибке (только чтение)."""
        dumps: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with self._lock:
            client = self._require()
            calls: dict[str, Callable[[], Any]] = {
                'device_information': client.device.information,
                'device_signal': client.device.signal,
                'device_antenna_settings': client.device.get_antenna_settings,
                'net_current_plmn': client.net.current_plmn,
                'net_net_mode': client.net.net_mode,
                'net_net_mode_list': client.net.net_mode_list,
                'net_cell_info': client.net.cell_info,
                'monitoring_status': client.monitoring.status,
                'monitoring_traffic_statistics': client.monitoring.traffic_statistics,
                'dialup_mobile_dataswitch': client.dial_up.mobile_dataswitch,
            }
            for name, fn in calls.items():
                try:
                    dumps[name] = fn()
                except Exception as exc:
                    errors[name] = f"{type(exc).__name__}: {exc}"
        return dumps, errors


class SessionWorker(threading.Thread):
    """Фоновый поток: вход, опрос, переподключение, выход.

    Колбэки вызываются из этого потока — UI сам переносит их в свой
    главный поток. Закрытие сессии тоже выполняется здесь, поэтому
    stop() никогда не блокирует интерфейс.

    Политика ошибок:
      * сетевая ошибка — повтор на той же сессии; после
        RECONNECT_REOPEN_AFTER ошибок подряд — повторный вход;
      * истёкшая сессия — сразу повторный вход;
      * ошибки входа (пароль, блокировка, «уже вошли») — стоп: повторные
        попытки не помогут и могут заблокировать вход на роутере.
    """

    def __init__(self, session: RouterSession, *,
                 interval: Callable[[], float],
                 on_connected: Callable[[dict[str, Any]], None],
                 on_snapshot: Callable[[Snapshot], None],
                 on_status: Callable[[str, float | None, BaseException | None], None],
                 on_fatal: Callable[[BaseException], None],
                 on_stopped: Callable[[], None] | None = None,
                 auto_reconnect: bool = True) -> None:
        super().__init__(daemon=True, name="hua4gmon-session")
        self.session = session
        self.auto_reconnect = auto_reconnect
        self._interval = interval
        self._on_connected = on_connected
        self._on_snapshot = on_snapshot
        self._on_status = on_status
        self._on_fatal = on_fatal
        self._on_stopped = on_stopped
        self._stop_evt = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def run(self) -> None:
        try:
            try:
                info = self.session.open()
            except Exception as exc:
                if not self._stop_evt.is_set():
                    self._on_fatal(exc)
                return
            if self._stop_evt.is_set():
                return
            self._on_connected(info)
            self._loop()
        finally:
            self.session.close()
            if self._on_stopped is not None:
                self._on_stopped()

    def _loop(self) -> None:
        failures = 0
        delay = RECONNECT_DELAY_INITIAL
        while not self._stop_evt.is_set():
            if not self._running.is_set():
                self._running.wait()
                continue
            try:
                raw = self.session.fetch()
            except Exception as exc:
                kind = classify_error(exc)
                if kind is ErrorKind.LOGIN_FATAL or not self.auto_reconnect:
                    self._on_fatal(exc)
                    return
                failures += 1
                # Трассировка — для ошибок, не похожих на обрыв связи (баг в разборе).
                logger.warning("Poll failed (%s): %s", kind.value, exc,
                               exc_info=kind is ErrorKind.OTHER)
                self._on_status('reconnecting', delay, exc)
                if self._stop_evt.wait(delay):
                    return
                delay = min(delay * 2, RECONNECT_DELAY_MAX)
                if kind is ErrorKind.SESSION or failures >= RECONNECT_REOPEN_AFTER:
                    try:
                        self.session.open()
                    except Exception as exc2:
                        if classify_error(exc2) is ErrorKind.LOGIN_FATAL:
                            self._on_fatal(exc2)
                            return
                        logger.warning("Re-login failed: %s", exc2)
                        continue
                    if self._stop_evt.is_set():
                        return
                    # «Подключено» — только после успешного опроса (ниже):
                    # вход удался, но данные могут по-прежнему не идти.
                continue
            if failures:
                failures = 0
                delay = RECONNECT_DELAY_INITIAL
                self._on_status('connected', None, None)
            try:
                snap = build_snapshot(raw, self.session.supported_bands)
            except Exception:
                logger.exception("Snapshot build failed — tick skipped")
            else:
                self._on_snapshot(snap)
            if self._stop_evt.wait(max(MIN_POLL_INTERVAL, float(self._interval()))):
                return
