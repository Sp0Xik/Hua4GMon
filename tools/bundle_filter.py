"""Отбор файлов для Windows-сборки (используется packaging/windows.spec).

Однофайловый .exe при каждом запуске распаковывает всё содержимое во
временную папку, а антивирус заново проверяет каждый распакованный файл:
на слабом ноутбуке это минуты. Поэтому в сборку не попадает то, что
программа никогда не загружает:
  * нативные модули pycryptodomex, кроме нужных huawei-lte-api
    (Tools.rsa_encrypt: RSA, PKCS#1 v1.5 и OAEP с SHA-1);
  * данные Tcl для команды clock — часовые пояса и названия месяцев
    (программа не вызывает clock, время форматирует Python);
  * картинки из примеров Tk.
Что всё нужное на месте, проверяет `Hua4GMon.exe --self-test` в CI,
а совпадение списка с реальной загрузкой — tests/test_bundle_filter.py.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

# Нативные модули pycryptodomex, которые загружает huawei-lte-api, когда
# длинная арифметика идёт без GMP (как на Windows: колёса без libgmp).
CRYPTO_NATIVES = frozenset({
    "Cryptodome/Cipher/_pkcs1_decode",
    "Cryptodome/Hash/_SHA1",
    "Cryptodome/Math/_modexp",
    "Cryptodome/Util/_strxor",
})

# Каталоги данных Tcl/Tk (в раскладке PyInstaller), которые программа
# не использует.
UNUSED_TCLTK_DIRS = frozenset({
    ("_tcl_data", "tzdata"),
    ("_tcl_data", "msgs"),
    ("_tk_data", "images"),
})

TocEntry = tuple[str, str, str]      # (путь в сборке, исходный файл, тип)


def _parts(dest: str) -> list[str]:
    return dest.replace("\\", "/").split("/")


def keep_binary(dest: str) -> bool:
    """Нужен ли двоичный файл сборки (путь назначения в TOC PyInstaller)."""
    parts = _parts(dest)
    if parts[0] != "Cryptodome":
        return True
    module = "/".join([*parts[:-1], parts[-1].split(".")[0]])
    return module in CRYPTO_NATIVES


def keep_data(dest: str) -> bool:
    """Нужен ли файл данных сборки."""
    return tuple(_parts(dest)[:2]) not in UNUSED_TCLTK_DIRS


def trim(entries: Iterable[TocEntry], keep: Callable[[str], bool]) -> list[TocEntry]:
    """Оставляет записи TOC, которые нужны программе."""
    return [entry for entry in entries if keep(entry[0])]
