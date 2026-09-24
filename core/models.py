"""
Типизированный снимок состояния модема.

Сырые ответы Huawei API (device/signal, net/current-plmn,
monitoring/status, traffic-statistics, month_statistics,
dialup/mobile-dataswitch) сливаются в один словарь, из которого
build_snapshot() строит неизменяемый Snapshot. Имена полей у прошивок
различаются — все варианты перебираются здесь, в одном месте, а UI
работает только с атрибутами Snapshot.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.constants import PLMN_MAP
from core.parsers import (
    active_bands,
    dl_earfcn,
    earfcn_to_freq_mhz,
    extract_number,
    first_present,
    format_band_list,
    parse_cell_id,
    parse_nci,
    parse_pusch_power,
    parse_tm,
    to_int,
)
from core.rf import classify_rat, detect_ca

METRICS: tuple[str, ...] = ('rsrp', 'rssi', 'sinr', 'rsrq')
NR_METRICS: tuple[str, ...] = ('nr_rsrp', 'nr_sinr', 'nr_rsrq')

# Варианты имён полей NR на прошивках 5G-CPE.
# Hardware validation required: сверить с реальным 5G-CPE.
_NR_KEYS: dict[str, tuple[str, ...]] = {
    'nr_rsrp': ('nrrsrp', 'nr_rsrp', 'NrRsrp'),
    'nr_sinr': ('nrsinr', 'nr_sinr', 'NrSinr'),
    'nr_rsrq': ('nrrsrq', 'nr_rsrq', 'NrRsrq'),
    'nr_arfcn': ('nrearfcn', 'nrarfcn', 'nr_arfcn'),
    'nr_pci': ('nrpci', 'nr_pci'),
    'nr_band': ('nrband', 'nr_band'),
    'nr_bandwidth': ('nrdlbandwidth', 'nrbandwidth'),
    'nr_cell_id': ('nrcellid', 'nr_cell_id', 'nci'),
}


@dataclass(frozen=True, slots=True)
class CellKey:
    """Идентичность обслуживающей соты: DL EARFCN + PCI."""
    earfcn: int | None
    pci: int | None

    @property
    def known(self) -> bool:
        return self.earfcn is not None and self.pci is not None


@dataclass(frozen=True, slots=True)
class Snapshot:
    rsrp: float | None = None
    rssi: float | None = None
    sinr: float | None = None
    rsrq: float | None = None
    nr_rsrp: float | None = None
    nr_sinr: float | None = None
    nr_rsrq: float | None = None
    nr_arfcn: int | None = None
    nr_pci: int | None = None
    nr_band: str = ''
    nr_bandwidth: str = ''
    nci: int | None = None
    earfcn_raw: str = ''
    earfcn: int | None = None
    freq_mhz: float | None = None
    pci: int | None = None
    enodeb: int | None = None
    sector: int | None = None
    tac: str = ''
    band_raw: str = ''
    bands: tuple[int, ...] = ()
    band_label: str = '-'
    plmn: str = ''
    operator: str = ''
    network_type_ex: int | None = None
    rat: str = ''
    ca: bool | None = None
    dl_bandwidth: str = ''
    ul_bandwidth: str = ''
    dl_mcs: str = ''
    ul_mcs: str = ''
    cqi0: int | None = None
    cqi1: int | None = None
    tm: int | None = None
    transmode: str = ''
    txpower: str = ''
    pusch_dbm: float | None = None
    rrc: str = ''
    temperature: str = ''
    dl_rate: int | None = None
    ul_rate: int | None = None
    total_dl: int | None = None
    total_ul: int | None = None
    connect_time: int | None = None
    month_dl: int | None = None
    month_ul: int | None = None
    data_enabled: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def cell(self) -> CellKey:
        return CellKey(self.earfcn, self.pci)

    @property
    def has_nr(self) -> bool:
        return self.nr_rsrp is not None or self.nr_sinr is not None

    def metric(self, name: str) -> float | None:
        """Значение метрики по имени ('rsrp', 'nr_sinr', ...) или None."""
        if name in METRICS or name in NR_METRICS:
            return getattr(self, name)
        return None

    @property
    def nr_band_label(self) -> str:
        """'n78' — NR-бэнд в принятой записи ('' — если роутер его не отдал)."""
        band = self.nr_band.strip()
        return f"n{band}" if band.isdigit() else band

    def nr_summary(self) -> str:
        """'n78 · 100MHz · RSRP -95 / SINR 9 · ARFCN 627264 · PCI 501'."""
        def num(v: float | int | None) -> str:
            return "-" if v is None else f"{v:g}"
        parts = [p for p in (self.nr_band_label, self.nr_bandwidth) if p]
        parts.append(f"RSRP {num(self.nr_rsrp)} / SINR {num(self.nr_sinr)}")
        if self.nr_arfcn:
            parts.append(f"ARFCN {self.nr_arfcn}")
        if self.nr_pci is not None:
            parts.append(f"PCI {self.nr_pci}")
        return " · ".join(parts)

    @property
    def bands_compact(self) -> str:
        """'B3+B1' — компактно, без кириллицы (для CSV и логов)."""
        return "+".join(f"B{b}" for b in self.bands)


def _str(data: Mapping[str, Any], keys: Iterable[str]) -> str:
    v = first_present(data, keys)
    return str(v).strip() if v is not None else ''


def _flag(value: Any) -> bool | None:
    n = to_int(value)
    return None if n is None else n != 0


def operator_name(plmn: str, full_name: str = '') -> str:
    """Имя оператора: из ответа роутера, иначе по справочнику PLMN."""
    if full_name:
        return full_name
    return PLMN_MAP.get(plmn, '')


def build_snapshot(raw: Mapping[str, Any],
                   known_bands: Iterable[int] | None = None) -> Snapshot:
    """Сырые данные роутера → Snapshot.

    known_bands — бэнды, поддерживаемые модемом (из net/net-mode-list);
    используются как фильтр правдоподобия для поля band.
    """
    data = dict(raw)
    metrics = {k: extract_number(data.get(k)) for k in METRICS}
    nr = {k: extract_number(first_present(data, _NR_KEYS[k])) for k in NR_METRICS}
    has_lte = any(metrics[k] is not None for k in ('rsrp', 'sinr'))
    has_nr = nr['nr_rsrp'] is not None or nr['nr_sinr'] is not None

    earfcn_raw = _str(data, ('earfcn', 'Earfcn'))
    earfcn = dl_earfcn(earfcn_raw)

    enodeb, sector = parse_cell_id(data.get('cell_id'))
    if enodeb is None:
        enodeb = to_int(first_present(data, ('enodeb_id', 'eNodeB')))
    nci = parse_nci(first_present(data, _NR_KEYS['nr_cell_id']))
    if nci is None and enodeb is None:
        # Поле cell_id шире 28 бит — это NCI 5G SA.
        nci = parse_nci(data.get('cell_id'))

    band_raw = _str(data, ('band',))
    net_ex = to_int(data.get('CurrentNetworkTypeEx'))
    bands = active_bands(band_raw, earfcn_raw, known_bands) if has_lte else []
    ca = detect_ca(net_ex, len(bands))
    if ca is False:
        # Авторитетный источник говорит «без CA» — лишние номера из поля
        # band не выдаём за агрегацию.
        bands = bands[:1]

    plmn = _str(data, ('Numeric', 'plmn'))
    full_name = _str(data, ('FullName', 'ShortName'))

    return Snapshot(
        rsrp=metrics['rsrp'], rssi=metrics['rssi'],
        sinr=metrics['sinr'], rsrq=metrics['rsrq'],
        nr_rsrp=nr['nr_rsrp'], nr_sinr=nr['nr_sinr'], nr_rsrq=nr['nr_rsrq'],
        nr_arfcn=to_int(first_present(data, _NR_KEYS['nr_arfcn'])),
        nr_pci=to_int(first_present(data, _NR_KEYS['nr_pci'])),
        nr_band=_str(data, _NR_KEYS['nr_band']),
        nr_bandwidth=_str(data, _NR_KEYS['nr_bandwidth']),
        nci=nci,
        earfcn_raw=earfcn_raw,
        earfcn=earfcn,
        freq_mhz=earfcn_to_freq_mhz(earfcn),
        pci=to_int(data.get('pci')),
        enodeb=enodeb,
        sector=sector,
        tac=_str(data, ('tac', 'TAC')),
        band_raw=band_raw,
        bands=tuple(bands),
        band_label=format_band_list(bands) if bands else (band_raw or '-'),
        plmn=plmn,
        operator=operator_name(plmn, full_name),
        network_type_ex=net_ex,
        rat=classify_rat(net_ex, has_lte, has_nr),
        ca=ca,
        dl_bandwidth=_str(data, ('dlbandwidth',)),
        ul_bandwidth=_str(data, ('ulbandwidth',)),
        dl_mcs=_str(data, ('dl_mcs', 'dlmcs', 'dlMcs')),
        ul_mcs=_str(data, ('ul_mcs', 'ulmcs', 'ulMcs')),
        cqi0=to_int(data.get('cqi0')),
        cqi1=to_int(data.get('cqi1')),
        tm=parse_tm(first_present(data, ('transmode', 'TransMode', 'mimo'))),
        transmode=_str(data, ('transmode', 'TransMode', 'mimo')),
        txpower=_str(data, ('txpower', 'TxPower', 'tx_power')),
        pusch_dbm=parse_pusch_power(first_present(data, ('txpower', 'TxPower', 'tx_power'))),
        rrc=_str(data, ('rrc_status',)),
        temperature=_str(data, ('Temperature',)),
        dl_rate=to_int(data.get('CurrentDownloadRate')),
        ul_rate=to_int(data.get('CurrentUploadRate')),
        total_dl=to_int(data.get('TotalDownload')),
        total_ul=to_int(data.get('TotalUpload')),
        connect_time=to_int(first_present(data, ('CurrentConnectTime', 'ConnectionTime'))),
        month_dl=to_int(data.get('CurrentMonthDownload')),
        month_ul=to_int(data.get('CurrentMonthUpload')),
        data_enabled=_flag(data.get('dataswitch')),
        raw=data,
    )
