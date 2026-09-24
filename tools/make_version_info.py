"""Генерирует файл VERSIONINFO для PyInstaller (--version-file) из core.__version__.

Использование (CI, Windows):
    python tools/make_version_info.py version_info.txt

Свойства .exe в Проводнике (версия файла и продукта, описание, авторские
права) берутся из единственного источника версии — core/__init__.py.
"""
from __future__ import annotations

import datetime
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r'''__version__ = ['"](\d+)\.(\d+)\.(\d+)['"]''')


def read_version(init_py: pathlib.Path = ROOT / "core" / "__init__.py") -> tuple[int, int, int]:
    m = VERSION_RE.search(init_py.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"__version__ X.Y.Z не найдена в {init_py}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def render(version: tuple[int, int, int], year: int | None = None) -> str:
    year = year or datetime.date.today().year
    text = ".".join(str(v) for v in version)
    quad = (*version, 0)
    strings = {
        "CompanyName": "Sp0Xik",
        "FileDescription": "Hua4GMon — Huawei LTE/5G antenna alignment monitor",
        "FileVersion": text,
        "InternalName": "Hua4GMon",
        "LegalCopyright": f"© 2024-{year} Sp0Xik and Hua4GMon contributors. MIT License.",
        "OriginalFilename": "Hua4GMon.exe",
        "ProductName": "Hua4GMon",
        "ProductVersion": text,
    }
    items = ",\n".join(f"          StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
{items}
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "version_info.txt"
    version = read_version()
    out.write_text(render(version), encoding="utf-8")
    print(f"VERSIONINFO {'.'.join(map(str, version))} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
