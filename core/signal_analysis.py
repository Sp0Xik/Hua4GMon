"""
Оценка качества LTE-сигнала по RSRP/SINR/RSSI/RSRQ.

Чистая логика: на вход число (dBm/dB), на выходе — текстовый статус,
цвет и процент качества для прогресс-бара.
"""
from __future__ import annotations

from typing import Optional, Tuple

from core.constants import SIGNAL_THRESHOLDS


def evaluate_signal(param: str,
                    val: Optional[float]) -> Tuple[str, str, int]:
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


def calculate_overall_health(rsrp: Optional[float],
                              sinr: Optional[float]
                              ) -> Tuple[int, str, str]:
    """Общая оценка качества связи на основе RSRP и SINR.

    Формула: 70% веса от худшего параметра, 30% от лучшего.
    Это даёт реалистичную оценку: один отличный показатель не
    компенсирует один плохой.

    Возвращает (процент 0..100, описание для UI, цвет).
    """
    if rsrp is None or sinr is None:
        return 0, "Нет данных", "gray"
    _, _, r_pct = evaluate_signal('rsrp', rsrp)
    _, _, s_pct = evaluate_signal('sinr', sinr)
    overall = int(min(r_pct, s_pct) * 0.7 + max(r_pct, s_pct) * 0.3)
    overall = max(0, min(100, overall))
    # Возвращаем ШАБЛОН с плейсхолдером {pct}, а не готовую строку:
    # слой отображения переводит его через i18n и подставляет число
    # (template.format(pct=overall)). Так health-сообщение тоже
    # локализуется. Подстрока с названием оценки сохранена для тестов.
    if overall >= 85:
        return overall, "Идеально ({pct}%) — 4K/онлайн-игры", "#00b894"
    if overall >= 65:
        return overall, "Хорошо ({pct}%) — стабильный FullHD", "#2ecc71"
    if overall >= 35:
        return overall, "Умеренно ({pct}%) — крутите антенну", "#fdcb6e"
    return overall, "Плохо ({pct}%) — будет рваться!", "#d63031"
