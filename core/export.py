"""
Экспорт: CSV сессии (совместимый с Excel) и диагностический отчёт.

CSV:
  * UTF-8 с BOM — Excel сразу распознаёт кодировку;
  * для русского интерфейса — разделитель «;» и десятичная запятая
    (так Excel с русскими региональными настройками открывает файл по
    колонкам), для английского — «,» и точка.

Диагностика: сырые ответы API для отчёта об ошибке. Идентификаторы
(IMEI, IMSI, ICCID, номер, серийный номер) маскируются с сохранением
последних 4 символов, сетевые адреса (любые MAC, в том числе Wi-Fi, IP, DNS)
— полностью. Номер соты и оператор остаются: без них диагностика бесполезна.
"""
from __future__ import annotations

import csv
import datetime
import json
from collections.abc import Iterable, Mapping
from typing import Any

CSV_FIELDS: tuple[str, ...] = (
    'ts', 'rsrp', 'rssi', 'sinr', 'rsrq', 'nr_rsrp', 'nr_sinr', 'plmn',
    'enodeb', 'sector', 'pci', 'earfcn', 'bands', 'rat',
)

# Части имён ключей (без учёта регистра), значения которых маскируются.
# Идентификаторы: последние 4 символа остаются — чтобы различать устройства.
ID_KEY_PARTS: tuple[str, ...] = ('imei', 'imsi', 'iccid', 'msisdn', 'serialnumber')
# Сетевые адреса — полностью: по MAC Wi-Fi (BSSID) общедоступные базы
# находят место установки, по IP и DNS — провайдера и район.
ADDRESS_KEY_PARTS: tuple[str, ...] = ('mac', 'dns', 'ipaddr', 'ipv4', 'ipv6address',
                                      'ip_address', 'wan_ip')


def csv_dialect(lang: str) -> tuple[str, str]:
    """(разделитель, десятичный знак) под язык интерфейса."""
    return (';', ',') if lang == 'ru' else (',', '.')


def _cell(value: Any, decimal: str) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        text = str(int(value)) if value.is_integer() else f"{value:g}"
        return text.replace('.', decimal)
    return str(value)


def write_session_csv(path: str, rows: Iterable[Mapping[str, Any]],
                      lang: str = 'ru') -> int:
    """Пишет лог сессии. Возвращает число записанных строк."""
    delimiter, decimal = csv_dialect(lang)
    count = 0
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(CSV_FIELDS)
        for row in rows:
            writer.writerow([_cell(row.get(k), decimal) for k in CSV_FIELDS])
            count += 1
    return count


def default_csv_name(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"hua4gmon-{now:%Y%m%d-%H%M%S}.csv"


def mask_value(value: Any, keep: int = 4) -> str:
    """Оставляет последние keep символов: '860000000000001' → '***********0001'."""
    s = str(value)
    if len(s) <= keep:
        return '*' * len(s)
    return '*' * (len(s) - keep) + (s[-keep:] if keep else '')


def _kept_chars(key: str) -> int | None:
    """Сколько символов значения оставить; None — ключ не секретный."""
    k = key.lower()
    if any(part in k for part in ADDRESS_KEY_PARTS):
        return 0
    if any(part in k for part in ID_KEY_PARTS):
        return 4
    return None


def mask_sensitive(data: Any, keep: int | None = None) -> Any:
    """Рекурсивно маскирует идентификаторы и сетевые адреса в ответах API.

    keep — для значений под секретным ключом (в том числе элементов списка
    вида {'MacAddress': ['AA:…', 'BB:…']}); вложенные словари проверяются
    по своим ключам.
    """
    if isinstance(data, Mapping):
        return {str(k): mask_sensitive(v, _kept_chars(str(k))) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_sensitive(v, keep) for v in data]
    if keep is not None and data not in (None, ''):
        return mask_value(data, keep)
    return data


def build_diagnostics(*, app_version: str, library_version: str,
                      platform: str, dumps: Mapping[str, Any],
                      errors: Mapping[str, str],
                      now: datetime.datetime | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now()
    return {
        'generated': now.isoformat(timespec='seconds'),
        'app': 'Hua4GMon',
        'app_version': app_version,
        'huawei_lte_api': library_version,
        'platform': platform,
        'dumps': mask_sensitive(dict(dumps)),
        'errors': dict(errors),
    }


def diagnostics_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
