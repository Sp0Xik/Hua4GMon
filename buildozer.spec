[app]

# Название приложения и пакета
title = Hua4GMon
package.name = hua4gmon
package.domain = io.github.sp0xik

# Исходники. CI собирает APK в отдельной папке build-android/, где
# android_main.py становится main.py (десктопный main.py — Tkinter).
source.dir = .
source.include_exts = py,png,ttf,txt

# Версия — единственный источник: __version__ в core/__init__.py.
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/core/__init__.py

# ЗАВИСИМОСТИ.
# python-for-android ставит чистые Python-пакеты с --no-deps, поэтому все
# транзитивные зависимости программы перечислены явно и закреплены (те же
# версии, что в requirements.txt — это проверяет тест).
#   * kivy, pycryptodome — рецепты p4a (версию задаёт рецепт закреплённого
#     p4a: Kivy 2.3.0, pycryptodome 3.6.3). Рецепт Kivy дополнительно
#     ставит chardet без закреплённой версии — requests принимает 3.0.2–7.x.
#   * huawei-lte-api требует pycryptodomex (неймспейс Cryptodome), но у
#     p4a нет его рецепта. Ставим pycryptodome (неймспейс Crypto), а
#     android_main.py перенаправляет Cryptodome.* -> Crypto.* на старте.
requirements = python3,kivy,huawei-lte-api==2.0.1,requests==2.34.2,urllib3==2.8.0,certifi==2026.7.22,idna==3.20,charset-normalizer==3.5.1,xmltodict==1.0.4,pycryptodome

# Ориентация и полноэкранность
orientation = portrait
fullscreen = 0

# Иконка и заставка (сгенерированы в assets/)
icon.filename = %(source.dir)s/assets/icon-512.png
presplash.filename = %(source.dir)s/assets/icon-512.png

# Разрешения: только сеть. Экран во время мониторинга держится флагом
# окна FLAG_KEEP_SCREEN_ON (android_main.set_keep_screen_on) — ему не
# нужно разрешение WAKE_LOCK.
android.permissions = INTERNET

# HTTP к роутеру идёт через сокеты Python, на которые политика cleartext
# Android (Network Security Config) не распространяется, поэтому
# отдельная настройка cleartext не требуется.
# Программа ничего не сохраняет — резервной копии Android копировать нечего.
android.allow_backup = False

# Версии API. targetSdk 33 (Android 13): приложение работает на Android
# 7–16. Переход на 35 включит принудительный edge-to-edge (Android 15+),
# под который нужен отдельный отступ интерфейса от системных панелей.
android.api = 33
android.minapi = 24
# NDK 25b выравнивает библиотеки по 4 КБ: на устройствах со страницами
# памяти 16 КБ (часть Android 15+) APK может не запуститься — ограничение
# закреплённого стека, см. HARDWARE_VALIDATION.md.
android.ndk = 25b
android.archs = arm64-v8a,armeabi-v7a

# Release — APK (по умолчанию buildozer делает AAB для Google Play; для
# раздачи через GitHub Releases нужен APK). Подпись — переменными
# окружения P4A_RELEASE_KEYSTORE/_KEYSTORE_PASSWD/_KEYALIAS/_KEYALIAS_PASSWD
# (см. .github/workflows/build-android.yml).
android.release_artifact = apk

# КРИТИЧНО: фиксируем стабильный python-for-android.
# Свежий p4a (master) по умолчанию тянет Python 3.14 + NDK r28c, для
# которых ещё нет рабочего pyjnius. Релиз v2024.01.21 использует
# Python 3.11 и собирает pyjnius из рецепта. Совместим с buildozer 1.5.0,
# Cython 0.29.36 и NDK 25b (заданы в CI). Проверка buildozer 1.6.0 с этим
# же p4a — отдельная задача CI «canary».
p4a.branch = v2024.01.21

# 2 — подробный лог buildozer (CI и так запускает с -v).
log_level = 2

[buildozer]
warn_on_root = 1
