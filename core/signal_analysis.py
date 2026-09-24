"""
Оценка качества LTE-сигнала по RSRP/SINR/RSSI/RSRQ.

Чистая логика: на вход число (dBm/dB), на выходе — текстовый статус,
цвет и процент качества для прогресс-бара.
"""
from __future__ import annotations

from core.constants import HEALTH_CURVES, SIGNAL_THRESHOLDS


def evaluate_signal(param: str,
                    val: float | None) -> tuple[str, str, int]:
    """Возвращает (текст_статуса, цвет, процент_качества).

    Параметры
    ---------
    param : 'rsrp', 'sinr', 'rssi' или 'rsrq'
    val   : значение в dBm или dB, или None если данных нет
    """
    if val is None:
        return "Нет данных", "gray", 0
    rules = SIGNAL_THRESHOLDS.get(param)
    if not rules:
        return "Н/Д", "gray", 0
    for threshold, text, color, pct in rules:
        if threshold is None or val >= threshold:
            return text, color, pct
    return "Н/Д", "gray", 0


def curve_score(param: str, val: float | None) -> float:
    """Непрерывная оценка 0..100 по опорным точкам HEALTH_CURVES."""
    points = HEALTH_CURVES.get(param)
    if val is None or not points:
        return 0.0
    if val <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if val <= x1:
            return y0 + (y1 - y0) * (val - x0) / (x1 - x0)
    return points[-1][1]


def calculate_overall_health(rsrp: float | None,
                             sinr: float | None
                             ) -> tuple[int, str, str]:
    """Общая оценка качества связи на основе RSRP и SINR.

    Формула: 70% веса от худшего параметра, 30% от лучшего — один
    отличный показатель не компенсирует один плохой. Внутри порогов
    оценка непрерывна, поэтому прогресс-бар откликается на поворот
    антенны даже на 1–2 дБ.

    Возвращает (процент 0..100, шаблон описания с {pct}, цвет).
    """
    if rsrp is None or sinr is None:
        return 0, "Нет данных", "gray"
    r_pct = curve_score('rsrp', rsrp)
    s_pct = curve_score('sinr', sinr)
    overall = int(round(min(r_pct, s_pct) * 0.7 + max(r_pct, s_pct) * 0.3))
    overall = max(0, min(100, overall))
    # Возвращаем ШАБЛОН с плейсхолдером {pct}: слой отображения переводит
    # его через i18n и подставляет число.
    if overall >= 85:
        return overall, "Отличный сигнал ({pct}%)", "#00b894"
    if overall >= 65:
        return overall, "Хороший сигнал ({pct}%)", "#2ecc71"
    if overall >= 35:
        return overall, "Средний сигнал — крутите антенну ({pct}%)", "#fdcb6e"
    return overall, "Слабый сигнал — ищите лучше ({pct}%)", "#d63031"
