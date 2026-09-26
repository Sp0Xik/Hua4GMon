"""
Состояние сессии мониторинга — общее для Windows и Android.

SignalState получает снимки (Snapshot) и считает всё, что нужно для
наведения антенны: пики и «Δ до пика», историю для графиков, стрелку
тенденции со сглаживанием и гистерезисом, джиттер, события смены соты,
таблицу лучших сот, лог сессии для CSV, признак устаревших данных.

Класс не потокобезопасен намеренно: его вызывает только главный поток UI.
"""
from __future__ import annotations

import datetime
import statistics
from collections import deque
from dataclasses import dataclass

from core.constants import (
    BEST_CELLS_SHOWN,
    CELL_EVENTS_MAX,
    DIRECTION_LOOKBACK,
    GRAPH_HISTORY,
    JITTER_WINDOW,
    SESSION_LOG_MAX,
    STALE_FACTOR,
    STALE_MIN_SECONDS,
    TREND_EMA_ALPHA,
    TREND_ENTER_DB,
    TREND_EXIT_DB,
)
from core.models import METRICS, NR_METRICS, CellKey, Snapshot

TRACKED: tuple[str, ...] = METRICS + NR_METRICS

# Состояния стрелки тенденции.
TREND_COLLECTING = 'collecting'
TREND_UP = 'up'
TREND_DOWN = 'down'
TREND_FLAT = 'flat'


class TrendTracker:
    """Тенденция метрики с подавлением шума.

    Медиана трёх последних значений → экспоненциальное сглаживание →
    разность со значением DIRECTION_LOOKBACK тиков назад. Гистерезис:
    ↑/↓ появляется при |Δ| ≥ enter, а исчезает только при |Δ| < exit —
    стрелка не «мигает» от замираний ±1 дБ.
    """

    def __init__(self, lookback: int = DIRECTION_LOOKBACK,
                 alpha: float = TREND_EMA_ALPHA,
                 enter: float = TREND_ENTER_DB,
                 exit_: float = TREND_EXIT_DB) -> None:
        self.lookback = lookback
        self.alpha = alpha
        self.enter = enter
        self.exit = exit_
        self._raw: deque[float] = deque(maxlen=3)
        self._smooth: deque[float] = deque(maxlen=lookback + 1)
        self._ema: float | None = None
        self.state = TREND_COLLECTING
        self.delta: float | None = None

    def reset(self) -> None:
        self._raw.clear()
        self._smooth.clear()
        self._ema = None
        self.state = TREND_COLLECTING
        self.delta = None

    def push(self, value: float) -> str:
        self._raw.append(value)
        med = statistics.median(self._raw)
        self._ema = med if self._ema is None else (
            self.alpha * med + (1 - self.alpha) * self._ema)
        self._smooth.append(self._ema)
        if len(self._smooth) <= self.lookback:
            self.state = TREND_COLLECTING
            self.delta = None
            return self.state
        delta = self._smooth[-1] - self._smooth[0]
        self.delta = delta
        if self.state == TREND_UP:
            if delta < self.exit:
                self.state = TREND_DOWN if delta <= -self.enter else TREND_FLAT
        elif self.state == TREND_DOWN:
            if delta > -self.exit:
                self.state = TREND_UP if delta >= self.enter else TREND_FLAT
        elif delta >= self.enter:
            self.state = TREND_UP
        elif delta <= -self.enter:
            self.state = TREND_DOWN
        else:
            self.state = TREND_FLAT
        return self.state


@dataclass(frozen=True, slots=True)
class CellChange:
    """Смена обслуживающей соты."""
    at: str                 # время ЧЧ:ММ:СС
    old: CellKey
    new: CellKey
    old_label: str
    new_label: str


@dataclass(slots=True)
class CellStats:
    """Лучшие значения, увиденные на одной соте за сессию."""
    key: CellKey
    band_label: str
    enodeb: int | None
    sector: int | None
    best_sinr: float | None = None
    best_rsrp: float | None = None
    samples: int = 0

    def update(self, snap: Snapshot) -> None:
        self.samples += 1
        if snap.sinr is not None and (self.best_sinr is None or snap.sinr > self.best_sinr):
            self.best_sinr = snap.sinr
        if snap.rsrp is not None and (self.best_rsrp is None or snap.rsrp > self.best_rsrp):
            self.best_rsrp = snap.rsrp
        if snap.enodeb is not None:
            self.enodeb, self.sector = snap.enodeb, snap.sector
        if snap.band_label and snap.band_label != '-':
            self.band_label = snap.band_label


def cell_label(snap: Snapshot) -> str:
    """'B3 · PCI 287' — короткая подпись соты."""
    band = f"B{snap.bands[0]}" if snap.bands else "?"
    pci = snap.pci if snap.pci is not None else "?"
    return f"{band} · PCI {pci}"


class SignalState:
    """Накопленное состояние сессии мониторинга."""

    def __init__(self, history: int = GRAPH_HISTORY,
                 log_max: int = SESSION_LOG_MAX,
                 trend_param: str = 'sinr') -> None:
        self.history: dict[str, deque[float]] = {
            p: deque(maxlen=history) for p in TRACKED}
        self.peaks: dict[str, float | None] = dict.fromkeys(TRACKED)
        self.session_min: dict[str, float | None] = dict.fromkeys(TRACKED)
        self.session_max: dict[str, float | None] = dict.fromkeys(TRACKED)
        self.log: deque[dict] = deque(maxlen=log_max)
        self.events: deque[CellChange] = deque(maxlen=CELL_EVENTS_MAX)
        self.cells: dict[CellKey, CellStats] = {}
        self.observed_bands: set[int] = set()
        self.trend_param = trend_param
        self.tracker = TrendTracker()
        self.last: Snapshot | None = None
        self.last_ok: float | None = None
        self._cell: CellKey | None = None
        self._cell_label = ''
        self._rsrp_in_cell = 0          # отсчётов RSRP с последней смены соты

    # ---- управление ----

    def set_trend_param(self, param: str) -> None:
        if param != self.trend_param:
            self.trend_param = param
            self.tracker.reset()

    def reset_peaks(self) -> None:
        self.peaks = dict.fromkeys(TRACKED)

    def reset(self) -> None:
        """Полный сброс (новое подключение)."""
        self.__init__(history=self.history['rsrp'].maxlen or GRAPH_HISTORY,
                      log_max=self.log.maxlen or SESSION_LOG_MAX,
                      trend_param=self.trend_param)

    # ---- приём данных ----

    def ingest(self, snap: Snapshot, now: float,
               wall: datetime.datetime | None = None) -> CellChange | None:
        """Принимает снимок. Возвращает событие смены соты, если оно было."""
        wall = wall or datetime.datetime.now()
        self.last = snap
        self.last_ok = now

        for p in TRACKED:
            v = snap.metric(p)
            if v is None:
                continue
            self.history[p].append(v)
            if self.peaks[p] is None or v > self.peaks[p]:
                self.peaks[p] = v
            if self.session_min[p] is None or v < self.session_min[p]:
                self.session_min[p] = v
            if self.session_max[p] is None or v > self.session_max[p]:
                self.session_max[p] = v

        change = self._track_cell(snap, wall)
        if change is not None:
            self._rsrp_in_cell = 0
        if snap.metric('rsrp') is not None:
            self._rsrp_in_cell += 1

        value = snap.metric(self.trend_param)
        if value is not None:
            self.tracker.push(value)
        else:
            # Метрика пропала (например, NR при уходе с 5G): без данных
            # стрелка не должна давать прежнее указание.
            self.tracker.reset()

        self.observed_bands.update(snap.bands)
        self._append_log(snap, wall)
        return change

    def _track_cell(self, snap: Snapshot, wall: datetime.datetime) -> CellChange | None:
        key = snap.cell
        if not key.known:
            return None
        stats = self.cells.get(key)
        if stats is None:
            stats = CellStats(key, snap.band_label, snap.enodeb, snap.sector)
            self.cells[key] = stats
        stats.update(snap)

        label = cell_label(snap)
        change = None
        if self._cell is not None and key != self._cell:
            change = CellChange(wall.strftime('%H:%M:%S'), self._cell, key,
                                self._cell_label, label)
            self.events.append(change)
            # Сравнивать уровень двух разных сот бессмысленно — тренд заново.
            self.tracker.reset()
        self._cell = key
        self._cell_label = label
        return change

    def _append_log(self, snap: Snapshot, wall: datetime.datetime) -> None:
        self.log.append({
            'ts': wall.isoformat(timespec='seconds'),
            'rsrp': snap.rsrp, 'rssi': snap.rssi,
            'sinr': snap.sinr, 'rsrq': snap.rsrq,
            'nr_rsrp': snap.nr_rsrp, 'nr_sinr': snap.nr_sinr,
            'plmn': snap.plmn,
            'enodeb': snap.enodeb, 'sector': snap.sector,
            'pci': snap.pci, 'earfcn': snap.earfcn,
            'bands': snap.bands_compact, 'rat': snap.rat,
        })

    # ---- вычисляемые величины ----

    def current(self, param: str) -> float | None:
        return self.last.metric(param) if self.last is not None else None

    def delta_to_peak(self, param: str) -> float | None:
        """Сколько дБ не хватает до лучшего значения сессии (≤ 0)."""
        cur, peak = self.current(param), self.peaks.get(param)
        if cur is None or peak is None:
            return None
        return round(cur - peak, 1)

    def jitter(self) -> float | None:
        """Размах RSRP за последние JITTER_WINDOW тиков на текущей соте.

        Скачок уровня при смене соты — не «гуляние» антенны.
        """
        hist = self.history['rsrp']
        if min(len(hist), self._rsrp_in_cell) < JITTER_WINDOW:
            return None
        recent = list(hist)[-JITTER_WINDOW:]
        return max(recent) - min(recent)

    @property
    def trend(self) -> str:
        return self.tracker.state

    def age(self, now: float) -> float | None:
        return None if self.last_ok is None else now - self.last_ok

    def is_stale(self, now: float, interval: float) -> bool:
        """Данные устарели: ответа нет дольше STALE_FACTOR интервалов."""
        if self.last_ok is None:
            return True
        return now - self.last_ok > max(STALE_MIN_SECONDS, STALE_FACTOR * interval)

    def top_cells(self, n: int = BEST_CELLS_SHOWN) -> list[CellStats]:
        """Лучшие соты сессии: по SINR, затем по RSRP."""
        def key(c: CellStats) -> tuple[float, float]:
            return (c.best_sinr if c.best_sinr is not None else -999.0,
                    c.best_rsrp if c.best_rsrp is not None else -999.0)
        return sorted(self.cells.values(), key=key, reverse=True)[:n]
