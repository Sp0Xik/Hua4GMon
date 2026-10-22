"""Закрепляет версии Android-зависимостей по requirements.txt (CI, сборка APK).

Единственный источник версий — requirements.txt: его обновляет Dependabot,
по нему ставятся зависимости Windows-сборки и тестов. В buildozer.spec
перечислены только ИМЕНА пакетов, а этот скрипт перед сборкой APK
дописывает к ним версии (в копию spec в папке сборки):

    python -m tools.android_requirements build-android/buildozer.spec requirements.txt

Так версии Windows и Android не расходятся и не требуют ручной правки
после обновления зависимостей. Без версии остаются только рецепты
python-for-android (их версию задаёт закреплённый p4a). Скрипт падает,
если в spec не хватает пакета из requirements.txt (p4a ставит пакеты без
зависимостей — пропущенный пакет сломал бы APK) или там есть посторонний
незакреплённый пакет.
"""
from __future__ import annotations

import pathlib
import re
import sys

# Рецепты python-for-android: версию задаёт закреплённый p4a, не requirements.txt.
P4A_RECIPES = frozenset({"python3", "kivy", "pycryptodome"})
# Только для Windows: на Android вместо него рецепт pycryptodome.
WINDOWS_ONLY = frozenset({"pycryptodomex"})

_SPEC_LINE = re.compile(r"^requirements\s*=\s*(.+)$", re.MULTILINE)


def read_pins(text: str) -> dict[str, str]:
    """`name==version` из requirements.txt → {имя в нижнем регистре: версия}."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    return pins


def spec_names(spec: str) -> list[str]:
    """Имена пакетов из строки requirements buildozer.spec."""
    m = _SPEC_LINE.search(spec)
    if not m:
        raise ValueError("в buildozer.spec нет строки requirements")
    return [item.strip() for item in m.group(1).split(",") if item.strip()]


def pinned_requirements(names: list[str], pins: dict[str, str]) -> str:
    """Строка requirements для p4a: пакеты из requirements.txt — с версиями."""
    out: list[str] = []
    for name in names:
        key = name.lower()
        if "==" in name:
            raise ValueError(f"{name}: версии в buildozer.spec не пишутся — их берут из requirements.txt")
        if key in pins:
            out.append(f"{name}=={pins[key]}")
        elif key in P4A_RECIPES:
            out.append(name)
        else:
            raise ValueError(f"{name}: нет в requirements.txt и это не рецепт p4a")
    missing = set(pins) - WINDOWS_ONLY - {n.lower() for n in names}
    if missing:
        raise ValueError(f"нет в buildozer.spec: {', '.join(sorted(missing))}")
    return ",".join(out)


def pin_spec(spec: str, pins: dict[str, str]) -> str:
    """buildozer.spec с версиями в строке requirements."""
    line = pinned_requirements(spec_names(spec), pins)
    return _SPEC_LINE.sub(lambda _m: f"requirements = {line}", spec, count=1)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.android_requirements <buildozer.spec> <requirements.txt>")
        return 2
    spec_path, req_path = (pathlib.Path(a) for a in argv)
    pins = read_pins(req_path.read_text(encoding="utf-8"))
    spec = pin_spec(spec_path.read_text(encoding="utf-8"), pins)
    spec_path.write_text(spec, encoding="utf-8")
    print("requirements =", ",".join(spec_names(spec)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
