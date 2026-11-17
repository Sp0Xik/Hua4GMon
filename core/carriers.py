"""Explicit LTE carrier tuples from Huawei's band description, not neighbours."""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.parsers import earfcn_to_band


@dataclass(frozen=True)
class LTECarrier:
    band: int
    earfcn: int
    bandwidth: float


def parse_lte_carriers(raw: str) -> tuple[LTECarrier, ...]:
    # Observed format: 20MHz@2850(B7) + 20MHz@3048(B7).
    # A bare B7+B7 does not identify two distinct carriers.
    result: list[LTECarrier] = []
    if len(raw.split('+')) > 16:
        return ()
    for part in raw.split('+')[:16]:
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)MHz@(\d+)\(B(\d+)\)\s*', part, re.I)
        if match is None:
            return ()
        width, channel, band = float(match[1]), int(match[2]), int(match[3])
        if width not in (1.4, 3, 5, 10, 15, 20) or earfcn_to_band(channel) != band:
            return ()
        if any(c.earfcn == channel for c in result):
            return ()
        result.append(LTECarrier(band, channel, width))
    return tuple(result)
