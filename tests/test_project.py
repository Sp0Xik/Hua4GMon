"""Проверки целостности проекта: локализация, слой совместимости,
закреплённые версии, сборочные файлы, документация, VERSIONINFO."""
import ast
import pathlib
import re

import pytest

import core
from core.i18n import EN
from tools import i18n_keys, make_version_info

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_FILES = [ROOT / "main.py", ROOT / "android_main.py", *sorted((ROOT / "core").glob("*.py")),
                *sorted((ROOT / "tools").glob("*.py"))]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# =========================================================
# Локализация
# =========================================================

def test_every_ui_string_has_translation():
    missing = sorted(k for k in i18n_keys.all_keys() if k not in EN)
    assert not missing, f"Нет EN-перевода: {missing[:10]}"


def test_no_dead_translations():
    used = i18n_keys.all_keys()
    unused = sorted(k for k in EN if k not in used)
    assert not unused, f"Переводы без использования (удалите): {unused[:10]}"


def test_android_text_fits_bundled_font():
    """Каждый символ, который может показать Android-версия, есть в её шрифте.

    Kivy не подставляет недостающий символ из другого шрифта: цветные
    эмодзи (🔊 🧪 ✅ …) на Android рисуются квадратом.
    """
    ttlib = pytest.importorskip("fontTools.ttLib")
    cmap = ttlib.TTFont(ROOT / "assets" / "DejaVuSans.ttf").getBestCmap()
    shown = i18n_keys.string_constants(ROOT / "android_main.py")
    for path in sorted((ROOT / "core").glob("*.py")):
        if path.name != "i18n.py":      # EN-словарь содержит и Windows-подписи
            shown |= i18n_keys.string_constants(path)
    shown |= i18n_keys.core_indirect()
    shown |= {EN[k] for k in shown if k in EN}
    bad = {}
    for text in shown:
        missing = sorted({ch for ch in text if ord(ch) not in cmap and ch not in "\n\r\t"})
        if missing:
            bad[text] = missing
    assert not bad, f"Нет в DejaVuSans.ttf (на Android будет квадрат): {bad}"
    assert ord("🔊") not in cmap, "проверка должна ловить цветные эмодзи"


PLACEHOLDER = re.compile(r"\{([a-z_]*)(?::[^}]*)?\}")


def test_translation_placeholders_match():
    """{pct}, {bands} и т.п. должны совпадать — иначе .format() упадёт."""
    for ru, en in EN.items():
        assert set(PLACEHOLDER.findall(ru)) == set(PLACEHOLDER.findall(en)), ru


# =========================================================
# Слой совместимости huawei-lte-api
# =========================================================

def _imports(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_library_imported_only_in_router():
    offenders = [p.name for p in SOURCE_FILES
                 if p.name != "router.py"
                 and any(n.split('.')[0] == "huawei_lte_api" for n in _imports(p))]
    assert not offenders, f"huawei_lte_api импортируется вне core/router.py: {offenders}"


@pytest.mark.parametrize("ui", ["main.py", "android_main.py"])
def test_ui_does_not_call_router_api_directly(ui):
    src = read(ui)
    for forbidden in (".reboot(", "set_net_mode(", "set_antenna_settings(", "set_control(",
                      "Connection(", "Client(", "tcp_reachable("):
        assert forbidden not in src, f"{ui}: прямой вызов {forbidden} — используйте core"


def test_no_deprecated_reboot_anywhere():
    for p in SOURCE_FILES:
        assert "device.reboot(" not in p.read_text(encoding="utf-8"), p.name


# =========================================================
# Чистота кода
# =========================================================

def test_no_todo_markers():
    # Отдельные слова: имена вроде PYCRYPTODOME_DISABLE_GMP — не пометки.
    # Шаблон собран из частей, чтобы не находить сам себя.
    markers = re.compile(r"\b(?:" + "TO" + "DO|" + "FIX" + "ME|" + "X" + r"XX:)")
    files = [*SOURCE_FILES, *sorted((ROOT / "tests").glob("*.py")),
             *sorted(ROOT.glob("*.md")), *sorted((ROOT / ".github").rglob("*.yml")),
             ROOT / "buildozer.spec", ROOT / "pyproject.toml",
             *sorted((ROOT / "packaging").glob("*.py")), ROOT / "packaging" / "windows.spec"]
    for p in files:
        m = markers.search(p.read_text(encoding="utf-8"))
        assert not m, f"{p.relative_to(ROOT)} содержит {m.group()}"


# =========================================================
# Версии и сборка
# =========================================================

def _requirements() -> dict[str, str]:
    pins = {}
    for line in read("requirements.txt").splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==")
            pins[name.strip().lower()] = ver.strip()
    return pins


def _spec_value(key: str) -> str:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", read("buildozer.spec"), re.MULTILINE)
    assert m, f"{key} нет в buildozer.spec"
    return m.group(1).strip()


def test_version_single_source_in_buildozer():
    spec = read("buildozer.spec")
    assert not re.search(r"^version\s*=", spec, re.MULTILINE)
    assert _spec_value("version.filename") == "%(source.dir)s/core/__init__.py"
    m = re.search(_spec_value("version.regex"), read("core/__init__.py"))
    assert m and m.group(1) == core.__version__


def test_android_pins_match_requirements():
    pins = _requirements()
    assert pins["huawei-lte-api"] == "2.0.1"
    reqs = {}
    for item in _spec_value("requirements").split(","):
        if "==" in item:
            name, ver = item.split("==")
            reqs[name.strip().lower()] = ver.strip()
    for name, ver in reqs.items():
        assert pins.get(name) == ver, f"{name}: spec {ver} != requirements {pins.get(name)}"
    assert set(reqs) == set(pins) - {"pycryptodomex"}


def test_android_toolchain_pinned():
    """Закреплённый стек: свежий p4a тянет Python 3.14 и ломает pyjnius."""
    assert _spec_value("p4a.branch") == "v2024.01.21"
    assert _spec_value("android.ndk") == "25b"
    assert _spec_value("android.release_artifact") == "apk"
    assert "usesCleartextTraffic" not in read("buildozer.spec")
    wf = read(".github/workflows/build-android.yml")
    action = read(".github/actions/buildozer-apk/action.yml")
    assert re.search(r"^\s+buildozer-version: '1\.5\.0'$", wf, re.MULTILINE)
    assert '"cython==0.29.36"' in action


@pytest.mark.parametrize("workflow", ["build.yml", "build-android.yml"])
def test_every_push_builds(workflow):
    """Каждый push в main собирает обе платформы: фильтр путей однажды
    оставил коммит только с Windows-изменениями без APK."""
    wf = read(f".github/workflows/{workflow}")
    push = wf[wf.index("  push:"):wf.index("  workflow_dispatch:")]
    assert "branches: [ main ]" in push
    assert "paths" not in push


def test_apk_build_step_survives_yes_pipe():
    """`yes | buildozer` под pipefail роняет успешную сборку (EPIPE у yes)."""
    action = read(".github/actions/buildozer-apk/action.yml")
    step = action[action.index("- name: Build APK"):]
    assert step.index("set +o pipefail") < step.index("yes | buildozer")
    wf = read(".github/workflows/build-android.yml")
    assert "| head" not in wf, "head в конвейере под pipefail может уронить шаг"


def test_windows_build_is_single_exe_with_self_test():
    """CI собирает один .exe из spec и проверяет его --self-test."""
    wf = read(".github/workflows/build.yml")
    job = wf[wf.index("build-windows:"):]
    assert "pyinstaller --noconfirm packaging/windows.spec" in job
    assert "--onefile" not in job, "параметры сборки — только в packaging/windows.spec"
    # Загрузчик PyInstaller — собственной сборки, и это проверяется до сборки .exe.
    install = job[job.index("- name: Install dependencies"):job.index("- name: Verify self-built")]
    assert "PYINSTALLER_COMPILE_BOOTLOADER: '1'" in install
    assert "pip install --no-binary pyinstaller -r requirements-build.txt" in install
    verify = job[job.index("- name: Verify self-built"):job.index("- name: Build single-file EXE")]
    assert "python tools/bootloader_check.py" in verify and "exit 1" in verify
    assert "3 { Write-Error" in verify, "код 3 = готовый загрузчик (tools/bootloader_check.EXIT_STOCK)"
    smoke = job[job.index("- name: Smoke-test EXE"):job.index("- name: Stage release assets")]
    assert "Start-Process dist/Hua4GMon.exe -ArgumentList '--self-test'" in smoke
    assert "exit 1" in smoke
    release = job[job.index("- name: Create GitHub Release"):]
    assert "${{ env.EXE_NAME }}" in release
    for other in ("installer.iss", "iscc", "innosetup", "setup.exe", ".zip"):
        assert other not in job.lower(), f"Windows-версия — только один .exe: {other}"
    assert "make_version_info.render" in read("packaging/windows.spec")


def test_library_version_matches_pin():
    assert core.library_version() == _requirements()["huawei-lte-api"]


# =========================================================
# VERSIONINFO (Windows .exe)
# =========================================================

# Сигнатуры конструкторов PyInstaller 6.x (PyInstaller/utils/win32/versioninfo.py).
_PYI_SIGNATURES = {
    'VSVersionInfo': ['ffi', 'kids'],
    'FixedFileInfo': ['filevers', 'prodvers', 'mask', 'flags', 'OS', 'fileType',
                      'subtype', 'date'],
    'StringFileInfo': ['kids'], 'StringTable': ['name', 'kids'],
    'StringStruct': ['name', 'val'], 'VarFileInfo': ['kids'], 'VarStruct': ['name', 'kids'],
}


def _pyinstaller_signatures() -> dict[str, list[str]]:
    import importlib.util
    spec = importlib.util.find_spec("PyInstaller")
    if spec is None or not spec.origin:
        return _PYI_SIGNATURES
    src = pathlib.Path(spec.origin).parent / "utils" / "win32" / "versioninfo.py"
    sigs = {}
    for node in ast.parse(src.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef) and node.name in _PYI_SIGNATURES:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    sigs[node.name] = [a.arg for a in item.args.args[1:]]
    return sigs


def test_version_info_evaluates_like_pyinstaller():
    captured = {}

    def stub(name, params):
        def make(*args, **kwargs):
            bound = dict(zip(params, args, strict=False))
            unknown = set(kwargs) - set(params)
            assert not unknown, f"{name}: неизвестные аргументы {unknown}"
            bound.update(kwargs)
            captured.setdefault(name, []).append(bound)
            return bound
        return make

    sigs = _pyinstaller_signatures()
    assert sigs == _PYI_SIGNATURES, "PyInstaller изменил формат VERSIONINFO"
    text = make_version_info.render(make_version_info.read_version(), year=2026)
    eval(text, {name: stub(name, params) for name, params in sigs.items()})  # noqa: S307
    ver = tuple(int(x) for x in core.__version__.split("."))
    assert captured['FixedFileInfo'][0]['filevers'] == (*ver, 0)
    strings = {s['name']: s['val'] for s in captured['StringStruct']}
    assert strings['FileVersion'] == strings['ProductVersion'] == core.__version__
    assert strings['OriginalFilename'] == "Hua4GMon.exe"


def test_version_info_cli(tmp_path):
    out = tmp_path / "vi.txt"
    assert make_version_info.main(["x", str(out)]) == 0
    assert core.__version__ in out.read_text(encoding="utf-8")


def test_read_version_rejects_missing(tmp_path):
    bad = tmp_path / "__init__.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        make_version_info.read_version(bad)


# =========================================================
# Документация и лицензии
# =========================================================

def test_changelog_has_current_version():
    assert f"## [{core.__version__}]" in read("CHANGELOG.md")


def _hardware_marked_names() -> set[str]:
    """Имена функций/классов (или констант модуля) с пометкой о проверке на железе."""
    marker = "Hardware validation required"
    names: set[str] = set()
    for p in SOURCE_FILES:
        text = p.read_text(encoding="utf-8")
        nodes = list(ast.walk(ast.parse(text)))
        scopes = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        assigns = [n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign))]
        for lineno, line in enumerate(text.splitlines(), start=1):
            if marker not in line:
                continue
            enclosing = [n for n in scopes if n.lineno <= lineno <= n.end_lineno]
            if enclosing:
                names.add(max(enclosing, key=lambda n: n.lineno).name)
                continue
            following = min((n for n in assigns if n.lineno > lineno), key=lambda n: n.lineno)
            target = following.targets[0] if isinstance(following, ast.Assign) else following.target
            names.add(target.id)
    return names


def test_hardware_markers_registered():
    doc = read("HARDWARE_VALIDATION.md")
    missing = sorted(n for n in _hardware_marked_names() if f"`{n}" not in doc)
    assert not missing, f"Не описаны в HARDWARE_VALIDATION.md: {missing}"


def test_license_texts_present():
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "DejaVu-Fonts.txt"):
        assert (ROOT / "LICENSES" / name).stat().st_size > 1000, name
