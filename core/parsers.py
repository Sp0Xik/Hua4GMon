"""
Разбор и форматирование сырых строк от роутера Huawei.

Эти функции — чистые. Никаких побочных эффектов, никакого Tk.
Принимают сырое значение из API и возвращают типизированный результат.
"""
from __future__ import annotations

import re
from typing import Any

from core.constants import ANTENNA_MODES, BAND_FREQ_MAP, BANDS, EARFCN_RANGES

# Регулярка для базовой валидации IPv4. Полная проверка диапазона — в is_valid_ip.
_IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')


def is_valid_ip(s: str) -> bool:
    """Базовая валидация IPv4."""
    if not s or not _IP_RE.match(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


def extract_number(val: Any) -> float | None:
    """Строгое извлечение числа. Не ведётся на строки вроде 'timeout 0'."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ('-', 'None', 'N/A', 'NA'):
        return None
    # Допускаем знак, дробную часть и опциональный суффикс (dBm, %, dB и т.п.)
    m = re.fullmatch(r'(-?\d+(?:\.\d+)?)\s*[a-zA-Z%/]*', s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_cell_id(raw: Any) -> tuple[int | None, int | None]:
    """Парсит cell_id из Huawei API. Возвращает (eNodeB_id, sector)."""
    if raw is None or raw == '':
        return None, None
    s = str(raw).strip()
    try:
        if s.lower().startswith('0x') or any(c in 'abcdefABCDEF' for c in s):
            cid = int(s, 16)
        else:
            cid = int(s)
    except (ValueError, TypeError):
        return None, None
    # Отбрасываем явные "плохие" значения
    if cid <= 0 or cid >= 0xFFFFFFFF:
        return None, None
    if cid > 0x0FFFFFFF:     # > 28 бит — не LTE CID
        return None, None
    return cid // 256, cid % 256


def parse_antenna_value(label: str) -> int | None:
    """Достаёт целочисленный код режима антенны из локализованной метки."""
    base = label.split('(')[0].strip()
    if base in ANTENNA_MODES:
        return ANTENNA_MODES[base]
    m = re.search(r'\((\d+)\)', label)
    if m:
        return int(m.group(1))
    return None


def earfcn_to_band(earfcn: Any) -> int | None:
    """EARFCN (DL channel) → номер LTE-band, или None если не определён."""
    try:
        e = int(earfcn)
    except (TypeError, ValueError):
        return None
    for lo, hi, band in EARFCN_RANGES:
        if lo <= e <= hi:
            return band
    return None


def format_band_label(band_raw: Any, earfcn: Any = None) -> str:
    """Человекочитаемая метка LTE-band.

    Понимает форматы:
      "LTE BAND 7", "7", "B7", "B7+B20", "7+20", "0x40".
    Если band недоступен — пытается определить по EARFCN.
    """
    if band_raw not in (None, '', '-'):
        s = str(band_raw).strip()
        # Hex-маска вида 0x40
        if s.lower().startswith('0x'):
            try:
                mask = int(s, 16)
                # Перебираем известные одиночные биты
                hits = [b for name, val in BANDS.items()
                        for b in [int(re.search(r'B(\d+)', name).group(1))]
                        if mask & val]
                if hits:
                    return _format_band_list(hits)
            except (ValueError, AttributeError):
                pass
        # Извлекаем все номера 1..100 (B1..B71, без false-positives).
        # Без \b — иначе не матчит "B3+B7" (B и 3 оба word-character).
        nums = [int(n) for n in re.findall(r'\d+', s)
                if 1 <= int(n) <= 100]
        if nums:
            return _format_band_list(nums)
        return s   # вернём как есть
    # Fallback по EARFCN
    if earfcn not in (None, '', '-'):
        b = earfcn_to_band(earfcn)
        if b is not None:
            freq = BAND_FREQ_MAP.get(b, '')
            tail = f" ({freq} МГц)" if freq else ""
            return f"≈ B{b}{tail} [по EARFCN={earfcn}]"
    return "-"


def _format_band_list(bands: list[int]) -> str:
    """Форматирует список номеров бандов в строку."""
    bands = list(dict.fromkeys(bands))   # дедуп с сохранением порядка
    if len(bands) == 1:
        b = bands[0]
        freq = BAND_FREQ_MAP.get(b, '')
        return f"B{b}" + (f" ({freq} МГц)" if freq else "")
    parts = []
    for b in bands:
        freq = BAND_FREQ_MAP.get(b, '')
        parts.append(f"B{b}" + (f"/{freq}" if freq else ""))
    return "CA: " + " + ".join(parts)


def format_bytes_mb(b: Any) -> str:
    """Сырые байты → '123.4 МБ' для UI."""
    try:
        return f"{int(b) / 1048576:.1f} МБ"
    except (TypeError, ValueError):
        return "-"


def format_rate_mbps(bps: Any) -> str:
    """Bytes/sec → 'X.YZ Мбит/с' для UI."""
    try:
        return f"{int(bps) * 8 / 1_000_000:.2f} Мбит/с"
    except (TypeError, ValueError):
        return "-"


def first_present(data: Any, keys: Any) -> Any:
    """Возвращает первое непустое значение по списку возможных ключей.

    Имена полей в ответе Huawei device/signal различаются между
    прошивками (dl_mcs / dlmcs / dlMcs и т.п.) — перебираем варианты.
    """
    try:
        for k in keys:
            v = data.get(k)
            if v not in (None, ''):
                return v
    except AttributeError:
        return None
    return None


def mcs_to_modulation(mcs: Any) -> str | None:
    """MCS-индекс → тип модуляции (LTE, 3GPP TS 36.213).

    Приближённо: точные границы зависят от используемой MCS-таблицы
    (с 256QAM они сдвинуты), поэтому тип помечается как ориентировочный
    на стороне вызывающего кода. None — если MCS не распознан.
    """
    n = extract_number(mcs)
    if n is None:
        return None
    n = int(n)
    if n < 0:
        return None
    if n <= 9:
        return "QPSK"
    if n <= 16:
        return "16QAM"
    if n <= 28:
        return "64QAM"
    if n <= 31:
        return "256QAM"
    return None


def bands_from_mask(mask: Any) -> list[str] | None:
    """Маска LTE-бэндов роутера (hex-строка) → список имён из BANDS.

    Возвращает:
        * список имён бэндов, отмеченных в маске;
        * [] — если маска соответствует AUTO (включены все бэнды);
        * None — если маску не удалось разобрать.

    Роутер отдаёт маску в net/net-mode как hex-строку, напр. '44'
    (B3+B7) или '7FFFFFFFFFFFFFFF' (все = AUTO).
    """
    if mask in (None, ''):
        return None
    s = str(mask).strip()
    try:
        val = int(s, 16)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    # AUTO: роутер вернул «все бэнды» — трактуем как отсутствие фиксации.
    # Проверяем, что установлены все биты известных нам бэндов И маска
    # заметно шире нашего набора (значит это общая AUTO-маска).
    known = 0
    for v in BANDS.values():
        known |= v
    if (val & known) == known and val > known:
        return []
    return [name for name, bit in BANDS.items() if val & bit]
