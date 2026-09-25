"""Runtime-хук PyInstaller для заставки .exe (packaging/windows.spec).

Выполняется первым, ещё до импорта программы. Пока .exe распаковывается,
загрузчик пишет на заставке имена файлов; после распаковки там оставалось
имя последнего файла (например, «zlib1»), хотя шла уже загрузка самой
программы. Хук сразу сообщает об этом этапе. Дальше состояние обновляет
и закрывает заставку main.py (splash_status, close_splash).

RuntimeError/OSError — загрузчик не смог показать заставку (или её
отключили переменной PYINSTALLER_SUPPRESS_SPLASH_SCREEN): запуск
продолжается без неё. ImportError — сборка без модуля pyi_splash.
"""
import contextlib

with contextlib.suppress(ImportError, RuntimeError, OSError):
    import pyi_splash

    pyi_splash.update_text("Загрузка программы…")
