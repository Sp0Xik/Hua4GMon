[app]

# Название приложения и пакета
title = Hua4GMon
package.name = hua4gmon
package.domain = io.github.sp0xik

# Исходники. Точка входа — android_main.py (переименуется в main.py
# внутри сборки через main.py-симлинк ниже не делаем — указываем явно).
source.dir = .
source.include_exts = py,png,jpg,svg,kv,atlas
# Включаем пакет core/ целиком и оба entry-point (на случай отладки).
source.include_patterns = core/*.py,assets/*.png

# Версия
version = 1.2

# Точка входа: Buildozer ищет main.py. Поскольку наш десктоп — main.py,
# а Android — android_main.py, указываем явно через p4a entrypoint.
# Простой и надёжный путь: держим копию android_main.py как main.py при
# сборке. См. README/CI; здесь указываем имя:
#   (p4a берёт файл, заданный в --private; стандартно — main.py)
# Чтобы не дублировать, CI копирует android_main.py -> main.py перед сборкой
# в отдельной папке. Локально можно сделать то же вручную.

# ЗАВИСИМОСТИ.
# kivy — UI. huawei-lte-api тянет requests/xmltodict/pycryptodomex.
#   * requests, xmltodict — собираются p4a без проблем.
#   * pycryptodomex — C-расширение; у python-for-android есть рецепт
#     pycryptodome. pycryptodomex (неймспейс Cryptodome) обычно тоже
#     собирается, но если первая сборка упадёт на нём — это первое,
#     что нужно проверять (см. заметку в README).
# certifi/urllib3/idna/charset-normalizer — транзитивные для requests.
requirements = python3,kivy==2.3.0,huawei-lte-api,requests,urllib3,certifi,idna,charset-normalizer,xmltodict,pycryptodomex

# Ориентация и полноэкранность
orientation = portrait
fullscreen = 0

# Иконка и заставка (сгенерированы в assets/)
icon.filename = %(source.dir)s/assets/icon-512.png
presplash.filename = %(source.dir)s/assets/icon-512.png

# Разрешения: только сеть. Никаких лишних — приложение лишь опрашивает
# роутер по HTTP в локальной сети.
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE

# Разрешаем cleartext HTTP: роутер отвечает по http://192.168.8.1,
# без этого Android 9+ заблокирует незашифрованные запросы.
android.allow_backup = True
android.usesCleartextTraffic = True

# Версии API. 33 (Android 13) — разумный современный таргет.
android.api = 33
android.minapi = 24
android.archs = arm64-v8a,armeabi-v7a

# Не показывать логи p4a в release; для отладки можно поднять.
log_level = 2

[buildozer]
warn_on_root = 1
