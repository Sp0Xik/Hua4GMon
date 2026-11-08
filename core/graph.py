"""Shared graph bounds and clipping for Tk and Kivy."""
from __future__ import annotations

import math

from core.constants import GRAPH_MIN_SPAN, PARAM_RANGES


class GraphViewport:
    """Latest time window; manual level bounds survive incoming samples."""

    def __init__(self) -> None:
        self.param = 'sinr'
        self.times: list[float] = []
        self.values: list[float] = []
        self.peak: float | None = None
        self.seconds: float | None = None
        self.levels: tuple[float, float] | None = None

    def reset(self) -> None:
        self.seconds = None
        self.levels = None

    def update(self, values: list[float], times: list[float], param: str,
               peak: float | None) -> None:
        if param != self.param:
            self.reset()
        self.param, self.peak = param, peak
        self.values = list(values)
        self.times = list(times) if len(times) == len(values) else list(map(float, range(len(values))))

    def x_range(self) -> tuple[float, float]:
        end = self.times[-1] if self.times else 0.0
        available = max(end - self.times[0], 1.0) if self.times else 1.0
        return end - min(self.seconds or available, available), end

    def y_range(self) -> tuple[float, float]:
        if self.levels is not None:
            return self.levels
        base = self.param.replace('nr_', '')
        low, high = PARAM_RANGES.get(base, (-140, 40))
        start, _ = self.x_range()
        points = [v for x, v in zip(self.times, self.values, strict=True)
                  if x >= start and math.isfinite(v)]
        if self.peak is not None:
            points.append(self.peak)
        if not points:
            return float(low), float(high)
        lo, hi = min(points), max(points)
        span = GRAPH_MIN_SPAN.get(base, 10.0)
        if hi - lo < span:
            mid = (hi + lo) / 2
            lo, hi = mid - span / 2, mid + span / 2
        lo = max(low, 5 * ((lo - 1) // 5))
        hi = min(high, 5 * -((-(hi + 1)) // 5))
        return float(lo), float(max(hi, lo + 1))

    def zoom(self, axis: str, factor: float) -> None:
        if factor <= 0:
            raise ValueError('Zoom factor must be positive')
        if axis == 'time':
            lo, hi = self.x_range()
            available = max(self.times[-1] - self.times[0], 1.0) if self.times else 1.0
            self.seconds = min(available, max(1.0, (hi - lo) * factor))
        elif axis == 'level':
            lo, hi = self.y_range()
            low, high = PARAM_RANGES.get(self.param.replace('nr_', ''), (-140, 40))
            span = min(high - low, max(1.0, (hi - lo) * factor))
            lo = min(max(low, (hi + lo - span) / 2), high - span)
            self.levels = float(lo), float(lo + span)
        else:
            raise ValueError('Unknown axis')

    def segments(self) -> list[tuple[float, float, float, float]]:
        """Clip line segments to bounds; return normalized coordinates."""
        xmin, xmax = self.x_range()
        ymin, ymax = self.y_range()
        points = [((x - xmin) / (xmax - xmin), (y - ymin) / (ymax - ymin))
                  for x, y in zip(self.times, self.values, strict=True)]
        result = []
        for (x, y), (xx, yy) in zip(points, points[1:], strict=False):
            if not all(math.isfinite(v) for v in (x, y, xx, yy)):
                continue
            dx, dy = xx - x, yy - y
            enter, leave = 0.0, 1.0
            for p, q in ((-dx, x), (dx, 1 - x), (-dy, y), (dy, 1 - y)):
                if p == 0:
                    if q < 0:
                        break
                elif p < 0:
                    enter = max(enter, q / p)
                else:
                    leave = min(leave, q / p)
                if enter > leave:
                    break
            else:
                result.append((x + enter * dx, y + enter * dy,
                               x + leave * dx, y + leave * dy))
        return result


class GestureViewport(GraphViewport):
    """Touch-only viewport; the desktop viewport keeps its existing behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.offset = 0.0

    def reset(self) -> None:
        super().reset()
        self.offset = 0.0

    def x_range(self) -> tuple[float, float]:
        lo, hi = super().x_range()
        first = self.times[0] if len(self.times) > 1 else lo
        offset = min(self.offset, max(0.0, lo - first))
        return lo - offset, hi - offset

    def transform(self, factor: float, anchor: tuple[float, float],
                  translation: tuple[float, float] = (0.0, 0.0)) -> None:
        """Scale around the fingers, then translate by fractions of plot size."""
        if factor <= 0:
            raise ValueError('Zoom factor must be positive')
        if len(self.times) < 2:
            return
        xlo, xhi = self.x_range()
        ylo, yhi = self.y_range()
        first, last = self.times[0], self.times[-1]
        available = max(last - first, 1.0)
        width = min(available, max(1.0, (xhi - xlo) * factor))
        end = xlo + anchor[0] * (xhi - xlo) + (1 - anchor[0]) * width
        end -= translation[0] * width
        end = min(last, max(last - available + width, end))
        self.seconds, self.offset = width, last - end
        low, high = PARAM_RANGES.get(self.param.replace('nr_', ''), (-140, 40))
        height = min(high - low, max(1.0, (yhi - ylo) * factor))
        bottom = ylo + anchor[1] * (yhi - ylo) - anchor[1] * height
        bottom -= translation[1] * height
        bottom = min(high - height, max(low, bottom))
        self.levels = bottom, bottom + height
