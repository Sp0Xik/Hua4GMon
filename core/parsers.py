"""
Разбор и форматирование сырых строк от роутера Huawei.

Эти функции — чистые. Никаких побочных эффектов, никакого Tk.
Принимают сырое значение из API и возвращают типизированный результат.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from core.constants import ANTENNA_MODES, BAND_FREQ_MAP, BANDS, EARFCN_RANGES

# Регулярка для базовой валидации IPv4. Полная проверка диапазона — в is_valid_ip.
_IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')


def is_valid_ip(s: str) -> bool:
    """Базовая валидация IPv4."""
    if not s or not _IP_RE.match(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


def extract_number(val: Any) -> Optional[float]:
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


def parse_cell_id(raw: Any) -> Tuple[Optional[int], Optional[int]]:
    """Парсит cell_id из Huawei API. Возвращает (eNodeB_id, sector)."""
    if raw is None or raw == '':
        return None, None
    s = str(raw).strip()
    try:
        if s.lower().startswith('0x'):
            cid = int(s, 16)
        elif any(c in 'abcdefABCDEF' for c in s):
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


def parse_antenna_value(label: str) -> Optional[int]:
    """Достаёт целочисленный код режима антенны из локализованной метки."""
    base = label.split('(')[0].strip()
    if base in ANTENNA_MODES:
        return ANTENNA_MODES[base]
    m = re.search(r'\((\d+)\)', label)
    if m:
        return int(m.group(1))
    return None


def earfcn_to_band(earfcn: Any) -> Optional[int]:
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


def _format_band_list(bands: List[int]) -> str:
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
