"""Windows-сборка: отбор файлов (tools/bundle_filter.py) и packaging/windows.spec.

Сама сборка идёт в CI на Windows; здесь проверяется логика spec-файла
без PyInstaller и то, что список нативных модулей pycryptodomex совпадает
с тем, что библиотека действительно загружает.
"""
import os
import pathlib
import struct
import subprocess
import sys
import types

import pytest

import core
from tools import bundle_filter

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "windows.spec"


@pytest.mark.parametrize("dest", [
    "Cryptodome/Cipher/_pkcs1_decode.pyd",
    "Cryptodome\\Hash\\_SHA1.pyd",
    "Cryptodome/Math/_modexp.abi3.so",
    "Cryptodome/Util/_strxor.pyd",
    "_tkinter.pyd",
    "libcrypto-3.dll",
    "charset_normalizer/md.cp313-win_amd64.pyd",
])
def test_needed_binaries_kept(dest):
    assert bundle_filter.keep_binary(dest)


@pytest.mark.parametrize("dest", [
    "Cryptodome/Cipher/_raw_aes.pyd",
    "Cryptodome\\Hash\\_SHA256.pyd",
    "Cryptodome/Util/_cpuid_c.abi3.so",
    "Cryptodome/PublicKey/_ec_ws.pyd",
])
def test_unused_crypto_binaries_dropped(dest):
    assert not bundle_filter.keep_binary(dest)


@pytest.mark.parametrize(("dest", "kept"), [
    ("_tcl_data/tzdata/Europe/Moscow", False),
    ("_tcl_data\\msgs\\ru.msg", False),
    ("_tk_data/images/logo.eps", False),
    ("_tcl_data/init.tcl", True),
    ("_tcl_data/encoding/cp1251.enc", True),
    ("_tk_data/msgs/ru.msg", True),          # подписи стандартных диалогов Tk
    ("_tk_data/ttk/clamTheme.tcl", True),
    ("certifi/cacert.pem", True),
    ("base_library.zip", True),
])
def test_data_filter(dest, kept):
    assert bundle_filter.keep_data(dest) is kept


def test_trim_keeps_toc_entries_intact():
    toc = [("Cryptodome/Hash/_SHA1.pyd", "/src/a", "BINARY"),
           ("Cryptodome/Hash/_MD5.pyd", "/src/b", "BINARY")]
    assert bundle_filter.trim(toc, bundle_filter.keep_binary) == toc[:1]


_SPY = """
import Cryptodome.Util._raw_api as raw
seen = set()
original = raw.load_pycryptodome_raw_lib
def spy(name, cdecl):
    seen.add(name.replace(".", "/"))
    return original(name, cdecl)
raw.load_pycryptodome_raw_lib = spy
from core.router import library_self_test
library_self_test()
print("\\n".join(sorted(seen)))
"""


def test_crypto_whitelist_matches_library_loading():
    """Ровно те модули, что грузит huawei-lte-api без GMP (как на Windows)."""
    pytest.importorskip("huawei_lte_api")
    env = {**os.environ, "PYCRYPTODOME_DISABLE_GMP": "1"}
    out = subprocess.run([sys.executable, "-c", _SPY], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert set(out.split()) == bundle_filter.CRYPTO_NATIVES


# ---------- packaging/windows.spec ----------

def _run_spec(tmp_path, monkeypatch):
    """Выполняет spec с заглушками классов PyInstaller и возвращает цели."""
    made: list[types.SimpleNamespace] = []

    def target(kind, **attrs):
        def factory(*args, **kwargs):
            obj = types.SimpleNamespace(kind=kind, args=args, kwargs=kwargs, **attrs)
            made.append(obj)
            return obj
        return factory

    analysis = target(
        "Analysis",
        pure=[("main", "m.py", "PYMODULE"), ("pyi_splash", "s.py", "PYMODULE")],
        scripts=[("main", "main.py", "PYSOURCE")],
        binaries=[("Cryptodome/Hash/_SHA1.pyd", "a", "BINARY"),
                  ("Cryptodome/Cipher/_raw_aes.pyd", "b", "BINARY"),
                  ("tcl86t.dll", "c", "BINARY")],
        datas=[("_tcl_data/init.tcl", "d", "DATA"),
               ("_tcl_data/tzdata/UTC", "e", "DATA")],
    )
    namespace = {
        "Analysis": analysis, "PYZ": target("PYZ"), "EXE": target("EXE"),
        "COLLECT": target("COLLECT"),
        "Splash": target("Splash", binaries=[("tk86t.dll", "f", "BINARY")]),
        "SPECPATH": str(SPEC.parent), "workpath": str(tmp_path / "work"), "os": os,
    }
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), namespace)  # noqa: S102
    return {kind: [o for o in made if o.kind == kind]
            for kind in ("Analysis", "PYZ", "EXE", "COLLECT", "Splash")}


def test_spec_builds_onefile_and_portable(tmp_path, monkeypatch):
    t = _run_spec(tmp_path, monkeypatch)
    (analysis,) = t["Analysis"]
    assert {"core.router", "core.demo", "core.i18n"} <= set(analysis.kwargs["hiddenimports"])
    assert "setuptools" in analysis.kwargs["excludes"]

    onefile, portable = t["EXE"]
    trimmed_bin = [("Cryptodome/Hash/_SHA1.pyd", "a", "BINARY"), ("tcl86t.dll", "c", "BINARY")]
    trimmed_dat = [("_tcl_data/init.tcl", "d", "DATA")]
    assert onefile.kwargs["name"] == "Hua4GMon-onefile"
    assert trimmed_bin in onefile.args and trimmed_dat in onefile.args
    (splash,) = t["Splash"]
    assert splash in onefile.args and splash.binaries in onefile.args
    assert splash.kwargs["binaries"] == trimmed_bin

    assert portable.kwargs["name"] == "Hua4GMon" and portable.kwargs["exclude_binaries"]
    assert splash not in portable.args
    portable_pyz = portable.args[0]
    assert portable_pyz.args[0] == [("main", "m.py", "PYMODULE")]   # без pyi_splash
    (collect,) = t["COLLECT"]
    assert collect.args == (portable, trimmed_bin, trimmed_dat)
    assert collect.kwargs["name"] == "Hua4GMon"

    for exe in (onefile, portable):
        assert exe.kwargs["upx"] is False and exe.kwargs["console"] is False
        assert pathlib.Path(exe.kwargs["icon"]).is_file()
        version_text = pathlib.Path(exe.kwargs["version"]).read_text(encoding="utf-8")
        assert f"'FileVersion', '{core.__version__}'" in version_text


def test_splash_image_fits_pyinstaller_limits():
    assert 'os.path.join(SPECPATH, "splash.png")' in SPEC.read_text(encoding="utf-8")
    data = (SPEC.parent / "splash.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    # Больше 760×480 PyInstaller уменьшает только при наличии Pillow.
    assert width <= 760 and height <= 480
