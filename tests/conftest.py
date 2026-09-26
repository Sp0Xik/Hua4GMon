"""
pytest conftest: гарантирует, что main.py из корня репо импортируется,
независимо от того, откуда запущен pytest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import core  # noqa: E402


@pytest.fixture(autouse=True)
def _russian_interface():
    """Каждый тест заканчивается русским интерфейсом, даже если упал
    посреди проверки английского."""
    yield
    core.set_language("ru")
