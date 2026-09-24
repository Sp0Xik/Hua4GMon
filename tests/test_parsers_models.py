"""Тесты разбора сырых значений и сборки Snapshot."""
import pytest

import core
from core.constants import EARFCN_RANGES, LTE_BAND_TABLE

# ---------- extract_number: граничные значения прошивок ----------


@pytest.mark.parametrize("raw, value", [
    (">=-51dBm", -51.0), ("<-140dBm", -140.0), ("<=-44dBm", -44.0),
    ("-95dBm", -95.0), ("-10.5 dB", -10.5), ("14", 14.0),
])
def test_extract_number_boundaries(raw, value):
    assert core.extract_number(raw) == value


@pytest.mark.parametrize("raw", ["--", "timeout 0", ">>5", "N/A"])
def test_extract_number_rejects(raw):
    assert core.extract_number(raw) is None


# ---------- MCS / модуляция ----------

@pytest.mark.parametrize("mcs, mod", [
    (0, "QPSK"), (9, "QPSK"), (10, "16QAM"), (16, "16QAM"),
    (17, "64QAM"), (28, "64QAM"),
])
def test_mcs_table(mcs, mod):
    assert core.mcs_to_modulation(mcs) == mod


@pytest.mark.parametrize("mcs", [29, 30, 31, -1, 99, "x"])
def test_mcs_reserved_and_invalid(mcs):
    """29–31 — повторные передачи, модуляцию не определяют."""
    assert core.mcs_to_modulation(mcs) is None


def test_modulation_reserved_index_not_shown_as_256qam():
    assert core.format_modulation(30) is None


# ---------- бэнды ----------

def test_band_label_ignores_bandwidth_numbers():
    """'20MHz' не должен превращаться в ложную CA с B20."""
    assert core.format_band_label("LTE BAND 3 (20MHz)") == "B3 (1800+ МГц)"


def test_active_bands_with_known_supported():
    """Бэнды модема из net-mode-list расширяют фильтр правдоподобия."""
    assert core.active_bands("B28+B3", None) == [3]
    assert core.active_bands("B28+B3", None, known=[3, 28]) == [28, 3]


def test_dl_earfcn_formats():
    assert core.dl_earfcn("DL:1725 UL:19725") == 1725
    assert core.dl_earfcn(300) == 300
    assert core.dl_earfcn("") is None
    assert core.dl_earfcn("-") is None


@pytest.mark.parametrize("earfcn, mhz", [
    (1300, 1815.0), (6300, 806.0), ("DL:200 UL:18200", 2130.0),
    (38000, 2595.0), (3000, 2645.0), (3500, 930.0),
])
def test_earfcn_to_freq(earfcn, mhz):
    assert core.earfcn_to_freq_mhz(earfcn) == mhz


def test_earfcn_to_freq_unknown():
    assert core.earfcn_to_freq_mhz(99999) is None
    assert core.earfcn_to_freq_mhz(None) is None


def test_band_table_consistent():
    """Нижняя граница каждого диапазона → свой бэнд и частота F_DL_low."""
    for band, (_n, _d, f_low, offs, n_max) in LTE_BAND_TABLE.items():
        assert offs <= n_max
        assert core.earfcn_to_band(offs) == band
        assert core.earfcn_to_band(n_max) == band
        assert core.earfcn_to_freq_mhz(offs) == round(f_low, 1)
    lows = [lo for lo, _, _ in EARFCN_RANGES]
    assert lows == sorted(lows)
    prev_hi = -1
    for lo, hi, _band in EARFCN_RANGES:
        assert lo > prev_hi, "диапазоны EARFCN пересекаются"
        prev_hi = hi


@pytest.mark.parametrize("band, label", [
    (3, "B3 (1800 МГц)"), (38, "B38 (TDD 2600)"), (20, "B20 (800 МГц)"),
    (32, "B32 (SDL 1500 L)"), (4, "B4 (AWS-1)"), (99, "B99"),
])
def test_lte_band_label(band, label):
    assert core.lte_band_label(band) == label


def test_lte_band_label_units_translatable():
    assert core.lte_band_label(3, mhz="MHz") == "B3 (1800 MHz)"


def test_mask_roundtrip():
    bands = [1, 3, 7, 20, 38, 41]
    assert core.mask_to_bands(core.bands_to_mask(bands)) == bands
    assert core.bands_to_mask([1, 3, 20]) == 0x80005   # пример из docstring библиотеки
    with pytest.raises(ValueError):
        core.bands_to_mask([64])


def test_bands_from_mask_returns_numbers():
    assert core.bands_from_mask("44") == [3, 7]
    assert core.bands_from_mask("7FFFFFFFFFFFFFFF") == []      # AUTO
    assert core.bands_from_mask("") is None
    assert core.bands_from_mask("zz") is None
    assert core.bands_from_mask("0") is None


def test_bands_from_mask_with_supported_mask():
    """Если прошивка хранит AUTO как маску поддерживаемых — это AUTO."""
    supported = core.bands_to_mask([1, 3, 7, 8, 20, 38])
    assert core.bands_from_mask("20000800C5", supported) == []
    assert core.bands_from_mask("4", supported) == [3]
    # Без знания поддерживаемых — честно показываем биты маски.
    assert core.bands_from_mask("20000800C5") == [1, 3, 7, 8, 20, 38]


def test_parse_supported_bands_grouped_and_single():
    nml = {'LTEBandList': {'LTEBand': [
        {'Name': 'LTE BC1/LTE BC3/LTE BC7', 'Value': '45'},
        {'Name': 'LTE BC20', 'Value': '80000'},
        {'Name': 'LTE ALL', 'Value': '7FFFFFFFFFFFFFFF'}]}}
    assert core.parse_supported_bands(nml) == [1, 3, 7, 20]


def test_parse_supported_bands_single_item_dict():
    nml = {'LTEBandList': {'LTEBand': {'Name': 'LTE BC38', 'Value': '2000000000'}}}
    assert core.parse_supported_bands(nml) == [38]


@pytest.mark.parametrize("bad", [None, {}, {'LTEBandList': None}, "x",
                                 {'LTEBandList': {'LTEBand': ['junk']}}])
def test_parse_supported_bands_garbage(bad):
    assert core.parse_supported_bands(bad) == []


def test_parse_access_modes():
    assert core.parse_access_modes({'AccessList': {'Access': ['00', '03']}}) == ['00', '03']
    assert core.parse_access_modes({'AccessList': {'Access': '03'}}) == ['03']
    assert core.parse_access_modes({}) == []


@pytest.mark.parametrize("raw, dbm", [
    ("PPusch:21dBm PPucch:9dBm", 21.0), ("PPusch: -3dBm", -3.0),
    ("12dBm", 12.0), (None, None), ("", None),
])
def test_parse_pusch_power(raw, dbm):
    assert core.parse_pusch_power(raw) == dbm


def test_parse_nci():
    assert core.parse_nci(str(0x123456789)) == 0x123456789
    assert core.parse_nci("0") is None
    assert core.parse_nci(str(1 << 36)) is None
    assert core.parse_nci("junk") is None


# ---------- Snapshot ----------

BASE_RAW = {
    'rsrp': '-88dBm', 'rsrq': '-9.0dB', 'rssi': '-61dBm', 'sinr': '14dB',
    'pci': '287', 'cell_id': str(180123 * 256 + 12),
    'earfcn': 'DL:1300 UL:19300', 'band': 'B3+B1',
    'Numeric': '25002', 'FullName': 'MegaFon', 'cqi0': '11', 'cqi1': '10',
    'transmode': 'TM[3]', 'txpower': 'PPusch:21dBm PPucch:9dBm',
    'CurrentDownloadRate': '125000', 'dataswitch': '1',
}


def test_snapshot_basic_fields():
    snap = core.build_snapshot(BASE_RAW)
    assert (snap.rsrp, snap.sinr, snap.rsrq, snap.rssi) == (-88.0, 14.0, -9.0, -61.0)
    assert snap.cell == core.CellKey(1300, 287)
    assert (snap.enodeb, snap.sector) == (180123, 12)
    assert snap.freq_mhz == 1815.0
    assert snap.operator == "MegaFon"
    assert snap.tm == 3 and snap.pusch_dbm == 21.0
    assert snap.dl_rate == 125000
    assert snap.data_enabled is True


def test_snapshot_ca_from_band_field_without_status():
    snap = core.build_snapshot(BASE_RAW)
    assert snap.bands == (3, 1)
    assert snap.ca is True
    assert snap.band_label == "CA: B3/1800+ + B1/2100"
    assert snap.bands_compact == "B3+B1"


def test_snapshot_status_101_overrides_false_ca():
    """Роутер сообщает LTE без CA — лишние номера из band не выдаём за CA."""
    snap = core.build_snapshot({**BASE_RAW, 'CurrentNetworkTypeEx': '101'})
    assert snap.ca is False
    assert snap.bands == (3,)
    assert snap.rat == 'lte'


def test_snapshot_status_1011_confirms_ca():
    snap = core.build_snapshot({**BASE_RAW, 'band': '3', 'CurrentNetworkTypeEx': '1011'})
    assert snap.ca is True
    assert snap.rat == 'lte_ca'


def test_snapshot_nr_nsa_and_sa():
    nsa = core.build_snapshot({**BASE_RAW, 'nrrsrp': '-95dBm', 'nrsinr': '9dB'})
    assert nsa.rat == 'nr_nsa' and nsa.has_nr
    assert nsa.metric('nr_sinr') == 9.0
    assert nsa.nr_summary() == "RSRP -95 / SINR 9"
    full = core.build_snapshot({**BASE_RAW, 'nrrsrp': '-95', 'nrsinr': '9', 'nrband': '78',
                                'nrdlbandwidth': '100MHz', 'nrearfcn': '627264',
                                'nrpci': '501'})
    assert full.nr_band_label == "n78"
    assert full.nr_summary() == "n78 · 100MHz · RSRP -95 / SINR 9 · ARFCN 627264 · PCI 501"
    assert core.build_snapshot({'nrrsrp': '-90', 'nr_band': 'N41'}).nr_band_label == "N41"
    sa = core.build_snapshot({'nrrsrp': '-95dBm', 'nrsinr': '9dB',
                              'cell_id': str(0x123456789)})
    assert sa.rat == 'nr_sa'
    assert sa.nci == 0x123456789 and sa.enodeb is None


def test_snapshot_operator_fallback_and_enodeb_field():
    snap = core.build_snapshot({'rsrp': '-90', 'Numeric': '25001', 'enodeb_id': '555'})
    assert snap.operator == "МТС"
    assert snap.enodeb == 555


def test_snapshot_data_switch_off():
    assert core.build_snapshot({'dataswitch': '0'}).data_enabled is False
    assert core.build_snapshot({}).data_enabled is None


def test_snapshot_empty_is_safe():
    snap = core.build_snapshot({})
    assert snap.rsrp is None and snap.bands == () and snap.band_label == '-'
    assert snap.rat == '' and snap.ca is None
    assert snap.metric('unknown') is None
    assert not snap.cell.known
