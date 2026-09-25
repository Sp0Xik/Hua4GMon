"""Проверка, что загрузчик PyInstaller собран из исходников (CI, Windows).

Загрузчик — та часть .exe, что распаковывает и запускает программу.
Готовый загрузчик из колеса PyInstaller на PyPI встроен в огромное число
чужих программ, в том числе вредоносных, поэтому браузеры и антивирусы
чаще принимают .exe с ним за опасный. CI собирает загрузчик сам
(PYINSTALLER_COMPILE_BOOTLOADER в .github/workflows/build.yml), а этот
скрипт убеждается, что в сборку пойдёт именно он:

    python tools/bootloader_check.py <официальное колесо PyInstaller .whl>

Код возврата 0 — установленный загрузчик отличается от официального.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import zipfile

# Оконный загрузчик для 64-битной Windows — с ним собирается Hua4GMon.exe.
BOOTLOADER = "PyInstaller/bootloader/Windows-64bit-intel/runw.exe"


def installed_bootloader(member: str = BOOTLOADER) -> pathlib.Path:
    import PyInstaller
    return pathlib.Path(PyInstaller.__file__).resolve().parent.parent / member


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_self_built(installed: pathlib.Path, official_wheel: pathlib.Path,
                  member: str = BOOTLOADER) -> bool:
    """Установленный загрузчик не совпадает с загрузчиком из колеса PyPI."""
    with zipfile.ZipFile(official_wheel) as wheel:
        official = wheel.read(member)
    return sha256(installed.read_bytes()) != sha256(official)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    installed = installed_bootloader()
    self_built = is_self_built(installed, pathlib.Path(argv[1]))
    verdict = "собран из исходников" if self_built else "готовый из PyPI"
    print(f"{installed}: sha256 {sha256(installed.read_bytes())} — {verdict}")
    return 0 if self_built else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
