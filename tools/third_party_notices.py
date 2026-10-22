"""Собирает LICENSES/THIRD-PARTY-NOTICES.txt из установленных пакетов (CI).

Тексты лицензий и NOTICE берутся из метаданных пакетов, закреплённых в
requirements.txt, — ровно тех версий, что установлены для сборки:

    python -m tools.third_party_notices requirements.txt LICENSES/THIRD-PARTY-NOTICES.txt

Файл не хранится в репозитории: CI создаёт его при каждой сборке .exe
(прикрепляется к релизу) и APK (кладётся внутрь). Поэтому обновление
зависимостей не требует ручной перегенерации. Скрипт падает, если пакет
не установлен, его версия не совпадает с закреплённой или у него нет
текста лицензии — тогда сборка не выйдет без уведомлений.

Только стандартная библиотека (importlib.metadata).
"""
from __future__ import annotations

import importlib.metadata as md
import pathlib
import re
import sys

from tools.android_requirements import read_pins

HEADER = """\
Hua4GMon — third-party notices
==============================

License and notice texts of the Python packages pinned in requirements.txt,
as published by their authors. The Windows .exe bundles exactly these
versions. The Android APK bundles the same packages, except that instead of
pycryptodomex it uses pycryptodome from the python-for-android recipe (same
authors and license); its other components are listed in the LICENSE file.

Generated at build time from the installed packages
(tools/third_party_notices.py in the source repository).

Native runtime components are listed in the LICENSE file of the project
(section THIRD-PARTY COMPONENTS).
"""

_RULE = "=" * 80
_THIN = "-" * 80
# Файлы лицензий в .dist-info: LICENSE*, LICENCE*, COPYING*, NOTICE*, AUTHORS*.
_LICENSE_FILE = re.compile(r"(?i)^(licen[cs]e|copying|notice|authors)")


def _field(meta: md.PackageMetadata, key: str) -> str:
    return (meta[key] or "").strip() if key in meta else ""


def _license_name(meta: md.PackageMetadata) -> str:
    if expr := _field(meta, "License-Expression"):
        return expr
    short = _field(meta, "License")
    if short and "\n" not in short and len(short) <= 100:
        return short
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return "; ".join(classifiers) or "see license text below"


def _homepage(meta: md.PackageMetadata) -> str:
    if home := _field(meta, "Home-page"):
        return home
    urls = [u.split(",", 1) for u in meta.get_all("Project-URL") or [] if "," in u]
    for label, url in urls:
        if label.strip().lower() in ("homepage", "source", "source code", "code", "repository"):
            return url.strip()
    return ""


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in sorted(dist.files or [], key=str):
        if ".dist-info" in str(f) and _LICENSE_FILE.match(f.name):
            texts.append((f.name, pathlib.Path(str(dist.locate_file(f))).read_text(
                encoding="utf-8", errors="replace").strip()))
    return texts


def package_section(name: str, version: str) -> str:
    """Раздел одного пакета; ValueError — если версия не та или нет лицензии."""
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        raise ValueError(f"{name}: пакет не установлен") from None
    if dist.version != version:
        raise ValueError(f"{name}: установлен {dist.version}, закреплён {version}")
    texts = _license_texts(dist)
    if not texts:
        raise ValueError(f"{name} {version}: в метаданных нет текста лицензии")
    meta = dist.metadata
    lines = [_RULE, f"{name} {version}", f"License: {_license_name(meta)}"]
    if url := _homepage(meta):
        lines.append(f"Homepage: {url}")
    for filename, text in texts:
        lines += [_THIN, f"[{filename}]", "", text]
    return "\n".join(lines)


def render(pins: dict[str, str]) -> str:
    sections = [package_section(name, pins[name]) for name in sorted(pins)]
    return HEADER + "\n" + "\n\n".join(sections) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tools.third_party_notices <requirements.txt> <output>")
        return 2
    req_path, out_path = (pathlib.Path(a) for a in argv)
    pins = read_pins(req_path.read_text(encoding="utf-8"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(pins), encoding="utf-8")
    print(f"{out_path}: {len(pins)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
