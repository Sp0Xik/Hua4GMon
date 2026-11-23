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
import re
from collections.abc import Iterable, Mapping
from typing import Any

CSV_FIELDS: tuple[str, ...] = (
    'ts', 'rsrp', 'rssi', 'sinr', 'rsrq', 'nr_rsrp', 'nr_sinr', 'plmn',
    'cell_id', 'enodeb', 'sector', 'pci', 'earfcn', 'bands', 'rat', 'registration', 'connection',
    'nci', 'nr_pci', 'nr_arfcn', 'nr_rsrq', 'lte_carriers',
)

# Части имён ключей (без учёта регистра), значения которых маскируются.
# Идентификаторы: последние 4 символа остаются — чтобы различать устройства.
ID_KEY_PARTS: tuple[str, ...] = ('imei', 'imsi', 'iccid', 'msisdn', 'serialnumber')
# Сетевые адреса — полностью: по MAC Wi-Fi (BSSID) общедоступные базы
# находят место установки, по IP и DNS — провайдера и район.
ADDRESS_KEY_PARTS: tuple[str, ...] = ('mac', 'dns', 'ipaddr', 'ipv4', 'ipv6address',
                                      'ip_address', 'wan_ip', 'bssid', 'gateway')


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
        return {str(k): mask_sensitive(v, keep if keep is not None else _kept_chars(str(k))) for k, v in data.items()}
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
        'errors': {name: redact_error(text) for name, text in errors.items()},
    }


def redact_error(text: str) -> str:
    """Exception messages are unstructured: remove URLs, hosts and addresses."""
    text = re.sub(r'https?://[^\s\x27\x22<>]+', '[URL]', str(text), flags=re.I)
    text = re.sub(r'(\b(?:host|hostname|server)\s*[=:]\s*)[\x27\x22]?[^\s,\x27\x22)]+[\x27\x22]?',
                  r'\1[HOST]', text, flags=re.I)
    text = re.sub(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', '[IP]', text)
    text = re.sub(r'(?<!\w)(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]*(?:%[\w.-]+)?',
                  '[IP]', text, flags=re.I)
    text = re.sub(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b', '[HOST]', text, flags=re.I)
    return re.sub(r' at 0x[0-9a-f]+', '', text, flags=re.I)


def diagnostics_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
