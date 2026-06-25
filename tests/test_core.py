# ruff: noqa: I001
"""
Тесты чистой логики пакета core/.

Раньше эти тесты импортировали из main.py. Теперь — из core/, что:
  * не требует Tkinter и не падает в headless-CI;
  * не зависит от huawei_lte_api;
  * можно запустить из любой среды (python-for-android тоже).

Запуск:
    pytest tests/

Эти тесты:
  * быстрые (миллисекунды) — гоняются на каждый push;
  * защищают от регрессий при правках логики;
  * НЕ требуют сетевого доступа (кроме одного теста на localhost:1).
"""
import pytest

import core


# =========================================================
# is_valid_ip
# =========================================================

@pytest.mark.parametrize("ip", [
    "192.168.8.1", "10.0.0.1", "255.255.255.255", "0.0.0.0",
    "127.0.0.1", "192.168.1.1",
])
def test_valid_ips(ip):
    assert core.is_valid_ip(ip)


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
    assert not core.is_valid_ip(ip)


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
    label, _, _ = core.evaluate_signal('rsrp', rsrp)
    assert label == expected_label


@pytest.mark.parametrize("sinr, expected_label", [
    (25,  "Идеальный"),
    (20,  "Идеальный"),
    (14,  "Хороший"),
    (5,   "Шумный"),
    (-3,  "Критичный"),
])
def test_sinr_evaluation(sinr, expected_label):
    label, _, _ = core.evaluate_signal('sinr', sinr)
    assert label == expected_label


def test_evaluate_none_value():
    assert core.evaluate_signal('rsrp', None) == ("Нет данных", "gray", 0)


def test_evaluate_unknown_param():
    label, _, _ = core.evaluate_signal('unknown_param', 5)
    assert label == "Н/Д"


# =========================================================
# calculate_overall_health
# =========================================================

def test_health_missing_data():
    assert core.calculate_overall_health(None, None) == \
           (0, "Нет данных", "gray")
    assert core.calculate_overall_health(-90, None) == \
           (0, "Нет данных", "gray")
    assert core.calculate_overall_health(None, 10) == \
           (0, "Нет данных", "gray")


def test_health_excellent():
    score, msg, _ = core.calculate_overall_health(-70, 25)
    assert score >= 85
    assert "Идеально" in msg


def test_health_poor():
    score, _, _ = core.calculate_overall_health(-115, -3)
    assert score < 35


def test_health_bounded():
    """Здоровье всегда 0..100, без выходов за границы."""
    for rsrp in (-50, -75, -100, -120):
        for sinr in (30, 15, 5, -10):
            score, _, _ = core.calculate_overall_health(rsrp, sinr)
            assert 0 <= score <= 100


# =========================================================
# extract_number
# =========================================================

def test_extract_numeric_types():
    assert core.extract_number(-85) == -85.0
    assert core.extract_number(-85.5) == -85.5
    assert core.extract_number(0) == 0.0


def test_extract_string_numerics():
    assert core.extract_number("-85") == -85.0
    assert core.extract_number("-85.5 dBm") == -85.5
    assert core.extract_number("12.3%") == 12.3
    assert core.extract_number("0") == 0.0


@pytest.mark.parametrize("garbage", [
    "timeout 0",   # ← ключевой кейс: в v1 это превращалось в 0!
    "timeout",
    "N/A", "NA", "None", "-", "",
    None,
    True, False,   # bool → не число
])
def test_extract_rejects_garbage(garbage):
    assert core.extract_number(garbage) is None


# =========================================================
# parse_cell_id
# =========================================================

def test_parse_cell_id_decimal():
    assert core.parse_cell_id(12345) == (12345 // 256, 12345 % 256)
    assert core.parse_cell_id("12345") == (12345 // 256, 12345 % 256)


def test_parse_cell_id_hex():
    assert core.parse_cell_id("0x12AB34") == \
           (0x12AB34 // 256, 0x12AB34 % 256)
    # Hex без 0x но с буквами
    assert core.parse_cell_id("ABCD12") == \
           (0xABCD12 // 256, 0xABCD12 % 256)


@pytest.mark.parametrize("bad", [
    None, "", "garbage", "FFFFFFFF",   # error sentinel
    "0", "-1",
])
def test_parse_cell_id_invalid(bad):
    assert core.parse_cell_id(bad) == (None, None)


# =========================================================
# parse_antenna_value
# =========================================================

def test_antenna_known_labels():
    assert core.parse_antenna_value("Авто") == 0
    assert core.parse_antenna_value("Внутренняя") == 1
    assert core.parse_antenna_value("Внешняя") == 2
    assert core.parse_antenna_value("Смешанная") == 3


def test_antenna_numeric_hint():
    """Legacy-метки вида 'Auto (0)' должны парситься."""
    assert core.parse_antenna_value("Auto (0)") == 0
    assert core.parse_antenna_value("Внешняя (2)") == 2


def test_antenna_unknown():
    assert core.parse_antenna_value("garbage") is None


# =========================================================
# format_band_label
# =========================================================

def test_band_single_label():
    assert core.format_band_label('7') == "B7 (2600 МГц)"
    assert core.format_band_label('LTE BAND 20') == "B20 (800DD МГц)"
    assert core.format_band_label('B3') == "B3 (1800+ МГц)"


def test_band_carrier_aggregation():
    assert core.format_band_label('7+20') == "CA: B7/2600 + B20/800DD"
    assert core.format_band_label('B3+B7') == "CA: B3/1800+ + B7/2600"


def test_band_hex_bitmask():
    assert core.format_band_label('0x40') == "B7 (2600 МГц)"
    # 0x80044 = 0x4 (B3) + 0x40 (B7) + 0x80000 (B20)
    assert core.format_band_label('0x80044') == \
           "CA: B3/1800+ + B7/2600 + B20/800DD"


def test_band_fallback_by_earfcn():
    """Если band пустой, но есть EARFCN — определяем band по нему."""
    assert core.format_band_label(None, 6300).startswith("≈ B20")
    assert core.format_band_label('', 1300).startswith("≈ B3")
    assert core.format_band_label('-', 3000).startswith("≈ B7")


@pytest.mark.parametrize("missing", [None, '', '-'])
def test_band_no_data(missing):
    assert core.format_band_label(missing) == '-'


# =========================================================
# earfcn_to_band
# =========================================================

@pytest.mark.parametrize("earfcn, band", [
    (1300, 3), (3000, 7), (6300, 20), (40000, 41), (66800, 66),
])
def test_earfcn_known(earfcn, band):
    assert core.earfcn_to_band(earfcn) == band


def test_earfcn_unknown():
    assert core.earfcn_to_band(99999) is None
    assert core.earfcn_to_band('garbage') is None
    assert core.earfcn_to_band(None) is None


# =========================================================
# analyze_whitelist_results
# =========================================================

def test_filter_off_all_works():
    title, _, color = core.analyze_whitelist_results(
        [('a', True), ('b', True), ('c', True)],
        [('d', True), ('e', True), ('f', True)])
    assert title == "Белые списки ВЫКЛЮЧЕНЫ"
    assert color == "#00b894"


def test_filter_on_only_whitelist():
    title, _, color = core.analyze_whitelist_results(
        [('a', True), ('b', True), ('c', True)],
        [('d', False), ('e', False), ('f', False)])
    assert "ВКЛЮЧЕНЫ" in title
    assert color == "#d63031"


def test_no_internet():
    title, _, _ = core.analyze_whitelist_results(
        [('a', False)] * 3, [('d', False)] * 3)
    assert title == "Нет интернета"


def test_anomaly_neutral_works_but_whitelist_doesnt():
    """Странный кейс — обычно означает VPN или другой канал."""
    title, _, _ = core.analyze_whitelist_results(
        [('a', False)] * 3, [('d', True)] * 3)
    assert title == "Аномалия"


def test_partial_whitelist_still_counts():
    """Если хоть один белый сайт ответил — белые списки 'не пустые'."""
    title, _, _ = core.analyze_whitelist_results(
        [('a', False), ('b', True), ('c', False)],   # 1 из 3
        [('d', False), ('e', False), ('f', False)])
    assert "ВКЛЮЧЕНЫ" in title


# =========================================================
# Форматтеры
# =========================================================

def test_format_bytes_mb():
    assert core.format_bytes_mb(1048576) == "1.0 МБ"
    assert core.format_bytes_mb(0) == "0.0 МБ"
    assert core.format_bytes_mb("garbage") == "-"
    assert core.format_bytes_mb(None) == "-"


def test_format_rate_mbps():
    # 125000 bytes/s = 1 Mbps
    assert core.format_rate_mbps(125000) == "1.00 Мбит/с"
    assert core.format_rate_mbps(0) == "0.00 Мбит/с"
    assert core.format_rate_mbps(None) == "-"


# =========================================================
# tcp_reachable (быстрый «отказ» на гарантированно недоступном порту)
# =========================================================

def test_tcp_unreachable_returns_false():
    """Тест не лезет в реальную сеть — порт 1 на localhost закрыт."""
    ok, reason = core.tcp_reachable('localhost', 1, timeout=0.5)
    assert ok is False
    assert reason   # непустая причина


# =========================================================
# Smoke-тесты: пакет core импортируется чисто
# =========================================================

def test_core_package_exports_match_all():
    """Все имена из core.__all__ реально доступны."""
    for name in core.__all__:
        assert hasattr(core, name), f"core.__all__ упоминает {name}, " \
                                     f"но в модуле его нет"


def test_core_does_not_pull_in_tkinter():
    """core не должен тащить за собой tkinter — это сломало бы Android."""
    import sys
    # Если бы core импортировал tkinter, он уже был бы в sys.modules.
    # Проверяем именно что core не зависит от tk: tk может быть в sys.modules
    # из других тестов или conftest — это нормально. Проверяем сами модули:
    import inspect
    for mod_name in ('core.constants', 'core.parsers',
                     'core.signal_analysis', 'core.whitelist', 'core.i18n'):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        src = inspect.getsource(mod)
        assert 'import tkinter' not in src, \
            f"{mod_name} тянет tkinter — Android-сборка сломается!"
        assert 'from tkinter' not in src, \
            f"{mod_name} тянет from tkinter — Android-сборка сломается!"


# =========================================================
# i18n — локализация
# =========================================================

def test_i18n_default_is_russian():
    core.set_language("ru")
    assert core.current_language() == "ru"
    # Русский: ключ возвращается как есть
    assert core.t("Подключиться") == "Подключиться"


def test_i18n_switch_to_english():
    core.set_language("en")
    assert core.current_language() == "en"
    assert core.t("Отключено") == "Disconnected"
    assert core.t("🚀 Подключиться") == "🚀 Connect"
    core.set_language("ru")   # вернуть, чтобы не влиять на другие тесты


def test_i18n_unknown_key_falls_back_to_key():
    """Если перевода нет — возвращается сам ключ, ничего не падает."""
    core.set_language("en")
    assert core.t("Совершенно непереведённая строка 12345") == \
        "Совершенно непереведённая строка 12345"
    core.set_language("ru")


def test_i18n_unknown_language_ignored():
    core.set_language("ru")
    core.set_language("klingon")   # не меняет язык
    assert core.current_language() == "ru"


def test_i18n_health_template_translatable():
    """health-шаблон должен переводиться и форматироваться числом."""
    core.set_language("en")
    score, tmpl, _ = core.calculate_overall_health(-70, 25)
    rendered = core.t(tmpl).format(pct=score)
    assert "Perfect" in rendered
    assert str(score) in rendered
    core.set_language("ru")


def test_i18n_available_languages():
    langs = core.available_languages()
    assert "ru" in langs
    assert "en" in langs


# =========================================================
# Android entry-point — статические проверки без импорта Kivy
# =========================================================

def _read_android_main():
    """Читает исходник android_main.py из корня репо (без импорта Kivy)."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "android_main.py"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def test_android_main_does_not_import_tkinter():
    """Android-точка входа не должна тянуть tkinter — на Android его нет."""
    src = _read_android_main()
    if src is None:
        import pytest
        pytest.skip("android_main.py отсутствует")
    assert "import tkinter" not in src
    assert "from tkinter" not in src


def test_android_main_reuses_core():
    """Android-версия должна переиспользовать общую логику из core."""
    src = _read_android_main()
    if src is None:
        import pytest
        pytest.skip("android_main.py отсутствует")
    assert "from core import" in src
    # Ключевые общие функции должны импортироваться, а не дублироваться
    for name in ("evaluate_signal", "format_band_label", "parse_cell_id",
                 "is_valid_ip", "t"):
        assert name in src, f"android_main должен использовать core.{name}"
