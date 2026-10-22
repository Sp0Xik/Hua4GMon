"""
Читаемые цвета текста на светлом фоне.

Цвета статусов в core (SIGNAL_THRESHOLDS, общая оценка, белые списки)
подобраны для тёмного фона: на тёмной теме Android и в крышном режиме
Windows они контрастны. На светлом фоне — окно Windows, тема «Солнце» на
Android — жёлтый и зелёный почти не видны (контраст 1,3–2,2:1). Здесь
цвет затемняется, пока не даст контраст WCAG не ниже 4,5:1 (норма для
обычного текста), с сохранением оттенка. Модуль чистый.
"""
from __future__ import annotations

# Фон окна Windows (SystemButtonFace) и карточки темы «Солнце» на Android.
WINDOWS_BG = '#f0f0f0'
SUN_CARD_BG = '#ebedf0'
MIN_CONTRAST = 4.5          # WCAG 2.x, уровень AA для обычного текста

NAMED: dict[str, str] = {
    'black': '#000000', 'white': '#ffffff', 'gray': '#808080', 'grey': '#808080',
    'green': '#008000', 'orange': '#ffa500', 'red': '#ff0000',
}
# Подобрано вручную: равномерное затемнение жёлтого даёт оливковый, а
# «Средний» должен читаться как предупреждение (тёмный янтарный).
_DARK_VARIANTS: dict[str, str] = {'#fdcb6e': '#8a6100'}

RGB = tuple[int, int, int]


def to_rgb(color: str) -> RGB:
    """'#00b894', '#0b9', 'gray' → (0, 184, 148). ValueError — если не разобрать."""
    h = NAMED.get(color.strip().lower(), color.strip()).lstrip('#')
    if len(h) == 3:
        h = ''.join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"цвет не разобран: {color!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: RGB) -> str:
    return '#{:02x}{:02x}{:02x}'.format(*rgb)


def relative_luminance(color: str) -> float:
    """Относительная яркость по WCAG 2.x (0 — чёрный, 1 — белый)."""
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = to_rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """Контраст двух цветов по WCAG: от 1 до 21."""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(color: str, background: str, min_ratio: float = MIN_CONTRAST) -> str:
    """Цвет текста с контрастом не ниже min_ratio на СВЕТЛОМ фоне.

    Уже читаемый цвет возвращается без изменений; иначе — затемнённый с
    тем же оттенком. На тёмном фоне затемнение не помогает, там цвет
    возвращается как есть (цвета статусов и так подобраны для тёмного).
    """
    if relative_luminance(background) < 0.5 or contrast_ratio(color, background) >= min_ratio:
        return color
    variant = _DARK_VARIANTS.get(to_hex(to_rgb(color)))
    if variant is not None and contrast_ratio(variant, background) >= min_ratio:
        return variant
    r, g, b = to_rgb(color)
    for step in range(99, -1, -1):
        candidate = to_hex((round(r * step / 100), round(g * step / 100), round(b * step / 100)))
        if contrast_ratio(candidate, background) >= min_ratio:
            return candidate
    return '#000000'
