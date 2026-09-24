"""
RF/LTE-аналитика: величины, которые вычисляются из снимка сигнала и
помогают точнее навести антенну.

Все функции чистые. Тексты — русские ключи для core.i18n.t().
"""
from __future__ import annotations

from core.constants import NETWORK_TYPE_EX_RAT

# Код технологии → подпись для UI (ключи i18n).
RAT_LABELS: dict[str, str] = {
    'none': "Нет сети",
    '2g': "2G",
    '3g': "3G",
    'lte': "LTE",
    'lte_ca': "LTE+ (CA)",
    'nr_nsa': "5G NSA",
    'nr_sa': "5G SA",
    '': "-",
}

# Максимальная мощность абонентского устройства класса 3 — 23 dBm.
UPLINK_LIMIT_DBM = 20.0
UPLINK_HIGH_DBM = 15.0


def classify_rat(network_type_ex: int | None, has_lte: bool, has_nr: bool) -> str:
    """Технология связи.

    5G определяется по наличию NR-метрик: коды CurrentNetworkTypeEx для 5G
    на разных прошивках различаются.
        LTE + NR метрики  → NSA (EN-DC, LTE-якорь + 5G);
        только NR         → SA.
    Hardware validation required: коды 5G на реальном 5G-CPE.
    """
    if has_nr:
        return 'nr_nsa' if has_lte else 'nr_sa'
    if network_type_ex is not None and network_type_ex in NETWORK_TYPE_EX_RAT:
        return NETWORK_TYPE_EX_RAT[network_type_ex]
    return 'lte' if has_lte else ''


def detect_ca(network_type_ex: int | None, band_count: int) -> bool | None:
    """Активна ли агрегация несущих.

    CurrentNetworkTypeEx — авторитетный источник: 1011 = LTE+ (CA),
    101 = LTE без CA. Без него — по числу распознанных бэндов.
    Hardware validation required: соответствие кодов на B636.
    """
    if network_type_ex == 1011:
        return True
    if network_type_ex == 101:
        return False
    if band_count <= 0:
        return None
    return band_count > 1


def mimo_status(cqi0: int | None, cqi1: int | None) -> str | None:
    """Состояние потоков MIMO по CQI двух кодовых слов.

    'single'    — второй поток не передаётся (CQI1 = 0 при нормальном CQI0);
    'imbalance' — потоки сильно различаются;
    'ok'        — два потока работают.
    None — роутер не отдаёт оба CQI, вывод сделать нельзя.
    Hardware validation required: наличие cqi1 при одном потоке на B636.
    """
    if cqi0 is None or cqi1 is None:
        return None
    if cqi1 == 0 and cqi0 >= 7:
        return 'single'
    if abs(cqi0 - cqi1) >= 4:
        return 'imbalance'
    return 'ok'


MIMO_LABELS: dict[str, str] = {
    'single': "1 поток",
    'imbalance': "потоки неравны",
    'ok': "2 потока",
}


def uplink_status(pusch_dbm: float | None) -> str | None:
    """Насколько модем «кричит» в аплинк: 'limit' / 'high' / 'ok'."""
    if pusch_dbm is None:
        return None
    if pusch_dbm >= UPLINK_LIMIT_DBM:
        return 'limit'
    if pusch_dbm >= UPLINK_HIGH_DBM:
        return 'high'
    return 'ok'


UPLINK_LABELS: dict[str, str] = {
    'limit': "на пределе",
    'high': "повышенная",
    'ok': "норма",
}


def advice(*, rsrp: float | None, sinr: float | None, rsrq: float | None,
           jitter: float | None, mimo: str | None,
           uplink: str | None) -> list[str]:
    """Практические подсказки монтажнику по текущим метрикам (в порядке важности)."""
    tips: list[str] = []
    if rsrp is not None and sinr is not None:
        if rsrp >= -95 and sinr < 5:
            tips.append("Уровень есть, но много помех: сместите азимут, "
                        "попробуйте другой сектор или проверьте поляризацию.")
        elif rsrp < -110 and sinr >= 10:
            tips.append("Сигнал чистый, но слабый: поднимите антенну выше, "
                        "возьмите антенну с большим усилением или укоротите кабель.")
    if rsrq is not None and sinr is not None and rsrq < -15 and sinr >= 13:
        tips.append("Сота загружена: сравните с соседней сотой или другим бэндом.")
    if mimo == 'single':
        tips.append("Работает один поток MIMO: проверьте второй кабель "
                    "и поляризацию второго порта.")
    elif mimo == 'imbalance':
        tips.append("Потоки MIMO неравны: проверьте разъёмы и кабели обоих портов.")
    if uplink == 'limit':
        tips.append("Модем передаёт почти на максимуме мощности: отдача "
                    "ограничена — нужна антенна точнее или выше.")
    if jitter is not None and jitter >= 7:
        tips.append("Сигнал «гуляет»: проверьте крепление антенны и переотражения.")
    return tips
