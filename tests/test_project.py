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
    # Обычное и жирное начертания: жирным выводятся значения и заголовки.
    cmaps = [set(ttlib.TTFont(ROOT / "assets" / name).getBestCmap())
             for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")]
    cmap = set.intersection(*cmaps)
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
                      "Connection(", "Client("):
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


def test_android_requirements_come_from_requirements_txt():
    """Версии Android-пакетов не дублируются в buildozer.spec: CI дописывает их
    из requirements.txt перед сборкой. Обновление от Dependabot не требует
    ручной правки spec, а APK собирается с теми же версиями, что .exe."""
    from tools.android_requirements import P4A_RECIPES, pin_spec, read_pins, spec_names
    spec = read("buildozer.spec")
    pins = read_pins(read("requirements.txt"))
    assert pins == _requirements()
    assert not [n for n in spec_names(spec) if "==" in n], "версии в buildozer.spec не пишутся"
    pinned = set(spec_names(pin_spec(spec, pins)))
    expected = {f"{n}=={v}" for n, v in pins.items() if n != "pycryptodomex"} | P4A_RECIPES
    assert pinned == expected


@pytest.mark.parametrize(("spec_line", "error"), [
    ("python3,kivy,requests==2.0,pycryptodome", "версии в buildozer.spec"),
    ("python3,kivy,pycryptodome", "нет в buildozer.spec: requests"),
    ("python3,kivy,requests,pycryptodome,six", "six: нет в requirements.txt"),
])
def test_android_requirements_refuses_bad_spec(spec_line, error):
    """Пропущенный пакет сломал бы APK (p4a ставит без зависимостей),
    незакреплённый посторонний сделал бы сборку невоспроизводимой."""
    from tools.android_requirements import pin_spec
    with pytest.raises(ValueError, match=error):
        pin_spec(f"[app]\nrequirements = {spec_line}\n", {"requests": "2.34.2"})


def test_android_build_pins_requirements_before_cache_and_build():
    """Версии дописываются в копию spec до ключа кэша и сборки; уведомления
    о лицензиях кладутся в APK из тех же версий."""
    action = read(".github/actions/buildozer-apk/action.yml")
    pin = action.index("python -m tools.android_requirements build-android/buildozer.spec requirements.txt")
    notices = action.index("-m tools.third_party_notices")
    assert action.index("- name: Stage Android sources") < pin < notices
    assert notices < action.index("uses: actions/cache@") < action.index("buildozer -v android")
    assert "requirements.txt build-android/LICENSES/THIRD-PARTY-NOTICES.txt" in action


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


@pytest.mark.parametrize("workflow", ["build.yml", "build-android.yml"])
def test_workflow_supply_chain_hygiene(workflow):
    """Токен не остаётся в .git для шагов со сторонним кодом; релиз — черновик,
    который публикуется вручную, когда в нём есть и .exe, и APK."""
    wf = read(f".github/workflows/{workflow}")
    checkouts = len(re.findall(r"uses: actions/checkout@v\d+", wf))
    assert checkouts and wf.count("persist-credentials: false") == checkouts
    release = wf[wf.index("softprops/action-gh-release@"):]
    assert "draft: true" in release.split("files:")[0]
    # Сторонние actions (не actions/* и не свои ./) — только по SHA коммита:
    # тег можно переписать, а у шага публикации есть право записи.
    for ref in re.findall(r"uses: (\S+)", wf):
        if not ref.startswith(("actions/", "./")):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), \
                f"сторонний action не закреплён по SHA: {ref}"
    # Пароли подписи APK — только в env шага сборки, не в GITHUB_ENV всех шагов.
    assert "_PASSWD=$" not in wf


def test_signing_material_removed_before_release_steps():
    """После сборки APK ключ подписи удаляется, а демон Gradle (пароли в его
    окружении) останавливается — до загрузки артефакта и стороннего action
    публикации, даже если сборка упала."""
    wf = read(".github/workflows/build-android.yml")
    build = wf[wf.index("\n  build:"):wf.index("\n  canary:")]
    after_build = build[build.index("uses: ./.github/actions/buildozer-apk"):]
    # Шаг сразу после сборки — очистка.
    steps = re.split(r"\n      - ", after_build)
    cleanup = steps[1]
    assert cleanup.startswith("name: Remove signing material")
    assert "if: always()" in cleanup
    assert 'rm -f "$RUNNER_TEMP/release.keystore"' in cleanup
    assert '> "$RUNNER_TEMP/release.keystore"' in build, "удаляется тот же файл, что записан"
    assert "pkill -f 'org.gradle.launcher.daemon' || true" in cleanup


def test_lint_checks_types():
    """mypy (core/, tools/) идёт в CI закреплённой версией."""
    wf = read(".github/workflows/build.yml")
    lint = wf[wf.index("  lint:"):wf.index("  test:")]
    assert "- run: mypy" in lint
    assert re.search(r"^mypy==\S+$", read("requirements-dev.txt"), re.MULTILINE)
    assert 'files = ["core", "tools"]' in read("pyproject.toml")


def test_linux_runners_pinned_to_ubuntu_24_04():
    """Образ раннера меняется только явно: ubuntu-latest переходит на 26.04,
    ubuntu-22.04 удаляется 17.04.2027."""
    for workflow in ("build.yml", "build-android.yml"):
        runners = re.findall(r"runs-on:\s*(\S+)", read(f".github/workflows/{workflow}"))
        linux = [r for r in runners if not r.startswith("windows")]
        assert linux and set(linux) == {"ubuntu-24.04"}, f"{workflow}: {runners}"


def test_every_ci_job_has_timeout():
    """Зависший раннер или apt не держит PR часами (по умолчанию — 6 ч)."""
    for workflow in ("build.yml", "build-android.yml"):
        wf = read(f".github/workflows/{workflow}")
        jobs = re.findall(r"^  ([\w-]+):\n((?:    .*\n|\n)+)", wf.split("\njobs:\n", 1)[1], re.MULTILINE)
        assert jobs, workflow
        for name, body in jobs:
            assert re.search(r"^    timeout-minutes: \d+", body, re.MULTILINE), f"{workflow}: {name}"


def test_android_gate_runs_android_ui_tests():
    """APK собирается только после тестов Android-интерфейса (Kivy под Xvfb)."""
    wf = read(".github/workflows/build-android.yml")
    test_job = wf[wf.index("  test:"):wf.index("  build:")]
    assert '"kivy==2.3.0"' in test_job and "xvfb-run -a pytest" in test_job


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


def test_third_party_notices_generated_from_installed_packages():
    """Уведомления собираются при сборке из метаданных установленных пакетов:
    каждый пакет requirements.txt — с закреплённой версией и текстом лицензии."""
    from tools.third_party_notices import render
    text = render(_requirements())
    for name, version in _requirements().items():
        parts = text.split(f"\n{name} {version}\n", 1)
        assert len(parts) == 2, f"{name} {version}: нет в уведомлениях"
        body = parts[1].split("=" * 80, 1)[0]
        assert "License: " in body and len(body) > 500, f"{name}: нет текста лицензии"


def test_third_party_notices_refuse_wrong_version_or_missing_package():
    """Сборка не выйдет с уведомлениями не от тех версий или без лицензии."""
    from tools.third_party_notices import package_section
    with pytest.raises(ValueError, match="закреплён 0.0.1"):
        package_section("requests", "0.0.1")
    with pytest.raises(ValueError, match="не установлен"):
        package_section("hua4gmon-no-such-package", "1.0")


def test_release_attaches_generated_notices():
    """.exe-задача создаёт уведомления до публикации и прикладывает их к релизу;
    в репозитории файла нет — он не может устареть."""
    wf = read(".github/workflows/build.yml")
    job = wf[wf.index("  build-windows:"):]
    gen = job.index("python -m tools.third_party_notices requirements.txt LICENSES/THIRD-PARTY-NOTICES.txt")
    release = job.index("- name: Create GitHub Release")
    assert gen < release
    assert "LICENSES/THIRD-PARTY-NOTICES.txt" in job[release:]
    assert "LICENSES/THIRD-PARTY-NOTICES.txt" in read(".gitignore")
