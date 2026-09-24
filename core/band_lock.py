"""
Band Lock: безопасное планирование записи в net/net-mode.

Принцип «сначала прочитай»:
  * NetworkBand (2G/3G) и режим сети берутся из текущих настроек роутера —
    меняется только то, что просил пользователь;
  * при подключении сохраняется снимок исходных настроек — кнопка
    «Вернуть как было» восстанавливает их точно;
  * «AUTO» включает все LTE-бэнды, не трогая режим сети;
  * после записи настройки перечитываются и сверяются.

Модуль чистый: работает с данными, а запись выполняет core.router.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.constants import (
    LTE_ALL_MASK,
    LTEBAND_AUTO_ALL,
    NETBAND_AUTO_MASK,
    NETMODE_AUTO,
    NETMODE_LTE_ONLY,
    REGION_BANDS,
)
from core.i18n import t
from core.models import Snapshot
from core.parsers import bands_from_mask, bands_to_mask, parse_hex_mask


@dataclass(frozen=True, slots=True)
class NetModeSettings:
    """Текущие настройки net/net-mode."""
    lte_band: str
    network_band: str
    network_mode: str

    @classmethod
    def from_response(cls, data: Mapping[str, Any] | None) -> NetModeSettings | None:
        if not isinstance(data, Mapping):
            return None
        lte = str(data.get('LTEBand') or '').strip()
        if parse_hex_mask(lte) is None:
            return None
        return cls(
            lte_band=lte,
            network_band=str(data.get('NetworkBand') or '').strip(),
            network_mode=str(data.get('NetworkMode') or '').strip(),
        )

    @property
    def lte_mask(self) -> int:
        return parse_hex_mask(self.lte_band) or 0


@dataclass(frozen=True, slots=True)
class BandLockPlan:
    """Что именно будет записано в net/net-mode."""
    lteband: str
    networkband: str
    networkmode: str


def modem_supports_5g(access_modes: Iterable[str]) -> bool:
    """Есть ли у модема режимы с NR (коды с '08' в net/net-mode-list).

    Hardware validation required: коды режимов 5G на реальном 5G-CPE.
    """
    return any('08' in str(m) for m in access_modes)


def locked_bands(settings: NetModeSettings | None,
                 supported: Iterable[int] = ()) -> list[int] | None:
    """Бэнды, зафиксированные на модеме: [] = AUTO, None = неизвестно."""
    if settings is None:
        return None
    sup = list(supported)
    return bands_from_mask(settings.lte_band, bands_to_mask(sup) if sup else None)


def _network_band(current: NetModeSettings | None) -> str:
    return current.network_band if current and current.network_band else NETBAND_AUTO_MASK


def plan_lock(current: NetModeSettings | None, bands: Iterable[int], *,
              force_lte_only: bool) -> BandLockPlan:
    """План фиксации LTE-бэндов.

    force_lte_only — режим «только 4G» (прежнее поведение, для LTE-модемов).
    Для 5G-модемов передавайте False: режим сети не меняется, 5G не
    выключается.
    """
    mask = bands_to_mask(bands)
    if mask == 0:
        raise ValueError("Не выбрано ни одного бэнда")
    if force_lte_only:
        mode = NETMODE_LTE_ONLY
    else:
        mode = current.network_mode if current and current.network_mode else NETMODE_AUTO
    return BandLockPlan(format(mask, 'X'), _network_band(current), mode)


def plan_auto(current: NetModeSettings | None,
              original: NetModeSettings | None) -> BandLockPlan:
    """Все LTE-бэнды. Режим сети — как был до программы (или текущий)."""
    if original and original.network_mode:
        mode = original.network_mode
    elif current and current.network_mode:
        mode = current.network_mode
    else:
        mode = NETMODE_AUTO
    return BandLockPlan(LTEBAND_AUTO_ALL, _network_band(current), mode)


def plan_restore(original: NetModeSettings) -> BandLockPlan:
    """Точное восстановление настроек, прочитанных при подключении."""
    return BandLockPlan(original.lte_band,
                        original.network_band or NETBAND_AUTO_MASK,
                        original.network_mode or NETMODE_AUTO)


def verify(plan: BandLockPlan, read_back: NetModeSettings | None) -> bool:
    """Совпадает ли записанное с тем, что роутер вернул при чтении.

    Hardware validation required: как прошивка сохраняет маску «все бэнды».
    """
    if read_back is None:
        return False
    want = parse_hex_mask(plan.lteband) or 0
    got = read_back.lte_mask
    if (want & LTE_ALL_MASK) == LTE_ALL_MASK:
        # «Все бэнды»: часть прошивок сохраняет маску поддерживаемых.
        return got != 0 and read_back.network_mode == plan.networkmode
    return got == want and read_back.network_mode == plan.networkmode


def lockable_bands(supported: Iterable[int], observed: Iterable[int],
                   fallback: Iterable[int] = REGION_BANDS) -> list[int]:
    """Бэнды для списка Band Lock: замеченные в эфире — первыми.

    Если модем не сообщил поддерживаемые бэнды — типовые для региона
    плюс замеченные в эфире.
    """
    sup = sorted(set(supported))
    seen = [b for b in sorted(set(observed)) if 1 <= b <= 63]
    base = sup if sup else sorted(set(fallback) | set(seen))
    first = [b for b in seen if b in base]
    return first + [b for b in base if b not in first]


def lock_warnings(bands: Iterable[int], snap: Snapshot | None) -> list[str]:
    """Предупреждения перед применением (уже переведённые)."""
    chosen = set(bands)
    warnings: list[str] = []
    if snap is None or not snap.bands:
        return warnings
    primary, secondary = snap.bands[0], snap.bands[1:]
    if primary not in chosen:
        warnings.append(t("Текущий бэнд B{band} исключён — связь пропадёт до "
                          "перерегистрации модема.").format(band=primary))
    lost = [b for b in secondary if b not in chosen]
    if lost and primary in chosen:
        warnings.append(t("Агрегация пропадёт: не выбраны {bands}.").format(
            bands=", ".join(f"B{b}" for b in lost)))
    return warnings
