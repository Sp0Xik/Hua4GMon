"""
Замер точки установки: устойчивая оценка сигнала в одном положении антенны.

Пик сессии — максимум шумного ряда: он завышен и достигается случайным
выбросом. Замер собирает отсчёты за фиксированное время, пока антенна
неподвижна, и даёт медиану и разброс p10–p90. По медианам сравниваются
положения антенны и места установки, лучшая точка становится ориентиром
(«Δ к лучшей точке»).

Модуль чистый: время и снимки передаёт вызывающий код (SignalState).
"""
from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from core.constants import MEASURE_MIN_SAMPLES
from core.i18n import t
from core.models import CellKey, Snapshot

# Метрики, по которым считается замер (есть у снимка — попадают в замер).
POINT_METRICS: tuple[str, ...] = ('sinr', 'rsrp', 'rsrq', 'rssi', 'nr_sinr', 'nr_rsrp')
# Сколько последних отсчётов сравнивать с лучшей точкой: медиана трёх
# гасит одиночный выброс и почти не запаздывает.
CURRENT_SAMPLES = 3


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль с линейной интерполяцией (как numpy по умолчанию). values — не пустой."""
    data = sorted(values)
    pos = (len(data) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


@dataclass(frozen=True, slots=True)
class Summary:
    """Медиана и разброс одной метрики за замер."""
    median: float
    p10: float
    p90: float

    @property
    def spread(self) -> float:
        return round(self.p90 - self.p10, 1)


def summarize(values: Sequence[float]) -> Summary | None:
    if not values:
        return None
    return Summary(round(percentile(values, 0.5), 1), round(percentile(values, 0.1), 1),
                   round(percentile(values, 0.9), 1))


@dataclass(frozen=True, slots=True)
class PointResult:
    """Итог замера одной точки."""
    number: int
    at: str                         # время начала, ЧЧ:ММ:СС
    samples: int
    metrics: Mapping[str, Summary] = field(compare=False)
    cell: CellKey | None = None     # сота, на которой прошло большинство отсчётов
    cell_label: str = ''            # 'B3 · PCI 287'
    band_label: str = ''
    cell_changed: bool = False      # за время замера сота менялась

    @property
    def ok(self) -> bool:
        """Замер пригоден: достаточно отсчётов и есть SINR или RSRP."""
        return self.samples >= MEASURE_MIN_SAMPLES and bool(
            {'sinr', 'rsrp', 'nr_sinr', 'nr_rsrp'} & set(self.metrics))

    def median(self, param: str) -> float | None:
        s = self.metrics.get(param)
        return s.median if s is not None else None


class PointRecorder:
    """Идущий замер: собирает отсчёты до истечения времени."""

    def __init__(self, started: float, duration: float,
                 wall: datetime.datetime | None = None) -> None:
        self.started = started
        self.duration = duration
        self.at = (wall or datetime.datetime.now()).strftime('%H:%M:%S')
        self.values: dict[str, list[float]] = {p: [] for p in POINT_METRICS}
        self.cells: Counter[CellKey] = Counter()
        self.labels: dict[CellKey, tuple[str, str]] = {}
        self.samples = 0

    def progress(self, now: float) -> float:
        return min(1.0, max(0.0, (now - self.started) / self.duration))

    def remaining(self, now: float) -> float:
        return max(0.0, self.started + self.duration - now)

    def due(self, now: float) -> bool:
        return now >= self.started + self.duration

    def add(self, snap: Snapshot, now: float, cell_label: str) -> None:
        if now < self.started or self.due(now):
            return
        self.samples += 1
        for p in POINT_METRICS:
            v = snap.metric(p)
            if v is not None:
                self.values[p].append(v)
        if snap.cell.known:
            self.cells[snap.cell] += 1
            self.labels.setdefault(snap.cell, (cell_label, snap.band_label))

    def result(self, number: int) -> PointResult:
        metrics = {p: s for p, vals in self.values.items() if (s := summarize(vals)) is not None}
        cell = self.cells.most_common(1)[0][0] if self.cells else None
        cell_label, band_label = self.labels.get(cell, ('', '')) if cell else ('', '')
        return PointResult(number, self.at, self.samples, metrics, cell, cell_label,
                           band_label, cell_changed=len(self.cells) > 1)


def best_point(points: Iterable[PointResult], param: str) -> PointResult | None:
    """Лучшая точка по медиане param; при равенстве — по медиане RSRP."""
    scored = [p for p in points if p.median(param) is not None]
    if not scored:
        return None
    return max(scored, key=lambda p: (p.median(param), p.median('rsrp') or -999.0))


def current_median(history: Sequence[float], n: int = CURRENT_SAMPLES) -> float | None:
    """Медиана последних n отсчётов — «сейчас» для сравнения с точкой."""
    tail = list(history)[-n:]
    return round(percentile(tail, 0.5), 1) if tail else None


def format_summary(s: Summary | None) -> str:
    """'14 (12…16)' — медиана и разброс p10…p90."""
    if s is None:
        return "-"
    def num(v: float) -> str:
        return f"{v:g}"
    return f"{num(s.median)} ({num(s.p10)}…{num(s.p90)})"


def point_lines(points: Sequence[PointResult], param: str = 'sinr') -> list[str]:
    """Текстовая таблица замеров (Android, буфер обмена). Лучшая точка — ★."""
    best = best_point(points, param)
    lines = []
    for p in points:
        mark = "★ " if best is not None and p is best else ""
        parts = [f"{mark}{t('Точка {n}').format(n=p.number)} · {p.at}"]
        for name, key in (("SINR", 'sinr'), ("RSRP", 'rsrp'), ("RSRQ", 'rsrq'),
                          ("5G SINR", 'nr_sinr')):
            if key in p.metrics:
                parts.append(f"{name} {format_summary(p.metrics[key])}")
        cell = p.cell_label or "?"
        if p.cell_changed:
            cell += " · " + t("сота менялась")
        parts.append(cell)
        lines.append(" · ".join(parts))
    return lines
