# -*- mode: python ; coding: utf-8 -*-
# Сборка Windows-версии — один portable .exe (CI: .github/workflows/build.yml):
#     pyinstaller --noconfirm packaging/windows.spec      →  dist/Hua4GMon.exe
#
# Однофайловый .exe при каждом запуске распаковывает содержимое во
# временную папку, а антивирус проверяет распакованные файлы. Поэтому:
#   * в сборку не попадает то, что программа не загружает
#     (tools/bundle_filter.py, excludes ниже) — меньше файлов, быстрее старт;
#   * пока идёт распаковка и загрузка, видна заставка splash.png с ходом
#     запуска.
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
    # Первым делом сообщает заставке, что распаковка закончилась.
    runtime_hooks=[os.path.join(SPECPATH, "splash_rthook.py")],
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

# Заставка загрузчика: появляется, как только .exe начал работать.
# Строка внизу — ход запуска: «Распаковка…», имена распаковываемых файлов,
# «Загрузка программы…» (splash_rthook.py), «Построение окна…» (main.py).
# Закрывает заставку программа, когда окно готово (main.close_splash).
splash = Splash(
    os.path.join(SPECPATH, "splash.png"),
    binaries=binaries,
    datas=datas,
    text_pos=(144, 150),
    text_size=-11,
    text_color="#666666",
    text_default="Распаковка…",
    always_on_top=False,
)

# upx=False: сжатие UPX замедляет запуск и вызывает ложные срабатывания AV.
EXE(
    PYZ(a.pure),
    a.scripts,
    splash,
    splash.binaries,
    binaries,
    datas,
    name="Hua4GMon",
    version=version_file,
    icon=os.path.join(ROOT, "icon.ico"),
    console=False,
    upx=False,
)
