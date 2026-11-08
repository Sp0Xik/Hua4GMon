"""Bounded survey of observations actually supplied by the modem.

No band or operator switching. No inferred PLMN for neighbour observations.
"""
from __future__ import annotations

import csv
import datetime
import io
import statistics
from dataclasses import dataclass, field, fields
from typing import Any

from core.i18n import t
from core.models import Snapshot, build_snapshot

SURVEY_HELP = ('Старт → медленно вращайте антенну с остановками → Стоп. '
               'Сохраняются только наблюдения, которые отдаёт модем, не все БС вокруг. '
               'Поиск операторов может временно прервать интернет. '
               'Список операторов не содержит уровней сигнала отдельных БС.')


@dataclass
class Observation:
    snap: Snapshot
    source: str
    first: str
    last: str
    samples: int = 0
    registered_samples: int = 0
    levels: dict[str, list[float]] = field(default_factory=dict)

    def add(self, snap: Snapshot, at: str) -> None:
        self.snap, self.last = snap, at
        self.samples += 1
        self.registered_samples += int(snap.registered)
        for key in ('sinr', 'rsrp', 'rsrq', 'rssi', 'nr_sinr', 'nr_rsrp', 'nr_rsrq'):
            value = snap.metric(key)
            if value is not None:
                values = self.levels.setdefault(key, [])
                values.append(value)
                del values[:-600]

    def median(self, metric: str) -> float | None:
        values = self.levels.get(metric, [])
        return statistics.median(values) if values else None


class Survey:
    def __init__(self) -> None:
        self.running = False
        self.generation = 0
        self.rows: dict[tuple[Any, ...], Observation] = {}
        self.operators: list[dict[str, str]] = []
        self.messages: list[str] = []
        self.limit_reached = False

    def start(self) -> None:
        self.generation += 1
        self.rows.clear()
        self.operators.clear()
        self.messages.clear()
        self.limit_reached = False
        self.running = True

    def stop(self) -> None:
        self.running = False

    def observe(self, snap: Snapshot, source: str = 'device/signal') -> None:
        if not self.running:
            return
        # Partial observations stay explicitly unidentified; never invent a Cell ID.
        if not any((snap.earfcn is not None, snap.nr_arfcn is not None,
                    snap.cell_id is not None, snap.nci is not None)):
            return
        key = (source, snap.plmn, snap.cell_id, snap.nci, snap.earfcn, snap.pci,
               snap.nr_arfcn, snap.nr_pci, snap.registration)
        at = datetime.datetime.now().isoformat(timespec='seconds')
        if key not in self.rows:
            if len(self.rows) >= 1000:
                self.limit_reached = True
                return
            self.rows[key] = Observation(snap, source, at, at)
        self.rows[key].add(snap, at)

    def accept_discovery(self, dumps: dict[str, Any], errors: dict[str, str]) -> None:
        self.messages = [t('Не удалось прочитать: {name} ({error})').format(name=k, error=v)
                         for k, v in errors.items()]
        self.operators = []
        for item in records(dumps.get('plmn_list')):
            numeric = str(item.get('Numeric') or '')
            if numeric.isdigit() and len(numeric) in (5, 6):
                row = {k: str(item.get(k) or '') for k in
                       ('Numeric', 'FullName', 'ShortName', 'Rat', 'State')}
                if row not in self.operators:
                    self.operators.append(row)
        self.accept_cells(dumps.get('cell_info'))
        if not self.operators:
            self.messages.append(t('Список операторов не получен. Накопление сигнала доступно.'))

    def accept_cells(self, data: Any) -> None:
        count = 0
        for item in records(data):
            # Only explicit fields matching device/signal semantics are accepted.
            # Unknown vendor structures remain available in diagnostics.
            if not any(k in item for k in ('rsrp', 'nrrsrp', 'earfcn', 'nrearfcn')):
                continue
            snap = build_snapshot({**item, '_network_uncertain': True})
            if snap.earfcn is not None or snap.nr_arfcn is not None:
                self.observe(snap, 'net/cell-info')
                count += 1
        message = t('Дополнительные соты с распознаваемыми параметрами не получены.')
        if not count and message not in self.messages:
            self.messages.append(message)
        elif count and message in self.messages:
            self.messages.remove(message)

    def ranked(self) -> list[Observation]:
        def score(row: Observation) -> tuple[float, ...]:
            metrics = ('nr_sinr', 'nr_rsrq', 'nr_rsrp') if row.snap.has_nr else ('sinr', 'rsrq', 'rsrp')
            values = [row.median(k) for k in metrics]
            return (float(row.registered_samples > 0),
                    *(v if v is not None else -999.0 for v in values))
        return sorted(self.rows.values(), key=score, reverse=True)

    def report(self) -> str:
        def value(v: Any) -> str:
            return t('Нет данных') if v is None or v == '' else str(v)
        lines = [t('Сбор идёт') if self.running else t('Сбор остановлен'), t(SURVEY_HELP),
                 t('Сначала зарегистрированные сети; далее по медиане SINR, RSRQ, RSRP. Скорость не измеряется.')]
        if self.limit_reached:
            lines.append(t('Достигнут предел 1000 наблюдений. Остановите сбор и сохраните результат.'))
        for index, row in enumerate(self.ranked(), 1):
            s = row.snap
            lines.extend(['', f'{index}. {s.operator or s.plmn or t("Оператор неизвестен")} ({s.plmn or "—"})',
                          t(s.network_message),
                          f'Cell ID: {value(s.cell_id or s.nci)} · PCI: {value(s.pci if s.pci is not None else s.nr_pci)}',
                          f'{s.band_label} · EARFCN: {value(s.earfcn)} · NR-ARFCN: {value(s.nr_arfcn)}',
                          f'DL MHz: {value(s.freq_mhz)} · TAC: {value(s.tac)} · BW: {value(s.dl_bandwidth or s.nr_bandwidth)}',
                          t('Источник: {source}; измерений: {n}; {first} — {last}').format(
                              source=row.source, n=row.samples, first=row.first, last=row.last)])
            if s.cell_id is None and s.nci is None:
                lines.append(t('Cell ID неизвестен: наблюдение не идентифицирует отдельную БС.'))
            for metric, values in row.levels.items():
                lines.append(f'{metric.upper()}: {statistics.median(values):g} '
                             f'[{min(values):g} … {max(values):g}]')
        if self.operators:
            lines.extend(['', t('Обнаруженные операторы (без радиометрик):')])
            for op in self.operators:
                lines.append(f'{op["Numeric"]} {op["FullName"] or op["ShortName"]} · RAT {op["Rat"]} · State {op["State"]}')
        lines.extend(self.messages)
        return '\n'.join(lines)

    def csv_text(self) -> str:
        output = io.StringIO(newline='')
        names = [f.name for f in fields(Snapshot) if f.name != 'raw']
        metrics = ('sinr', 'rsrp', 'rsrq', 'rssi', 'nr_sinr', 'nr_rsrp', 'nr_rsrq')
        stats = [f'{key}_{stat}' for key in metrics for stat in ('median', 'min', 'max')]
        writer = csv.DictWriter(output, fieldnames=[
            'source', 'first', 'last', 'samples', 'rat_code', 'operator_state', *names, *stats])
        writer.writeheader()
        for row in self.ranked():
            values = {name: getattr(row.snap, name) for name in names}
            values.update(source=row.source, first=row.first, last=row.last, samples=row.samples)
            for metric in metrics:
                data = row.levels.get(metric, [])
                if data:
                    values.update({f'{metric}_median': statistics.median(data),
                                   f'{metric}_min': min(data), f'{metric}_max': max(data)})
            writer.writerow(values)
        for op in self.operators:
            writer.writerow({'source': 'net/plmn-list', 'plmn': op['Numeric'],
                             'operator': op['FullName'] or op['ShortName'],
                             'rat_code': op['Rat'], 'operator_state': op['State']})
        return output.getvalue()


def records(data: Any, depth: int = 0):
    """Bound recursion for unexpected firmware responses."""
    if depth > 12:
        return
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from records(value, depth + 1)
    elif isinstance(data, list):
        for value in data[:1000]:
            yield from records(value, depth + 1)
