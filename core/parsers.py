"""
Разбор и форматирование сырых строк от роутера Huawei.

Эти функции — чистые. Никаких побочных эффектов, никакого UI.
Принимают сырое значение из API и возвращают типизированный результат.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from core.constants import (
    ANTENNA_MODES,
    BAND_FREQ_MAP,
    EARFCN_RANGES,
    LTE_ALL_MASK,
    LTE_BAND_TABLE,
    REGION_BANDS,
)
from core.i18n import t

# Регулярка для базовой валидации IPv4. Полная проверка диапазона — в is_valid_ip.
_IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
# Число с необязательным знаком сравнения («>=-51dBm», «<-140dBm» — так
# часть прошивок отдаёт граничные значения) и суффиксом единиц.
_NUMBER_RE = re.compile(r'(?:[<>]=?)?\s*(-?\d+(?:\.\d+)?)\s*[a-zA-Z%/]*')
_BANDWIDTH_RE = re.compile(r'\d+(?:\.\d+)?\s*MHz', re.IGNORECASE)


def is_valid_ip(s: str) -> bool:
    """Базовая валидация IPv4."""
    if not s or not _IP_RE.match(s):
        return False
    return all(0 <= int(p) <= 255 for p in s.split('.'))


def extract_number(val: Any) -> float | None:
    """Строгое извлечение числа. Не ведётся на строки вроде 'timeout 0'.

    Понимает граничные значения вида '>=-51dBm' и '<-140dBm': возвращает
    саму границу — это лучшая доступная оценка.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ('-', '--', 'None', 'N/A', 'NA'):
        return None
    m = _NUMBER_RE.fullmatch(s)
    if not m:
        return None
    return float(m.group(1))


def to_int(val: Any) -> int | None:
    """Целое из сырого значения API (с единицами или без) или None."""
    n = extract_number(val)
    return int(n) if n is not None else None


def parse_cell_id(raw: Any) -> tuple[int | None, int | None]:
    """Парсит cell_id (ECI) из Huawei API. Возвращает (eNodeB_id, sector)."""
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
    # Отбрасываем явные «плохие» значения и всё, что шире 28 бит (не LTE ECI).
    if cid <= 0 or cid > 0x0FFFFFFF:
        return None, None
    return cid // 256, cid % 256


def parse_nci(raw: Any) -> int | None:
    """NR Cell Identity (36 бит) или None.

    Длина gNB ID внутри NCI задаётся оператором (22–32 бита), поэтому
    разделить NCI на «вышку» и «сектор» без данных сети нельзя —
    возвращаем NCI целиком.
    """
    if raw is None or raw == '':
        return None
    s = str(raw).strip()
    try:
        if s.lower().startswith('0x') or any(c in 'abcdefABCDEF' for c in s):
            nci = int(s, 16)
        else:
            nci = int(s)
    except (ValueError, TypeError):
        return None
    if nci <= 0 or nci >= 1 << 36:
        return None
    return nci


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


def dl_earfcn(earfcn: Any) -> int | None:
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


def earfcn_to_freq_mhz(earfcn: Any) -> float | None:
    """Центральная частота DL, МГц: F = F_DL_low + 0.1·(N_DL − N_Offs-DL)."""
    n = dl_earfcn(earfcn)
    if n is None:
        return None
    band = earfcn_to_band(n)
    if band is None:
        return None
    _name, _duplex, f_low, offs, _n_max = LTE_BAND_TABLE[band]
    return round(f_low + 0.1 * (n - offs), 1)


def lte_band_label(band: int, mhz: str = "МГц") -> str:
    """Подпись бэнда для списков: 'B3 (1800 МГц)', 'B38 (TDD 2600)'."""
    info = LTE_BAND_TABLE.get(band)
    if info is None:
        return f"B{band}"
    name, duplex = info[0], info[1]
    if duplex == "TDD":
        return f"B{band} (TDD {name})"
    if duplex == "SDL":
        return f"B{band} (SDL {name})"
    return f"B{band} ({name} {mhz})" if name.isdigit() else f"B{band} ({name})"


def active_bands(band_raw: Any, earfcn: Any = None,
                 known: Iterable[int] | None = None) -> list[int]:
    """Активные LTE-бэнды: primary (по EARFCN) первым, затем остальные.

    Поле ``band`` у части роутеров (Huawei B636) содержит номера
    вперемешку, включая невозможные для региона. Поэтому из него берём
    только правдоподобные бэнды: поддерживаемые модемом (``known``,
    из net/net-mode-list) либо, если это неизвестно, типовые для РФ/СНГ.

    Понимает форматы band: "LTE BAND 7", "7", "B7", "B7+B20", "0x40".
    """
    allowed = frozenset(known) if known else frozenset(REGION_BANDS)
    primary = earfcn_to_band(dl_earfcn(earfcn))

    from_band: list[int] = []
    if band_raw not in (None, '', '-'):
        s = str(band_raw).strip()
        if s.lower().startswith('0x'):
            try:
                mask = int(s, 16)
            except ValueError:
                mask = 0
            from_band = [b for b in range(1, 64) if mask & (1 << (b - 1))]
        else:
            # Ширину канала («20MHz») убираем, чтобы 20 не стал бэндом B20.
            s = _BANDWIDTH_RE.sub(' ', s)
            from_band = [int(n) for n in re.findall(r'B(\d+)', s)
                         if int(n) in allowed]
            if not from_band:
                # Форматы без 'B': "7", "7+20", "LTE BAND 20".
                from_band = [int(n) for n in re.findall(r'\d+', s)
                             if int(n) in allowed]

    active: list[int] = []
    if primary is not None:
        active.append(primary)
    for n in from_band:
        if n not in active:
            active.append(n)
    return active


def format_band_list(bands: Iterable[int]) -> str:
    """Список номеров бэндов → 'B3 (1800+ МГц)' или 'CA: B3/1800+ + B1/2100'."""
    bands = list(dict.fromkeys(bands))   # дедуп с сохранением порядка
    if not bands:
        return "-"
    if len(bands) == 1:
        b = bands[0]
        freq = BAND_FREQ_MAP.get(b, '')
        return f"B{b}" + (f" ({freq} МГц)" if freq else "")
    parts = []
    for b in bands:
        freq = BAND_FREQ_MAP.get(b, '')
        parts.append(f"B{b}" + (f"/{freq}" if freq else ""))
    return "CA: " + " + ".join(parts)


def format_band_label(band_raw: Any, earfcn: Any = None,
                      known: Iterable[int] | None = None) -> str:
    """Человекочитаемая метка активного(-ых) LTE-band (см. active_bands)."""
    active = active_bands(band_raw, earfcn, known)
    if active:
        return format_band_list(active)
    # Ни EARFCN, ни распознанных бэндов — вернём сырьё как есть.
    if band_raw not in (None, '', '-'):
        return str(band_raw).strip()
    return "-"


def format_bytes_mb(b: Any) -> str:
    """Сырые байты → '123.4 МБ' для UI (единица — на языке интерфейса)."""
    try:
        return f"{int(b) / 1048576:.1f} {t('МБ')}"
    except (TypeError, ValueError):
        return "-"


def format_rate_mbps(bps: Any) -> str:
    """Bytes/sec → 'X.YZ Мбит/с' для UI (единица — на языке интерфейса)."""
    try:
        return f"{int(bps) * 8 / 1_000_000:.2f} {t('Мбит/с')}"
    except (TypeError, ValueError):
        return "-"


def first_present(data: Any, keys: Iterable[str]) -> Any:
    """Возвращает первое непустое значение по списку возможных ключей.

    Имена полей в ответе Huawei device/signal различаются между
    прошивками (dl_mcs / dlmcs / dlMcs и т.п.) — перебираем варианты.
    """
    if not isinstance(data, Mapping):
        return None
    for k in keys:
        v = data.get(k)
        if v not in (None, ''):
            return v
    return None


# Верхние границы MCS для QPSK / 16QAM / 64QAM (3GPP TS 36.213):
# DL — таблица 7.1.7.1-1, UL — таблица 8.6.1-1.
_MCS_BOUNDS = {False: (9, 16, 28), True: (10, 20, 28)}


def mcs_to_modulation(mcs: Any, uplink: bool = False) -> str | None:
    """MCS-индекс → модуляция по таблице 64QAM (DL или UL).

    Индексы 29–31 — повторные передачи: по голому номеру не понять, какая
    таблица действует, поэтому возвращается None. Если сеть использует
    таблицу 256QAM, точную модуляцию даёт только развёрнутая строка роутера
    ('27@256QAM'), которую разбирает format_modulation.
    """
    n = extract_number(mcs)
    if n is None:
        return None
    n = int(n)
    if n < 0:
        return None
    qpsk, qam16, qam64 = _MCS_BOUNDS[uplink]
    if n <= qpsk:
        return "QPSK"
    if n <= qam16:
        return "16QAM"
    if n <= qam64:
        return "64QAM"
    return None


def format_modulation(raw: Any, uplink: bool = False) -> str | None:
    """Модуляция → компактный вид.

    Роутер отдаёт либо MCS-индекс числом (5, 27), либо подробную строку
    вида 'mcsDownCarrier1Code0:27@256QAM mcsDownCarrier1Code1:27@256QAM'
    (несколько carrier/codeword). Приводим к короткому '256QAM (MCS 27)'
    или '256QAM (MCS 23/27)', если MCS разные. Строка без модуляции
    ('mcsDownCarrier1Code0:27 mcsDownCarrier1Code1:26') → 'MCS 26/27':
    таблица MCS (64QAM или 256QAM) из неё не видна, модуляция не
    угадывается. None — если не разобрать.

    Hardware validation required: формат dl_mcs / ul_mcs на B636 и B535.
    """
    if raw in (None, ''):
        return None
    s = str(raw)
    pairs = re.findall(r'(\d+)@(QPSK|\w*QAM)', s, re.IGNORECASE)
    if pairs:
        mcs = sorted({int(m) for m, _ in pairs})
        qam = list(dict.fromkeys(q.upper() for _, q in pairs))
        return f"{' + '.join(qam)} (MCS {'/'.join(str(m) for m in mcs)})"
    codes = sorted({int(c) for c in re.findall(r':\s*(\d+)', s)})
    if codes:
        return f"MCS {'/'.join(str(c) for c in codes)}"
    mod = mcs_to_modulation(raw, uplink)
    if mod is not None:
        return f"{mod} (MCS {int(extract_number(raw))})"
    return None


# TM (Transmission Mode, 3GPP TS 36.213) — схема передачи, а НЕ число
# антенн: TM3/TM4 работают и в 2x2, и в 4x4, TM9 — до 8 слоёв.
_TM_LABELS: dict[int, str] = {
    1: "SISO",
    2: "Tx diversity",
    3: "MIMO open-loop",
    4: "MIMO closed-loop",
    5: "MU-MIMO",
    6: "closed-loop rank 1",
    7: "beamforming 1-layer",
    8: "beamforming 2-layer",
    9: "MIMO up to 8 layers",
    10: "CoMP up to 8 layers",
}


def parse_tm(value: Any) -> int | None:
    """'TM[4]' / '4' / 'TM4' → 4."""
    if value in (None, ''):
        return None
    m = re.search(r'\d+', str(value))
    return int(m.group(0)) if m else None


def format_mimo(value: Any) -> str:
    """'TM[4]' / '4' → 'MIMO closed-loop [TM4]'. Неизвестное — как есть."""
    if value in (None, ''):
        return "-"
    tm = parse_tm(value)
    if tm is None:
        return str(value)
    label = _TM_LABELS.get(tm)
    return f"{label} [TM{tm}]" if label else f"TM{tm}"


def parse_pusch_power(txpower: Any) -> float | None:
    """Мощность PUSCH, dBm, из txpower ('PPusch:21dBm PPucch:9dBm' или '21dBm').

    Hardware validation required: формат поля txpower на разных прошивках.
    """
    if txpower in (None, ''):
        return None
    s = str(txpower)
    m = re.search(r'PPusch\s*:\s*(-?\d+(?:\.\d+)?)', s, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return extract_number(s)


def parse_hex_mask(value: Any) -> int | None:
    """Hex-строка маски без '0x' ('80045') → int. None — если не разобрать."""
    if value in (None, ''):
        return None
    s = str(value).strip()
    if s.lower().startswith('0x'):
        s = s[2:]
    try:
        v = int(s, 16)
    except ValueError:
        return None
    return v if v >= 0 else None


def mask_to_bands(mask: int) -> list[int]:
    """Битовая маска LTEBand → номера бэндов (бит N−1 = бэнд N)."""
    return [b for b in range(1, 64) if mask & (1 << (b - 1))]


def bands_to_mask(bands: Iterable[int]) -> int:
    """Номера бэндов → маска LTEBand."""
    mask = 0
    for b in bands:
        if not 1 <= int(b) <= 63:
            raise ValueError(f"LTE band {b} вне диапазона маски 1..63")
        mask |= 1 << (int(b) - 1)
    return mask


def bands_from_mask(mask: Any, supported_mask: int | None = None) -> list[int] | None:
    """Маска LTE-бэндов роутера → номера зафиксированных бэндов.

    Возвращает:
        * список номеров, отмеченных в маске;
        * [] — если маска означает AUTO (включены все бэнды);
        * None — если маску не удалось разобрать.

    AUTO определяется так:
        * маска покрывает всё, что поддерживает модем (supported_mask из
          net/net-mode-list), — если это известно;
        * иначе: маска покрывает все бэнды региона и заметно шире их
          (типовое '7FFFFFFFFFFFFFFF').
    """
    val = parse_hex_mask(mask)
    if val is None or val <= 0:
        return None
    if supported_mask:
        if (val & supported_mask) == supported_mask:
            return []
    else:
        region = bands_to_mask(REGION_BANDS)
        if (val & region) == region and val > region:
            return []
    if (val & LTE_ALL_MASK) == LTE_ALL_MASK:
        return []
    return mask_to_bands(val)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_supported_bands(net_mode_list: Any) -> list[int]:
    """Бэнды, которые поддерживает модем, из ответа net/net-mode-list.

    Формат LTEBandList различается между прошивками: элементы бывают по
    одному на бэнд или сгруппированы ('LTE BC1/LTE BC3/...' с общей
    маской). Надёжно — объединить биты Value всех элементов, кроме
    «все бэнды», и добавить номера из имён.
    Hardware validation required: формат LTEBandList на B636 и 5G-CPE.
    """
    if not isinstance(net_mode_list, Mapping):
        return []
    container = net_mode_list.get('LTEBandList')
    if not isinstance(container, Mapping):
        return []
    found: set[int] = set()
    for item in _as_list(container.get('LTEBand')):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get('Name') or '')
        if 'ALL' in name.upper():
            continue        # «LTE ALL» — маска «все бэнды», а не бэнды модема
        val = parse_hex_mask(item.get('Value'))
        if val is not None and 0 < val < LTE_ALL_MASK:
            found.update(mask_to_bands(val))
        found.update(int(n) for n in re.findall(r'BC\s*(\d+)', name, re.IGNORECASE)
                     if 1 <= int(n) <= 63)
    return sorted(found)


def parse_access_modes(net_mode_list: Any) -> list[str]:
    """Коды режимов сети, которые поддерживает модем (AccessList → Access)."""
    if not isinstance(net_mode_list, Mapping):
        return []
    container = net_mode_list.get('AccessList')
    if not isinstance(container, Mapping):
        return []
    return [str(a).strip() for a in _as_list(container.get('Access'))
            if str(a).strip()]


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
    if not isinstance(res, Mapping):
        n = extract_number(res)
        return int(n) if n is not None and 0 <= n <= 3 else None

    known = ('antennatype', 'antenna_type', 'antennaType', 'AntennaType',
             'curtype', 'type', 'Type', 'mode', 'Mode', 'antennamode')
    for k in known:
        if k in res:
            n = extract_number(res[k])
            if n is not None and 0 <= n <= 3:
                return int(n)
    for k, v in res.items():
        kl = str(k).lower()
        if 'antenna' in kl or 'type' in kl or 'mode' in kl:
            n = extract_number(v)
            if n is not None and 0 <= n <= 3:
                return int(n)
    return None
