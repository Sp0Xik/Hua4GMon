"""
Тесты чистых (без побочных эффектов) функций main.py.

Запуск:
    pytest tests/

Эти тесты:
* быстрые (миллисекунды) — гоняются на каждый push;
* защищают от регрессий при правках логики;
* НЕ требуют сетевого доступа или роутера;
* проверяют те же случаи, что вручную найденные в аудите.
"""
import pytest

import main


# =========================================================
# is_valid_ip
# =========================================================

@pytest.mark.parametrize("ip", [
    "192.168.8.1", "10.0.0.1", "255.255.255.255", "0.0.0.0",
    "127.0.0.1", "192.168.1.1",
])
def test_valid_ips(ip):
    assert main.is_valid_ip(ip)


@pytest.mark.parametrize("ip", [
    "256.0.0.1",       # октет > 255
    "",                # пусто
    "192.168.8",       # три октета
    "1.2.3.4.5",       # пять октетов
    "hello",           # не IP
    "192.168.8.1.",    # лишняя точка
    "192.168.8.-1",    # отрицательное
    "  192.168.8.1",   # пробелы
])
def test_invalid_ips(ip):
    assert not main.is_valid_ip(ip)


# =========================================================
# evaluate_signal
# =========================================================

@pytest.mark.parametrize("rsrp, expected_label", [
    (-70,  "Отличный"),
    (-80,  "Отличный"),    # ровно на пороге
    (-85,  "Хороший"),
    (-90,  "Хороший"),
    (-95,  "Средний"),
    (-100, "Средний"),
    (-115, "Плохой"),
])
def test_rsrp_evaluation(rsrp, expected_label):
    label, _, _ = main.evaluate_signal('rsrp', rsrp)
    assert label == expected_label


@pytest.mark.parametrize("sinr, expected_label", [
    (25,  "Идеальный"),
    (20,  "Идеальный"),
    (14,  "Хороший"),
    (5,   "Шумный"),
    (-3,  "Критичный"),
])
def test_sinr_evaluation(sinr, expected_label):
    label, _, _ = main.evaluate_signal('sinr', sinr)
    assert label == expected_label


def test_evaluate_none_value():
    assert main.evaluate_signal('rsrp', None) == ("Нет данных", "gray", 0)


def test_evaluate_unknown_param():
    label, _, _ = main.evaluate_signal('unknown_param', 5)
    assert label == "Н/Д"


# =========================================================
# calculate_overall_health
# =========================================================

def test_health_missing_data():
    assert main.calculate_overall_health(None, None) == \
           (0, "Нет данных", "gray")
    assert main.calculate_overall_health(-90, None) == \
           (0, "Нет данных", "gray")
    assert main.calculate_overall_health(None, 10) == \
           (0, "Нет данных", "gray")


def test_health_excellent():
    score, msg, _ = main.calculate_overall_health(-70, 25)
    assert score >= 85
    assert "Идеально" in msg


def test_health_poor():
    score, _, _ = main.calculate_overall_health(-115, -3)
    assert score < 35


def test_health_bounded():
    """Здоровье всегда 0..100, без выходов за границы."""
    for rsrp in (-50, -75, -100, -120):
        for sinr in (30, 15, 5, -10):
            score, _, _ = main.calculate_overall_health(rsrp, sinr)
            assert 0 <= score <= 100


# =========================================================
# extract_number
# =========================================================

def test_extract_numeric_types():
    assert main.extract_number(-85) == -85.0
    assert main.extract_number(-85.5) == -85.5
    assert main.extract_number(0) == 0.0


def test_extract_string_numerics():
    assert main.extract_number("-85") == -85.0
    assert main.extract_number("-85.5 dBm") == -85.5
    assert main.extract_number("12.3%") == 12.3
    assert main.extract_number("0") == 0.0


@pytest.mark.parametrize("garbage", [
    "timeout 0",   # ← ключевой кейс: в v1 это превращалось в 0!
    "timeout",
    "N/A", "NA", "None", "-", "",
    None,
    True, False,   # bool → не число
])
def test_extract_rejects_garbage(garbage):
    assert main.extract_number(garbage) is None


# =========================================================
# parse_cell_id
# =========================================================

def test_parse_cell_id_decimal():
    assert main.parse_cell_id(12345) == (12345 // 256, 12345 % 256)
    assert main.parse_cell_id("12345") == (12345 // 256, 12345 % 256)


def test_parse_cell_id_hex():
    assert main.parse_cell_id("0x12AB34") == \
           (0x12AB34 // 256, 0x12AB34 % 256)
    # Hex без 0x но с буквами
    assert main.parse_cell_id("ABCD12") == \
           (0xABCD12 // 256, 0xABCD12 % 256)


@pytest.mark.parametrize("bad", [
    None, "", "garbage", "FFFFFFFF",   # error sentinel
    "0", "-1",
])
def test_parse_cell_id_invalid(bad):
    assert main.parse_cell_id(bad) == (None, None)


# =========================================================
# parse_antenna_value
# =========================================================

def test_antenna_known_labels():
    assert main.parse_antenna_value("Авто") == 0
    assert main.parse_antenna_value("Внутренняя") == 1
    assert main.parse_antenna_value("Внешняя") == 2
    assert main.parse_antenna_value("Смешанная") == 3


def test_antenna_numeric_hint():
    """Legacy-метки вида 'Auto (0)' должны парситься."""
    assert main.parse_antenna_value("Auto (0)") == 0
    assert main.parse_antenna_value("Внешняя (2)") == 2


def test_antenna_unknown():
    assert main.parse_antenna_value("garbage") is None


# =========================================================
# format_band_label
# =========================================================

def test_band_single_label():
    assert main.format_band_label('7') == "B7 (2600 МГц)"
    assert main.format_band_label('LTE BAND 20') == "B20 (800DD МГц)"
    assert main.format_band_label('B3') == "B3 (1800+ МГц)"


def test_band_carrier_aggregation():
    assert main.format_band_label('7+20') == "CA: B7/2600 + B20/800DD"
    assert main.format_band_label('B3+B7') == "CA: B3/1800+ + B7/2600"


def test_band_hex_bitmask():
    assert main.format_band_label('0x40') == "B7 (2600 МГц)"
    # 0x80044 = 0x4 (B3) + 0x40 (B7) + 0x80000 (B20)
    assert main.format_band_label('0x80044') == \
           "CA: B3/1800+ + B7/2600 + B20/800DD"


def test_band_fallback_by_earfcn():
    """Если band пустой, но есть EARFCN — определяем band по нему."""
    assert main.format_band_label(None, 6300).startswith("≈ B20")
    assert main.format_band_label('', 1300).startswith("≈ B3")
    assert main.format_band_label('-', 3000).startswith("≈ B7")


@pytest.mark.parametrize("missing", [None, '', '-'])
def test_band_no_data(missing):
    assert main.format_band_label(missing) == '-'


# =========================================================
# earfcn_to_band
# =========================================================

@pytest.mark.parametrize("earfcn, band", [
    (1300, 3), (3000, 7), (6300, 20), (40000, 41), (66800, 66),
])
def test_earfcn_known(earfcn, band):
    assert main.earfcn_to_band(earfcn) == band


def test_earfcn_unknown():
    assert main.earfcn_to_band(99999) is None
    assert main.earfcn_to_band('garbage') is None
    assert main.earfcn_to_band(None) is None


# =========================================================
# analyze_whitelist_results
# =========================================================

def test_filter_off_all_works():
    title, _, color = main.analyze_whitelist_results(
        [('a', True), ('b', True), ('c', True)],
        [('d', True), ('e', True), ('f', True)])
    assert title == "Белые списки ВЫКЛЮЧЕНЫ"
    assert color == "#00b894"


def test_filter_on_only_whitelist():
    title, _, color = main.analyze_whitelist_results(
        [('a', True), ('b', True), ('c', True)],
        [('d', False), ('e', False), ('f', False)])
    assert "ВКЛЮЧЕНЫ" in title
    assert color == "#d63031"


def test_no_internet():
    title, _, _ = main.analyze_whitelist_results(
        [('a', False)] * 3, [('d', False)] * 3)
    assert title == "Нет интернета"


def test_anomaly_neutral_works_but_whitelist_doesnt():
    """Странный кейс — обычно означает VPN или другой канал."""
    title, _, _ = main.analyze_whitelist_results(
        [('a', False)] * 3, [('d', True)] * 3)
    assert title == "Аномалия"


def test_partial_whitelist_still_counts():
    """Если хоть один белый сайт ответил — белые списки 'не пустые'."""
    title, _, _ = main.analyze_whitelist_results(
        [('a', False), ('b', True), ('c', False)],   # 1 из 3
        [('d', False), ('e', False), ('f', False)])
    assert "ВКЛЮЧЕНЫ" in title


# =========================================================
# Форматтеры
# =========================================================

def test_format_bytes_mb():
    assert main.format_bytes_mb(1048576) == "1.0 МБ"
    assert main.format_bytes_mb(0) == "0.0 МБ"
    assert main.format_bytes_mb("garbage") == "-"
    assert main.format_bytes_mb(None) == "-"


def test_format_rate_mbps():
    # 125000 bytes/s = 1 Mbps
    assert main.format_rate_mbps(125000) == "1.00 Мбит/с"
    assert main.format_rate_mbps(0) == "0.00 Мбит/с"
    assert main.format_rate_mbps(None) == "-"


# =========================================================
# tcp_reachable (быстрый «отказ» на гарантированно недоступном порту)
# =========================================================

def test_tcp_unreachable_returns_false():
    """Тест не лезет в реальную сеть — порт 1 на localhost закрыт."""
    ok, reason = main.tcp_reachable('localhost', 1, timeout=0.5)
    assert ok is False
    assert reason   # непустая причина
