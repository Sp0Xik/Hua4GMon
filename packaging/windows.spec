# -*- mode: python ; coding: utf-8 -*-
# Сборка Windows-версии (CI: .github/workflows/build.yml):
#     pyinstaller --noconfirm packaging/windows.spec
#
# Один анализ — две сборки в dist/:
#   Hua4GMon/              portable-папка: Hua4GMon.exe + _internal.
#                          Ничего не распаковывает при запуске — открывается
#                          за секунды даже на слабом ноутбуке с антивирусом.
#   Hua4GMon-onefile.exe   один файл. При каждом запуске распаковывается во
#                          временную папку, и антивирус проверяет её заново;
#                          пока идёт распаковка, видна заставка splash.png.
#
# Из обеих сборок убраны файлы, которые программа не загружает
# (tools/bundle_filter.py): меньше файлов — быстрее распаковка и проверка.
# VERSIONINFO (свойства файла в Проводнике) — из core.__version__.
import pkgutil
import sys

ROOT = os.path.dirname(SPECPATH)
sys.path.insert(0, ROOT)

from tools import bundle_filter, make_version_info  # noqa: E402

os.makedirs(workpath, exist_ok=True)
version_file = os.path.join(workpath, "version_info.txt")
with open(version_file, "w", encoding="utf-8") as fh:
    fh.write(make_version_info.render(make_version_info.read_version()))

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    # Все модули core, в том числе те, что main.py не импортирует напрямую.
    hiddenimports=[f"core.{m.name}" for m in pkgutil.iter_modules([os.path.join(ROOT, "core")])],
    # Тяжёлые пакеты, которые анализ иногда подтягивает транзитивно.
    # setuptools попадает через необязательный импорт backports.zstd в
    # urllib3 (анализ находит его среди библиотек, вложенных в setuptools),
    # а runtime-хук PyInstaller для setuptools импортирует его при каждом
    # запуске — около 270 модулей. Программе setuptools не нужен.
    excludes=["pytest", "kivy", "numpy", "matplotlib", "PIL", "pandas", "scipy",
              "setuptools", "pkg_resources", "_distutils_hack"],
)
binaries = bundle_filter.trim(a.binaries, bundle_filter.keep_binary)
datas = bundle_filter.trim(a.datas, bundle_filter.keep_data)
pyz = PYZ(a.pure)

# upx=False: сжатие UPX замедляет запуск и вызывает ложные срабатывания AV.
exe_options = dict(
    version=version_file,
    icon=os.path.join(ROOT, "icon.ico"),
    console=False,
    upx=False,
)

# Заставка загрузчика: видна сразу после запуска, пока .exe распаковывается;
# строка внизу показывает распаковываемые файлы. Закрывает её программа,
# когда окно готово (main.close_splash).
splash = Splash(
    os.path.join(SPECPATH, "splash.png"),
    binaries=binaries,
    datas=datas,
    text_pos=(144, 150),
    text_size=-11,
    text_color="#666666",
    text_default="...",
    always_on_top=False,
)
EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    binaries,
    datas,
    name="Hua4GMon-onefile",
    **exe_options,
)

# Заставка нужна только однофайловой сборке. Без модуля pyi_splash
# main.close_splash ничего не делает.
portable = EXE(
    PYZ([entry for entry in a.pure if entry[0] != "pyi_splash"]),
    a.scripts,
    exclude_binaries=True,
    name="Hua4GMon",
    **exe_options,
)
COLLECT(portable, binaries, datas, name="Hua4GMon", upx=False)
