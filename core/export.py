"""
Экспорт: CSV сессии (совместимый с Excel) и диагностический отчёт.

CSV:
  * UTF-8 с BOM — Excel сразу распознаёт кодировку;
  * для русского интерфейса — разделитель «;» и десятичная запятая
    (так Excel с русскими региональными настройками открывает файл по
    колонкам), для английского — «,» и точка.

Диагностика: сырые ответы API для отчёта об ошибке. Идентификаторы
(IMEI, IMSI, ICCID, номер, серийный номер, MAC, WAN IP) маскируются.
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

SENSITIVE_KEYS = frozenset(k.lower() for k in (
    'Imei', 'Imsi', 'Iccid', 'Msisdn', 'SerialNumber', 'MacAddress1',
    'MacAddress2', 'WanIPAddress', 'WanIPv6Address', 'wan_ip', 'Mac',
    'PrimaryDns', 'SecondaryDns', 'PrimaryIPv6Dns', 'SecondaryIPv6Dns',
))


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


def mask_value(value: Any) -> str:
    """Оставляет последние 4 символа: '860000000000001' → '***********0001'."""
    s = str(value)
    if len(s) <= 4:
        return '*' * len(s)
    return '*' * (len(s) - 4) + s[-4:]


def mask_sensitive(data: Any) -> Any:
    """Рекурсивно маскирует идентификаторы в ответах API."""
    if isinstance(data, Mapping):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_KEYS and v not in (None, ''):
                out[str(k)] = mask_value(v)
            else:
                out[str(k)] = mask_sensitive(v)
        return out
    if isinstance(data, list):
        return [mask_sensitive(v) for v in data]
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
