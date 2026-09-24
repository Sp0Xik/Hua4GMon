"""Сбор ключей локализации из исходников (используется тестом i18n).

Ключи — русские строки, которые попадают в интерфейс:
  * в UI-файлах (main.py, android_main.py) — все кириллические строковые
    константы, кроме докстрингов, аргументов логгера и справки argparse;
  * в core — литералы, переданные в t("..."), и данные, которые UI
    переводит косвенно (пороги сигнала, подписи, сообщения, советы).
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_FILES = ("main.py", "android_main.py")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LOGGER_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
# Справка командной строки — не интерфейс приложения.
CLI_CALLS = {"add_argument", "ArgumentParser"}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _skipped_call_args(tree: ast.AST) -> set[int]:
    """Аргументы логгера и argparse — не переводятся."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in LOGGER_METHODS or name in CLI_CALLS:
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                for sub in ast.walk(arg):
                    ids.add(id(sub))
    return ids


def ui_strings(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree) | _skipped_call_args(tree)
    from core.i18n import LANGUAGES
    language_names = set(LANGUAGES.values())
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip and CYRILLIC.search(node.value)
                and node.value not in language_names):
            found.add(node.value)
    return found


def t_literals(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "t" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value)
    return found


def core_indirect() -> set[str]:
    """Русские строки из данных core, которые UI передаёт в t()."""
    import core
    from core import errors, rf
    keys: set[str] = set()
    for rules in core.SIGNAL_THRESHOLDS.values():
        keys.update(label for _thr, label, _col, _pct in rules)
    keys.update({"Нет данных", "Н/Д"})
    for rsrp, sinr in ((-70, 25), (-85, 15), (-100, 5), (-120, -5)):
        keys.add(core.calculate_overall_health(rsrp, sinr)[1])
    keys.update(rf.RAT_LABELS.values())
    keys.update(rf.MIMO_LABELS.values())
    keys.update(rf.UPLINK_LABELS.values())
    keys.update(errors._MESSAGES.values())
    keys.update(core.ANTENNA_MODES)
    keys.update(rf.advice(rsrp=-85, sinr=2, rsrq=-17, jitter=9,
                          mimo='single', uplink='limit'))
    keys.update(rf.advice(rsrp=-115, sinr=12, rsrq=-8, jitter=1,
                          mimo='imbalance', uplink='ok'))
    keys.update(rf.advice(rsrp=-90, sinr=15, rsrq=-17, jitter=1,
                          mimo='ok', uplink='ok'))
    return {k for k in keys if CYRILLIC.search(k)}


def all_keys() -> set[str]:
    keys: set[str] = set()
    for name in UI_FILES:
        keys |= ui_strings(ROOT / name)
    for path in sorted((ROOT / "core").glob("*.py")):
        keys |= t_literals(path)
    keys |= core_indirect()
    return keys
