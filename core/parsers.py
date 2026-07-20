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


def _earfcn_dl(earfcn: Any) -> Any:
    """Достаёт DL-EARFCN. Роутер может отдать число (200) или строку
    вида 'DL:200 UL:18200' — берём именно DL."""
    if earfcn in (None, '', '-'):
        return None
    s = str(earfcn)
    m = re.search(r'DL[:\s]*(\d+)', s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'\d+', s)
    return int(m.group(0)) if m else None


def format_band_label(band_raw: Any, earfcn: Any = None) -> str:
    """Человекочитаемая метка активного LTE-band.

    ВАЖНО: primary-band определяется в первую очередь по EARFCN — это
    надёжный признак АКТИВНОГО канала. Поле ``band`` у части роутеров
    (например Huawei B636) содержит список поддерживаемых/сконфигуриро-
    ванных бэндов, а не активную агрегацию, поэтому доверять ему для
    определения рабочего бэнда нельзя. Факт агрегации показывается
    отдельно (поле «Агрегация (CA)»).

    Понимает форматы band: "LTE BAND 7", "7", "B7", "B7+B20", "0x40".
    """
    # 1. EARFCN — приоритет (активный primary-band).
    b = earfcn_to_band(_earfcn_dl(earfcn))
    if b is not None:
        freq = BAND_FREQ_MAP.get(b, '')
        return f"B{b}" + (f" ({freq} МГц)" if freq else "")

    # 2. EARFCN недоступен — разбираем поле band как раньше.
    if band_raw not in (None, '', '-'):
        s = str(band_raw).strip()
        # Hex-маска вида 0x40
        if s.lower().startswith('0x'):
            try:
                mask = int(s, 16)
                hits = [b for name, val in BANDS.items()
                        for b in [int(re.search(r'B(\d+)', name).group(1))]
                        if mask & val]
                if hits:
                    return _format_band_list(hits)
            except (ValueError, AttributeError):
                pass
        nums = [int(n) for n in re.findall(r'\d+', s)
                if 1 <= int(n) <= 100]
        if nums:
            return _format_band_list(nums)
        return s
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


# TM (Transmission Mode, 3GPP TS 36.213) → человекочитаемая схема MIMO.
# Роутер Huawei отдаёт режим как "TM[4]" / "4".
_TM_MIMO = {
    1: "1x1 (SISO)",
    2: "2x2 (Tx div)",
    3: "2x2 (open-loop)",
    4: "2x2 (closed-loop)",
    5: "MU-MIMO",
    6: "1-layer",
    7: "single-layer",
    8: "2-layer",
    9: "4x4",
    10: "4x4",
}


def format_mimo(value: Any) -> str:
    """'TM[4]' / '4' → '2x2 (closed-loop) [TM4]'. Неизвестное — как есть."""
    if value in (None, ''):
        return "-"
    s = str(value)
    m = re.search(r'\d+', s)
    if not m:
        return s
    tm = int(m.group(0))
    label = _TM_MIMO.get(tm)
    return f"{label} [TM{tm}]" if label else f"TM{tm}"


def format_modulation(raw: Any) -> str | None:
    """Модуляция → компактный вид.

    Роутер отдаёт либо MCS-индекс числом (5, 27), либо подробную строку
    вида 'mcsDownCarrier1Code0:27@256QAM mcsDownCarrier1Code1:27@256QAM'
    (несколько carrier/codeword). Приводим к короткому '256QAM (MCS 27)'
    или '256QAM (MCS 23/27)', если MCS разные. None — если не разобрать.
    """
    if raw in (None, ''):
        return None
    s = str(raw)
    # Строка с парами MCS@модуляция
    pairs = re.findall(r'(\d+)@(\w*QAM)', s, re.IGNORECASE)
    if pairs:
        mcs = sorted({int(m) for m, _ in pairs})
        qam = list(dict.fromkeys(q.upper() for _, q in pairs))  # уник, порядок
        qam_str = " + ".join(qam)
        mcs_str = "/".join(str(m) for m in mcs)
        return f"{qam_str} (MCS {mcs_str})"
    # Просто MCS-индекс числом
    mod = mcs_to_modulation(raw)
    if mod is not None:
        return f"{mod} (MCS {int(extract_number(raw))})"
    return None


def parse_antenna_response(res: Any) -> int | None:
    """Извлекает код режима антенны (0..3) из ответа Huawei API.

    Имена полей различаются между моделями/endpoint (antennatype,
    antenna_type, antennaType, type, mode, curtype…), поэтому:
      1. пробуем известные ключи;
      2. затем — любой ключ, содержащий 'antenna' или 'type'/'mode',
         значение которого приводится к числу 0..3.
    Возвращает код или None.
    """
    if res is None:
        return None
    # Не-dict (число/строка) — пробуем напрямую
    if not isinstance(res, dict):
        n = extract_number(res)
        return int(n) if n is not None and 0 <= n <= 3 else None

    known = ('antennatype', 'antenna_type', 'antennaType', 'AntennaType',
             'curtype', 'type', 'Type', 'mode', 'Mode', 'antennamode')
    for k in known:
        if k in res:
            n = extract_number(res[k])
            if n is not None and 0 <= n <= 3:
                return int(n)
    # Эвристика: любой «antenna/type/mode» ключ с числом 0..3
    for k, v in res.items():
        kl = str(k).lower()
        if 'antenna' in kl or 'type' in kl or 'mode' in kl:
            n = extract_number(v)
            if n is not None and 0 <= n <= 3:
                return int(n)
    return None
