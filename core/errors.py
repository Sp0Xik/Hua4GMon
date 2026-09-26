"""
Классификация и понятные сообщения для ошибок связи с роутером.

Модуль не импортирует huawei_lte_api и requests:
  * у исключений huawei_lte_api есть атрибут ``code`` (код ошибки API);
  * исключения requests (ConnectionError, Timeout) наследуются от OSError.
"""
from __future__ import annotations

import enum

from core.i18n import t


class NetModeUnavailable(Exception):
    """Настройки сети модема не прочитаны — запись Band Lock отменена."""


class ErrorKind(enum.Enum):
    NETWORK = 'network'            # роутер недоступен / таймаут
    SESSION = 'session'            # сессия истекла — нужен повторный вход
    LOGIN_FATAL = 'login_fatal'    # повторный вход не поможет (пароль и т.п.)
    NOT_SUPPORTED = 'not_supported'
    BUSY = 'busy'
    OTHER = 'other'


# Коды ошибок входа (huawei_lte_api.enums.user.LoginErrorEnum).
LOGIN_FATAL_CODES = frozenset({108001, 108002, 108003, 108006, 108007, 115002})
# Коды истёкшей сессии/токена (huawei_lte_api.enums.client.ResponseCodeEnum).
SESSION_CODES = frozenset({100003, 125001, 125002, 125003})
NOT_SUPPORTED_CODES = frozenset({100002})
BUSY_CODES = frozenset({100004})

_MESSAGES: dict[int, str] = {
    108001: "Неверное имя пользователя.",
    108002: "Неверный пароль.",
    108006: "Неверный логин или пароль. Пароль — на наклейке роутера.",
    108007: "Слишком много неудачных попыток входа — роутер временно "
            "заблокировал вход. Подождите несколько минут и проверьте пароль.",
    108003: "Уже выполнен вход с другого устройства — закройте "
            "веб-интерфейс роутера и повторите.",
    115002: "Роутер требует сменить пароль — сделайте это в веб-интерфейсе.",
    100002: "Функция не поддерживается этой моделью или прошивкой.",
    100003: "Сессия истекла — нужно войти заново.",
    100004: "Роутер занят — повторите через несколько секунд.",
    125001: "Сессия устарела — нужно войти заново.",
    125002: "Сессия устарела — нужно войти заново.",
    125003: "Сессия устарела — нужно войти заново.",
}


def error_code(exc: BaseException) -> int | None:
    code = getattr(exc, 'code', None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def classify_error(exc: BaseException) -> ErrorKind:
    code = error_code(exc)
    if code in LOGIN_FATAL_CODES:
        return ErrorKind.LOGIN_FATAL
    if code in SESSION_CODES:
        return ErrorKind.SESSION
    if code in NOT_SUPPORTED_CODES:
        return ErrorKind.NOT_SUPPORTED
    if code in BUSY_CODES:
        return ErrorKind.BUSY
    if isinstance(exc, OSError):            # TimeoutError и ошибки requests — тоже OSError
        return ErrorKind.NETWORK
    return ErrorKind.OTHER


def humanize_error(exc: BaseException) -> str:
    """Короткое понятное сообщение на текущем языке."""
    code = error_code(exc)
    if code in _MESSAGES:
        return t(_MESSAGES[code])
    if isinstance(exc, NetModeUnavailable):
        return t("Не удалось прочитать настройки модема — запись отменена, модем "
                 "не изменён. Повторите через несколько секунд.")
    if isinstance(exc, OSError):
        return t("Роутер не отвечает: проверьте подключение к его Wi-Fi/USB "
                 "и IP-адрес.")
    text = str(exc).strip() or exc.__class__.__name__
    return text if len(text) <= 200 else text[:200] + "…"
