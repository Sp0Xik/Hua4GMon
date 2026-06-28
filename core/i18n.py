"""
Простая система локализации для Hua4GMon.

Подход «русский-как-ключ»:
    * исходные строки в коде остаются на русском и используются как ключи;
    * для английского хранится словарь RU → EN;
    * если перевода нет — возвращается сам ключ (русский), ничего не падает.

Это позволяет добавить второй язык в уже существующий русский UI с
минимальным риском: достаточно обернуть строку в t("..."), а её перевод
добавить в EN ниже. Отсутствие перевода не ломает интерфейс.

Модуль НЕ зависит от Tkinter/Kivy — пригоден и для Windows, и для Android.

Использование:
    from core.i18n import t, set_language, current_language

    set_language("en")
    label = t("Подключиться")        # -> "Connect"

Строки с подстановкой переводятся как шаблоны:
    t("Идеально ({pct}%) — 4K/онлайн-игры").format(pct=90)
"""
from __future__ import annotations

from typing import Dict, List

# Поддерживаемые языки: код → человекочитаемое имя (для меню выбора).
LANGUAGES: Dict[str, str] = {
    "ru": "Русский",
    "en": "English",
}

_DEFAULT_LANG = "ru"
_current_lang = _DEFAULT_LANG

# RU → EN. Ключ — исходная русская строка (ровно как в коде).
# Если строки здесь нет, t() вернёт ключ (т.е. русский вариант).
EN: Dict[str, str] = {
    # --- Вкладки ---
    "⚙️ Подключение": "⚙️ Connection",
    "📈 Монитор": "📈 Monitor",
    "🎛️ Сеть": "🎛️ Network",
    "🗼 Вышка": "🗼 Tower",
    "📊 Состояние": "📊 Status",
    "🛡 Белые списки (РФ)": "🛡 Whitelist (RU)",

    # --- Верхняя панель ---
    "Отключено": "Disconnected",
    "Подключено": "Connected",
    "Подключение...": "Connecting...",
    "Ошибка": "Error",
    "Поверх окон": "Always on top",
    "Язык:": "Language:",
    "Портативный монитор LTE Huawei": "Portable Huawei LTE monitor",
    "Тестовый режим (без модема)": "Test mode (no modem)",
    "Тестовый режим": "Test mode",
    # Чистые подписи без эмодзи (для Android: эмодзи не рендерятся)
    "Подключиться": "Connect",
    "Отключиться": "Disconnect",
    "Сеть": "Network",
    "Перезагрузить роутер": "Reboot router",
    "Проверить сейчас": "Check now",
    "Белые списки (РФ)": "Whitelist (RU)",
    "Информация": "Information",
    "Инфо": "Info",
    "Подсказка": "Help",
    "Состояние": "Status",
    "Вышка": "Tower",
    "Назад": "Back",
    "Качество связи": "Link quality",
    "ТЕСТОВЫЙ РЕЖИМ — демо-данные": "TEST MODE — demo data",
    "ДЕМО": "DEMO",
    "Во весь экран": "Fullscreen",
    "Модуляция DL": "Modulation DL",
    "Модуляция UL": "Modulation UL",
    "Модуляция DL / UL": "Modulation DL / UL",
    "Ширина канала": "Channel width",
    "TAC (зона)": "TAC (area)",
    "Мощность передатчика": "TX power",
    "Режим MIMO": "MIMO mode",
    "Трафик за месяц (↓/↑)": "Monthly traffic (↓/↑)",
    "Операции с роутером недоступны в тестовом режиме.":
        "Router operations are unavailable in test mode.",
    "← Назад": "← Back",

    # --- Вкладка Подключение ---
    "Параметры роутера": "Router settings",
    "IP адрес:": "IP address:",
    "Пароль:": "Password:",
    "Опрос (сек):": "Polling (sec):",
    "Авто-переподключение при обрыве": "Auto-reconnect on drop",
    "🚀 Подключиться": "🚀 Connect",
    "⏹ Отключиться": "⏹ Disconnect",
    "Подключение и частые ошибки": "Connection & common errors",
    (
        "IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 "
        "или 192.168.3.1). Логин: admin, пароль — на наклейке роутера.\n"
        "\n"
        "Частые ошибки и что делать:\n"
        "• 401 Unauthorized — неверный пароль, либо в веб-морду уже "
        "вошли с другого устройства. Закройте веб-интерфейс роутера "
        "и проверьте пароль.\n"
        "• 108003 / 108006 — превышено число сессий или уже выполнен "
        "вход. Перезагрузите роутер или подождите 1–2 минуты.\n"
        "• 100002 / 100003 — функция не поддерживается этой моделью "
        "или прошивкой. Часть возможностей будет недоступна — это "
        "нормально.\n"
        "• 125002 / 125003 — устарел токен сессии. Переподключитесь.\n"
        "• Таймаут / нет ответа — проверьте, что ноутбук подключён "
        "к Wi-Fi или USB именно этого роутера и IP введён верно."
    ): (
        "Default IP: 192.168.8.1 (for B315/B525 — 192.168.1.1 "
        "or 192.168.3.1). Login: admin, password is on the router label.\n"
        "\n"
        "Common errors and fixes:\n"
        "• 401 Unauthorized — wrong password, or the web UI is already "
        "open on another device. Close the router web interface and "
        "check the password.\n"
        "• 108003 / 108006 — too many sessions or already logged in. "
        "Reboot the router or wait 1–2 minutes.\n"
        "• 100002 / 100003 — feature not supported by this model or "
        "firmware. Some functions will be unavailable — this is normal.\n"
        "• 125002 / 125003 — session token expired. Reconnect.\n"
        "• Timeout / no response — make sure the laptop is connected to "
        "the Wi-Fi or USB of this exact router and the IP is correct."
    ),

    # --- Вкладка Монитор ---
    "Общее качество связи": "Overall link quality",
    "Подключитесь к роутеру": "Connect to the router",
    "Нет данных": "No data",
    "Н/Д": "N/A",
    "Пик: -": "Peak: -",
    "Пик: {v}": "Peak: {v}",
    "Тенденция RSRP (поворачивайте антенну)":
        "RSRP trend (rotate the antenna)",
    "Накапливаю данные...": "Collecting data...",
    "Джиттер: -": "Jitter: -",
    "🔊 Аудио-помощник": "🔊 Audio assistant",
    "🔊 Аудио (ОС не поддерживается)": "🔊 Audio (OS not supported)",
    "🖥 Крышный режим": "🖥 Rooftop mode",
    "График:": "Chart:",
    "Сбросить пики": "Reset peaks",
    "💾 Экспорт CSV": "💾 Export CSV",
    "[ESC] для выхода": "[ESC] to exit",

    # Направление сигнала
    "Сигнал улучшается — продолжайте в том же направлении":
        "Signal improving — keep turning that way",
    "Сигнал ухудшается — поверните обратно":
        "Signal getting worse — turn back",
    "Сигнал стабилен — зафиксируйте антенну":
        "Signal stable — fix the antenna",

    # Здоровье связи (шаблоны с {pct})
    "Идеально ({pct}%) — 4K/онлайн-игры":
        "Perfect ({pct}%) — 4K/online gaming",
    "Хорошо ({pct}%) — стабильный FullHD":
        "Good ({pct}%) — stable FullHD",
    "Умеренно ({pct}%) — крутите антенну":
        "Moderate ({pct}%) — adjust the antenna",
    "Плохо ({pct}%) — будет рваться!":
        "Poor ({pct}%) — will keep dropping!",

    # Джиттер (шаблон)
    "Джиттер: {j:.1f} dB": "Jitter: {j:.1f} dB",

    # --- Вкладка Сеть ---
    "Фиксация частот (Band Lock)": "Band Lock",
    (
        "ВНИМАНИЕ: фиксация диапазона может уменьшить покрытие. "
        "Применяйте, чтобы привязаться к лучшей вышке — сначала "
        "определите рабочий band на вкладке «Вышка»."
    ): (
        "WARNING: locking a band may reduce coverage. Use it to pin to "
        "the best cell — first identify the working band on the «Tower» tab."
    ),
    "Применить Band Lock": "Apply Band Lock",
    "Сбросить в AUTO": "Reset to AUTO",
    "Переключение антенн": "Antenna switching",
    "Режим:": "Mode:",
    "Применить": "Apply",
    "Управление роутером": "Router management",
    (
        "Перезагрузка иногда нужна после Band Lock, переключения "
        "антенн или при «зависании» сетевой части. Через 1–2 минуты "
        "переподключитесь вручную."
    ): (
        "A reboot is sometimes needed after Band Lock, antenna switching "
        "or when the network stack hangs. Reconnect manually after "
        "1–2 minutes."
    ),
    "🔄 Перезагрузить роутер": "🔄 Reboot router",

    # Антенна (режимы)
    "Авто": "Auto",
    "Внутренняя": "Internal",
    "Внешняя": "External",
    "Смешанная": "Mixed",

    # --- Вкладка Вышка ---
    "Информация о станции": "Cell info",
    "Оператор (PLMN)": "Operator (PLMN)",
    "Рабочий Band (LTE)": "Working band (LTE)",
    "EARFCN (канал DL)": "EARFCN (DL channel)",
    "Агрегация (CA)": "Aggregation (CA)",
    "Ширина канала (DL)": "Channel width (DL)",
    "Сектор антенны (PCI)": "Antenna sector (PCI)",
    "eNodeB (Вышка)": "eNodeB (Tower)",
    "Cell (Локальный сектор)": "Cell (local sector)",
    "SIM / Устройство": "SIM / Device",
    "IMEI (роутер)": "IMEI (router)",
    "IMSI (SIM)": "IMSI (SIM)",
    "ICCID (SIM-карта)": "ICCID (SIM card)",
    "Номер телефона": "Phone number",
    "Серийный номер": "Serial number",
    "Модель": "Model",
    "Прошивка": "Firmware",
    "🗺 Открыть на CellMapper": "🗺 Open in CellMapper",
    "Неизвестный оператор": "Unknown operator",
    "Активна": "Active",
    "Нет (Single)": "No (Single)",

    # --- Вкладка Состояние ---
    "Мониторинг железа и трафика": "Hardware & traffic monitor",
    "Время сессии": "Session time",
    "Температура чипа": "Chip temperature",
    "Скорость (Download)": "Speed (Download)",
    "Скорость (Upload)": "Speed (Upload)",
    "Скачано за сессию": "Downloaded this session",
    "Отдано за сессию": "Uploaded this session",
    "RSRP мин / макс": "RSRP min / max",
    "SINR мин / макс": "SINR min / max",

    # --- Вкладка Белые списки ---
    "Перед проверкой": "Before testing",
    (
        "⚠ Ноутбук должен быть подключён к Wi-Fi или USB именно этого "
        "роутера — иначе тест измерит чужой канал.\n"
        "• Применимо только для РФ."
    ): (
        "⚠ The laptop must be connected to the Wi-Fi or USB of this exact "
        "router — otherwise the test measures a different link.\n"
        "• Applies to Russia only."
    ),
    "🔍 Проверить сейчас": "🔍 Check now",
    "Проверка…": "Checking…",
    "Подождите 1–3 секунды.": "Please wait 1–3 seconds.",
    "Вердикт": "Verdict",
    "Не проверялось": "Not tested",
    "✅ В белых списках": "✅ In whitelist",
    "⚪ Нейтральные": "⚪ Neutral",
    "не проверено": "not tested",

    # --- messagebox: заголовки и тексты ---
    "Успех": "Success",
    "Внимание": "Warning",
    "Подтверждение": "Confirm",
    "Ошибка подключения": "Connection error",
    "Экспорт": "Export",
    "Перезагрузка": "Reboot",
    "Сначала подключитесь к роутеру.": "Connect to the router first.",
    "Выберите хотя бы один диапазон!": "Select at least one band!",
    "Неизвестный режим антенны.": "Unknown antenna mode.",
    "Сеть сброшена в AUTO.": "Network reset to AUTO.",
    "Неверный IP-адрес: {ip}\nПример: 192.168.8.1":
        "Invalid IP address: {ip}\nExample: 192.168.8.1",
    "Связь с роутером не удалась:\n\n{err}":
        "Failed to reach the router:\n\n{err}",
    "Band Lock применён (mask: {mask}).":
        "Band Lock applied (mask: {mask}).",
    "Роутер отклонил команду:\n{err}":
        "The router rejected the command:\n{err}",
    "Тип антенны изменён: {mode}": "Antenna type changed: {mode}",
    "Перезагрузить роутер?\n\nСоединение с интернетом прервётся на 1–2 "
    "минуты. После загрузки переподключитесь вручную.":
        "Reboot the router?\n\nInternet will drop for 1–2 minutes. "
        "Reconnect manually after it boots.",
    "Команда отправлена. Роутер вернётся через 1–2 минуты.":
        "Command sent. The router will be back in 1–2 minutes.",
    "Не удалось перезагрузить:\n{err}": "Failed to reboot:\n{err}",
    "Недостаточно данных о вышке (нужны PLMN и eNodeB).":
        "Not enough cell data (PLMN and eNodeB required).",
    "Не открыть браузер: {e}": "Cannot open browser: {e}",
    "Лог сессии пуст. Подключитесь и подождите, пока соберутся данные.":
        "Session log is empty. Connect and wait for data to accumulate.",
    "Сохранено {n} записей в:\n{path}":
        "Saved {n} records to:\n{path}",
    "Не удалось записать файл: {e}": "Failed to write file: {e}",
    "Таймаут API...": "API timeout...",
    "Переподключение через {d:.0f}с...": "Reconnecting in {d:.0f}s...",

    # Вердикты белых списков (заголовки)
    "Белые списки ВЫКЛЮЧЕНЫ": "Whitelist OFF",
    "⚠ Белые списки ВКЛЮЧЕНЫ": "⚠ Whitelist ON",
    "Аномалия": "Anomaly",
    "Нет интернета": "No internet",

    # Метки уровней сигнала (из SIGNAL_THRESHOLDS)
    "Отличный": "Excellent",
    "Хороший": "Good",
    "Средний": "Fair",
    "Плохой": "Poor",
    "Идеальный": "Ideal",
    "Шумный": "Noisy",
    "Критичный": "Critical",
    "Сильный": "Strong",
    "Нормальный": "Normal",
    "Слабый": "Weak",
    "Очень слабый": "Very weak",
    "Стабильный": "Stable",
    "Потери": "Losses",
    "Высокие потери": "High losses",

    # Обозначения бэндов (для Band Lock)
    "B1 (2100 МГц)": "B1 (2100 MHz)",
    "B3 (1800 МГц)": "B3 (1800 MHz)",
    "B5 (850 МГц)": "B5 (850 MHz)",
    "B7 (2600 МГц)": "B7 (2600 MHz)",
    "B8 (900 МГц)": "B8 (900 MHz)",
    "B20 (800 МГц)": "B20 (800 MHz)",
    "B38 (TDD 2600)": "B38 (TDD 2600)",
    "B40 (TDD 2300)": "B40 (TDD 2300)",
    "B41 (TDD 2500)": "B41 (TDD 2500)",

    # Android-подсказки (Сеть)
    ("ВНИМАНИЕ: фиксация диапазона может уменьшить покрытие. "
     "Применяйте, чтобы привязаться к лучшей вышке — сначала "
     "определите рабочий band на экране монитора."): (
        "WARNING: locking a band may reduce coverage. Use it to bind to "
        "the best tower — first identify the working band on the monitor "
        "screen."),
    ("⚠ Телефон должен быть подключён к Wi-Fi именно этого "
     "роутера — иначе тест измерит чужой канал. Применимо только "
     "для РФ."): (
        "⚠ The phone must be connected to the Wi-Fi of this exact router "
        "— otherwise the test measures a different link. Applies to "
        "Russia only."),

    # Детали вердикта белых списков (шаблоны с подстановкой счётчиков)
    ("Обычный режим — открыт весь интернет "
     "(белых: {w}/{wt}, нейтральных: {n}/{nt})."): (
        "Normal mode — full internet is open "
        "(whitelisted: {w}/{wt}, neutral: {n}/{nt})."),
    ("Сейчас на БС работают ТОЛЬКО разрешённые сайты "
     "(белых: {w}/{wt}, нейтральных: 0/{nt}). "
     "Обычные сайты заблокированы оператором."): (
        "Only allowed sites work on this cell right now "
        "(whitelisted: {w}/{wt}, neutral: 0/{nt}). "
        "Regular sites are blocked by the operator."),
    ("Нейтральные сайты доступны, но «белые» не отвечают. "
     "Скорее всего, вы вышли в интернет не через 4G "
     "(другой Wi-Fi, провод, VPN). Подключитесь к Wi-Fi роутера "
     "и повторите."): (
        "Neutral sites are reachable but whitelisted ones are not. "
        "You are likely online not via 4G (another Wi-Fi, cable, VPN). "
        "Connect to the router's Wi-Fi and retry."),
    ("Ни одна цель не отвечает. Либо у роутера нет связи с БС, "
     "либо проблема с DNS/маршрутом. Проверьте RSRP и трафик."): (
        "No target responds. Either the router has no cell link, or "
        "there is a DNS/route problem. Check RSRP and traffic."),

    # График (Windows)
    "последние {n} точек": "last {n} points",
}


def set_language(lang: str) -> None:
    """Устанавливает текущий язык ('ru' или 'en'). Неизвестный — игнор."""
    global _current_lang
    if lang in LANGUAGES:
        _current_lang = lang


def current_language() -> str:
    """Возвращает код текущего языка."""
    return _current_lang


def available_languages() -> List[str]:
    """Список кодов поддерживаемых языков."""
    return list(LANGUAGES.keys())


def t(text: str) -> str:
    """Переводит строку на текущий язык.

    Русский — возвращает ключ как есть. Английский — ищет в EN,
    при отсутствии возвращает ключ (русский) как fallback.
    """
    if _current_lang == "ru":
        return text
    return EN.get(text, text)
