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
    t("Отличный сигнал ({pct}%)").format(pct=90)
"""
from __future__ import annotations

# Поддерживаемые языки: код → человекочитаемое имя (для меню выбора).
LANGUAGES: dict[str, str] = {
    "ru": "Русский",
    "en": "English",
}

_DEFAULT_LANG = "ru"
_current_lang = _DEFAULT_LANG

# RU → EN. Ключ — исходная русская строка (ровно как в коде).
# Если строки здесь нет, t() вернёт ключ (т.е. русский вариант).
EN: dict[str, str] = {
    # --- Ядро (core): статусы сигнала, ошибки, RF-подсказки, Band Lock, белые списки ---
    "1 поток":
        "1 stream",
    "2 потока":
        "2 streams",
    "DNS не отвечает":
        "DNS not responding",
    "TCP есть, TLS оборван ({reason})":
        "TCP ok, TLS dropped ({reason})",
    "Авто":
        "Auto",
    "Агрегация пропадёт: не выбраны {bands}.":
        "Carrier aggregation will stop: {bands} not selected.",
    "Аномалия":
        "Anomaly",
    "Белые списки ВЫКЛЮЧЕНЫ":
        "Whitelist OFF",
    "Внешняя":
        "External",
    "Внутренняя":
        "Internal",
    "Загрузка/помехи":
        "Load/interference",
    "Идеальный":
        "Ideal",
    "Критичный":
        "Critical",
    ("Модем передаёт почти на максимуме мощности: отдача ограничена — нужна "
     "антенна точнее или выше."):
        ("The modem transmits near maximum power: upload is limited — aim the "
         "antenna better or mount it higher."),
    "Н/Д":
        "N/A",
    "Неверное имя пользователя.":
        "Wrong username.",
    "Неверный логин или пароль. Пароль — на наклейке роутера.":
        "Wrong username or password. The password is on the router's label.",
    "Неверный пароль.":
        "Wrong password.",
    ("Нейтральные сайты доступны, но «белые» не отвечают. Скорее всего, вы "
     "вышли в интернет не через 4G (другой Wi-Fi, провод, VPN). "
     "Подключитесь к Wi-Fi роутера и повторите."):
        ("Neutral sites are reachable but whitelisted ones are not. You are "
         "likely online not via 4G (another Wi-Fi, cable, VPN). Connect to the "
         "router's Wi-Fi and retry."),
    "Нет данных":
        "No data",
    "Нет интернета":
        "No internet",
    "Нет сети":
        "No service",
    ("Ни одна цель не отвечает. Либо у роутера нет связи с БС, либо "
     "проблема с DNS/маршрутом. Проверьте RSRP и трафик."):
        ("No target responds. Either the router has no cell link, or there is a "
         "DNS/route problem. Check RSRP and traffic."),
    "Нормальный":
        "Normal",
    ("Обычный режим — открыт весь интернет (белых: {w}/{wt}, нейтральных: "
     "{n}/{nt})."):
        ("Normal mode — full internet is open (whitelisted: {w}/{wt}, neutral: "
         "{n}/{nt})."),
    "Отличный":
        "Excellent",
    "Отличный сигнал ({pct}%)":
        "Excellent signal ({pct}%)",
    "Очень слабый":
        "Very weak",
    "Плохой":
        "Poor",
    "Потоки MIMO неравны: проверьте разъёмы и кабели обоих портов.":
        "MIMO streams are unequal: check connectors and cables of both ports.",
    "Похоже на фильтрацию по SNI: TCP проходит, TLS — нет.":
        "Looks like SNI filtering: TCP passes, TLS does not.",
    ("Работает один поток MIMO: проверьте второй кабель и поляризацию "
     "второго порта."):
        ("Only one MIMO stream is active: check the second cable and the second "
         "port's polarization."),
    ("Разрешённые сайты отвечают, а нейтральные — нет (белых: {w}/{wt}, "
     "нейтральных: 0/{nt}). Похоже на режим белых списков оператора. Для "
     "точности проверьте открытие обычного сайта в браузере."):
        ("Allowed sites respond but neutral ones do not (whitelisted: {w}/{wt}, "
         "neutral: 0/{nt}). Looks like the operator's whitelist mode. To be "
         "sure, try opening a regular site in a browser."),
    "Роутер занят — повторите через несколько секунд.":
        "Router is busy — retry in a few seconds.",
    "Роутер не отвечает: проверьте подключение к его Wi-Fi/USB и IP-адрес.":
        "Router is not responding: check the Wi-Fi/USB link and the IP address.",
    "Роутер требует сменить пароль — сделайте это в веб-интерфейсе.":
        "The router requires a password change — do it in the web interface.",
    "Сессия истекла — нужно войти заново.":
        "Session expired — log in again.",
    "Сессия устарела — нужно войти заново.":
        "Session is stale — log in again.",
    "Сигнал «гуляет»: проверьте крепление антенны и переотражения.":
        "The signal fluctuates: check the antenna mount and reflections.",
    ("Сигнал чистый, но слабый: поднимите антенну выше, возьмите антенну с "
     "большим усилением или укоротите кабель."):
        ("Signal is clean but weak: mount the antenna higher, use a higher-gain "
         "antenna or a shorter cable."),
    "Сильный":
        "Strong",
    "Слабый":
        "Weak",
    "Слабый сигнал — ищите лучше ({pct}%)":
        "Weak signal — look for a better spot ({pct}%)",
    ("Слишком много неудачных попыток входа — роутер временно заблокировал "
     "вход. Подождите несколько минут и проверьте пароль."):
        ("Too many failed login attempts — the router has temporarily blocked "
         "logins. Wait a few minutes and check the password."),
    "Смешанная":
        "Mixed",
    "Сота загружена: сравните с соседней сотой или другим бэндом.":
        "The cell is loaded: compare with a neighbor cell or another band.",
    "Средний":
        "Fair",
    "Средний сигнал — крутите антенну ({pct}%)":
        "Fair signal — adjust the antenna ({pct}%)",
    ("Текущий бэнд B{band} исключён — связь пропадёт до перерегистрации "
     "модема."):
        ("The current band B{band} is excluded — the link will drop until the "
         "modem re-registers."),
    ("Уже выполнен вход с другого устройства — закройте веб-интерфейс "
     "роутера и повторите."):
        ("Already logged in from another device — close the router's web "
         "interface and retry."),
    ("Уровень есть, но много помех: сместите азимут, попробуйте другой "
     "сектор или проверьте поляризацию."):
        ("Level is fine but interference is high: shift the azimuth, try "
         "another sector or check polarization."),
    "Функция не поддерживается этой моделью или прошивкой.":
        "Not supported by this model or firmware.",
    "Хороший":
        "Good",
    "Хороший сигнал ({pct}%)":
        "Good signal ({pct}%)",
    "Шумный":
        "Noisy",
    "на пределе":
        "at the limit",
    "норма":
        "normal",
    "ошибка ({code})":
        "error ({code})",
    "повышенная":
        "elevated",
    "потоки неравны":
        "streams unequal",
    "соединение отклонено":
        "connection refused",
    "соединение сброшено":
        "connection reset",
    "таймаут":
        "timeout",
    "⚠ Вероятна фильтрация (белые списки)":
        "⚠ Filtering likely (whitelist)",

    # --- Общие строки Windows и Android ---
    "Band Lock применён: {bands}.":
        "Band Lock applied: {bands}.",
    "CQI (потоки)":
        "CQI (streams)",
    "Cell (Локальный сектор)":
        "Cell (local sector)",
    "EARFCN (канал DL)":
        "EARFCN (DL channel)",
    "ICCID (SIM-карта)":
        "ICCID (SIM card)",
    "IMEI (роутер)":
        "IMEI (router)",
    "IP адрес:":
        "IP address:",
    "RSRP мин / макс":
        "RSRP min / max",
    "SIM / Устройство":
        "SIM / Device",
    "SINR мин / макс":
        "SINR min / max",
    "TAC (зона)":
        "TAC (area)",
    "eNodeB (Вышка)":
        "eNodeB (Tower)",
    "{at} смена соты: {old} → {new}":
        "{at} cell change: {old} → {new}",
    "Агрегация (CA)":
        "Aggregation (CA)",
    "Активна":
        "Active",
    "ВЫКЛЮЧЕНЫ":
        "OFF",
    "Включаю все бэнды…":
        "Enabling all bands…",
    "Включены все бэнды (AUTO).":
        "All bands enabled (AUTO).",
    "Восстанавливаю настройки…":
        "Restoring settings…",
    "Восстановлены настройки, прочитанные при подключении.":
        "Settings read at connection time have been restored.",
    "Время сессии":
        "Session time",
    "Выберите хотя бы один диапазон!":
        "Select at least one band!",
    "Зафиксировать бэнды: {bands}?":
        "Lock bands: {bands}?",
    "Информация о станции":
        "Cell info",
    "Исходные настройки не прочитаны — роутер не отдал net-mode.":
        "Original settings were not read — the router did not return net-mode.",
    "Ищу роутер…":
        "Looking for the router…",
    ("Команда отправлена, но роутер вернул другие настройки — проверьте "
     "строку «Сейчас на модеме»."):
        ("Command sent, but the router reports different settings — check the "
         "“Now on the modem” line."),
    "Лучшие соты за сессию":
        "Best cells this session",
    "МГц":
        "MHz",
    "Мобильные данные":
        "Mobile data",
    "Мобильные данные на роутере выключены.":
        "Mobile data is turned off on the router.",
    "Модель":
        "Model",
    "Модем перерегистрируется в сети (до ~30 с).":
        "The modem is re-registering on the network (up to ~30 s).",
    "Модуляция DL / UL":
        "Modulation DL / UL",
    "Мощность передатчика":
        "TX power",
    "Найден роутер {model} на {ip}":
        "Found router {model} at {ip}",
    "Накапливаю данные...":
        "Collecting data...",
    ("Не удалось включить мобильные данные — включите их в веб-интерфейсе "
     "роутера."):
        ("Could not turn mobile data back on — enable it in the router's web "
         "interface."),
    ("Неверный IP-адрес: {ip}\n"
     "Пример: 192.168.8.1"):
        ("Invalid IP address: {ip}\n"
         "Example: 192.168.8.1"),
    "Неизвестный оператор":
        "Unknown operator",
    "Неизвестный режим антенны.":
        "Unknown antenna mode.",
    "Нет":
        "No",
    "Нет связи с роутером — переподключаюсь…":
        "No link to the router — reconnecting…",
    "Номер телефона":
        "Phone number",
    "Оператор (PLMN)":
        "Operator (PLMN)",
    "Отдано за сессию":
        "Uploaded this session",
    "Отметить текущие":
        "Mark current",
    "Отмечены текущие бэнды: {bands}":
        "Current bands marked: {bands}",
    "Отправляю команду перезагрузки…":
        "Sending the reboot command…",
    "Ошибка":
        "Error",
    "Пароль:":
        "Password:",
    ("Перезагрузить роутер?\n"
     "\n"
     "Соединение с интернетом прервётся на 1–2 минуты. Программа "
     "переподключится автоматически."):
        ("Reboot the router?\n"
         "\n"
         "The internet connection will drop for 1–2 minutes. The app will "
         "reconnect automatically."),
    "Переключаю антенну…":
        "Switching the antenna…",
    "Переключение антенн":
        "Antenna switching",
    "Переподключаю связь…":
        "Reconnecting the link…",
    ("Переподключить мобильную связь?\n"
     "\n"
     "Интернет пропадёт примерно на 5–10 секунд."):
        ("Reconnect the mobile link?\n"
         "\n"
         "The internet will drop for about 5–10 seconds."),
    "Пик: {v} (Δ {d})":
        "Peak: {v} (Δ {d})",
    "Подключение и частые ошибки":
        "Connection & common errors",
    "Подключение...":
        "Connecting...",
    "Подождите 1–3 секунды.":
        "Please wait 1–3 seconds.",
    "Подтверждение":
        "Confirm",
    "Применить":
        "Apply",
    "Применить Band Lock":
        "Apply Band Lock",
    "Применяю Band Lock…":
        "Applying Band Lock…",
    "Проверка…":
        "Checking…",
    "Прошивка":
        "Firmware",
    "Рабочий Band (LTE)":
        "Working band (LTE)",
    "Режим MIMO":
        "MIMO mode",
    "Режим сети будет «только 4G».":
        "Network mode will be “4G only”.",
    "Роутер не найден. Проверьте подключение к его Wi-Fi/USB.":
        "Router not found. Check the connection to its Wi-Fi/USB.",
    "Роутер отклонил команду: {err}":
        "The router rejected the command: {err}",
    "Роутер перезагружается — переподключусь автоматически.":
        "The router is rebooting — I will reconnect automatically.",
    "Роутер принял команду, но сообщает другой режим антенны.":
        "The router accepted the command but reports a different antenna mode.",
    "Связь переподключена.":
        "Link reconnected.",
    "Сейчас на модеме: AUTO (все бэнды)":
        "Now on the modem: AUTO (all bands)",
    "Сейчас на модеме: {bands}":
        "Now on the modem: {bands}",
    "Сейчас на модеме: не прочитано":
        "Now on the modem: not read",
    "Сектор антенны (PCI)":
        "Antenna sector (PCI)",
    "Серийный номер":
        "Serial number",
    "Скачано за сессию":
        "Downloaded this session",
    "Скорость (Download)":
        "Speed (Download)",
    "Скорость (Upload)":
        "Speed (Upload)",
    "Сначала подключитесь к роутеру.":
        "Connect to the router first.",
    "Собираю диагностику…":
        "Collecting diagnostics…",
    "Текущий бэнд ещё не определён.":
        "The current band is not known yet.",
    "Температура чипа":
        "Chip temperature",
    "Технология":
        "Technology",
    "Тип антенны изменён: {mode}":
        "Antenna type changed: {mode}",
    "Трафик за месяц (↓/↑)":
        "Monthly traffic (↓/↑)",
    "Управление роутером":
        "Router management",
    "Фиксация частот (Band Lock)":
        "Band Lock",
    "Частота DL":
        "DL frequency",
    "Ширина канала":
        "Channel width",
    "Язык:":
        "Language:",
    "включены":
        "on",
    "нет свежих данных":
        "no fresh data",
    "последние {n} точек":
        "last {n} points",
    "⚠ Данные устарели — нет ответа {s:.0f} с":
        "⚠ Data is stale — no response for {s:.0f} s",
    "⚪ Нейтральные":
        "⚪ Neutral",
    "✅ В белых списках":
        "✅ In whitelist",
    "📶 Переподключить связь":
        "📶 Reconnect link",
    "🔄 Перезагрузить роутер":
        "🔄 Reboot router",
    "🧪 ДЕМО · азимут {a}":
        "🧪 DEMO · azimuth {a}",
    "🧪 Тестовый режим":
        "🧪 Test mode",

    # --- Windows (main.py) ---
    ("IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 или "
     "192.168.3.1) — кнопка «Найти роутер» проверит их сама. Логин: admin, "
     "пароль — на наклейке роутера.\n"
     "\n"
     "Частые ошибки и что делать:\n"
     "• 108006 — неверный логин или пароль.\n"
     "• 108007 — слишком много неудачных попыток: роутер временно "
     "заблокировал вход. Подождите несколько минут.\n"
     "• 108003 — уже выполнен вход с другого устройства. Закройте "
     "веб-интерфейс роутера.\n"
     "• 100002 — функция не поддерживается этой моделью или прошивкой. "
     "Часть возможностей будет недоступна — это нормально.\n"
     "• 100003 / 125002 / 125003 — истекла сессия. Программа войдёт заново "
     "сама.\n"
     "• Нет ответа — проверьте, что компьютер подключён к Wi-Fi или USB "
     "именно этого роутера и IP введён верно.\n"
     "\n"
     "Тестовый режим показывает работу программы без роутера."):
        ("Default IP: 192.168.8.1 (B315/B525: 192.168.1.1 or 192.168.3.1) — the "
         "“Find router” button checks them automatically. Login: admin, the "
         "password is on the router's label.\n"
         "\n"
         "Common errors and what to do:\n"
         "• 108006 — wrong username or password.\n"
         "• 108007 — too many failed attempts: the router has temporarily "
         "blocked logins. Wait a few minutes.\n"
         "• 108003 — already logged in from another device. Close the router's "
         "web interface.\n"
         "• 100002 — not supported by this model or firmware. Some features "
         "will be unavailable — this is normal.\n"
         "• 100003 / 125002 / 125003 — session expired. The app logs in again "
         "by itself.\n"
         "• No response — make sure the computer is connected to this router's "
         "Wi-Fi or USB and the IP is correct.\n"
         "\n"
         "Test mode shows how the app works without a router."),
    "[ESC] или F11 — выход":
        "[ESC] or F11 — exit",
    ("«Переподключить связь» выключает и включает мобильные данные: модем "
     "заново выбирает лучшую соту — быстрее перезагрузки. После "
     "перезагрузки программа переподключится сама."):
        ("“Reconnect link” turns mobile data off and on: the modem picks the "
         "best cell again — faster than a reboot. After a reboot the app "
         "reconnects by itself."),
    "Авто-переподключение при обрыве":
        "Auto-reconnect on drop",
    "Бэнд":
        "Band",
    "Вердикт":
        "Verdict",
    "Внимание":
        "Warning",
    "Все бэнды (AUTO)":
        "All bands (AUTO)",
    "График и стрелка:":
        "Graph and arrow:",
    "Джиттер: -":
        "Jitter: -",
    "Джиттер: {j:.1f} dB":
        "Jitter: {j:.1f} dB",
    "Диагностика сохранена: {path}. Личные номера замаскированы.":
        "Diagnostics saved: {path}. Personal identifiers are masked.",
    "Замеров":
        "Samples",
    "Лог сессии пуст. Подключитесь и подождите, пока соберутся данные.":
        "Session log is empty. Connect and wait for data to accumulate.",
    "Лучший RSRP":
        "Best RSRP",
    "Лучший SINR":
        "Best SINR",
    "Мониторинг железа и трафика":
        "Hardware & traffic monitor",
    "Не открыть браузер: {e}":
        "Cannot open browser: {e}",
    "Не проверялось":
        "Not tested",
    "Не удалось записать файл: {e}":
        "Failed to write file: {e}",
    "Недостаточно данных о вышке (нужны PLMN и eNodeB).":
        "Not enough cell data (PLMN and eNodeB required).",
    "Общее качество связи":
        "Overall link quality",
    "Опрос (сек):":
        "Polling (sec):",
    "Отключено":
        "Disconnected",
    "Параметры роутера":
        "Router settings",
    "Перед проверкой":
        "Before testing",
    "Пик: -":
        "Peak: -",
    "Поверх окон":
        "Always on top",
    "Подключено":
        "Connected",
    "Подключитесь к роутеру":
        "Connect to the router",
    "Режим:":
        "Mode:",
    "Сбросить пики (Ctrl+R)":
        "Reset peaks (Ctrl+R)",
    "Связь с роутером потеряна":
        "Connection to the router lost",
    "Сейчас на модеме: -":
        "Now on the modem: -",
    "Сигнал стабилен — зафиксируйте антенну":
        "Signal stable — fix the antenna",
    "Сигнал улучшается — продолжайте в том же направлении":
        "Signal improving — keep turning that way",
    "Сигнал ухудшается — поверните обратно":
        "Signal getting worse — turn back",
    ("Сохранено {n} записей в:\n"
     "{path}"):
        ("Saved {n} records to:\n"
         "{path}"),
    "Тенденция {param} (поворачивайте антенну)":
        "{param} trend (turn the antenna)",
    ("Фиксация бэндов привязывает модем к выбранным частотам. Список взят "
     "из модема; замеченные в эфире бэнды — первыми. Перед записью "
     "программа читает текущие настройки и меняет только LTE-бэнды; "
     "«Вернуть как было» восстановит настройки, прочитанные при подключении."):
        ("Band locking ties the modem to the selected frequencies. The list "
         "comes from the modem; bands seen on air come first. Before writing, "
         "the app reads the current settings and changes only the LTE bands; "
         "“Restore as before” brings back the settings read at connection time."),
    "Экспорт":
        "Export",
    "не проверено":
        "not tested",
    "пик {v}":
        "peak {v}",
    "↩ Вернуть как было":
        "↩ Restore as before",
    "⏹ Отключиться":
        "⏹ Disconnect",
    "⚙️ Подключение":
        "⚙️ Connection",
    ("⚠ Ноутбук должен быть подключён к Wi-Fi или USB именно этого роутера "
     "— иначе тест измерит чужой канал.\n"
     "• Применимо только для РФ."):
        ("⚠ The laptop must be connected to the Wi-Fi or USB of this exact "
         "router — otherwise the test measures a different link.\n"
         "• Applies to Russia only."),
    "🎛️ Сеть":
        "🎛️ Network",
    "💾 Экспорт CSV":
        "💾 Export CSV",
    "📈 Монитор":
        "📈 Monitor",
    "📊 Состояние":
        "📊 Status",
    "🔊 Аудио (ОС не поддерживается)":
        "🔊 Audio (OS not supported)",
    "🔊 Звук (Ctrl+M)":
        "🔊 Sound (Ctrl+M)",
    "🔍 Проверить сейчас":
        "🔍 Check now",
    "🔎 Найти роутер":
        "🔎 Find router",
    "🖥 Крышный режим (F11)":
        "🖥 Roof mode (F11)",
    "🗺 Открыть на CellMapper":
        "🗺 Open in CellMapper",
    "🗼 Вышка":
        "🗼 Tower",
    "🚀 Подключиться":
        "🚀 Connect",
    "🛡 Белые списки (РФ)":
        "🛡 Whitelist (RU)",
    "🧾 Сохранить диагностику":
        "🧾 Save diagnostics",

    # --- Android (android_main.py) ---
    ("IP по умолчанию: 192.168.8.1 (для B315/B525 — 192.168.1.1 или "
     "192.168.3.1) — кнопка «Найти» проверит их сама. Логин: admin, пароль "
     "— на наклейке роутера.\n"
     "\n"
     "Частые ошибки и что делать:\n"
     "• 108006 — неверный логин или пароль.\n"
     "• 108007 — слишком много неудачных попыток: роутер временно "
     "заблокировал вход. Подождите несколько минут.\n"
     "• 108003 — уже выполнен вход с другого устройства. Закройте "
     "веб-интерфейс роутера.\n"
     "• 100002 — функция не поддерживается этой моделью или прошивкой. "
     "Часть возможностей будет недоступна — это нормально.\n"
     "• 100003 / 125002 / 125003 — истекла сессия. Программа войдёт заново "
     "сама.\n"
     "• Нет ответа — проверьте, что телефон подключён к Wi-Fi именно этого "
     "роутера и IP введён верно.\n"
     "\n"
     "Тестовый режим показывает работу программы без роутера."):
        ("Default IP: 192.168.8.1 (B315/B525: 192.168.1.1 or 192.168.3.1) — the "
         "“Find” button checks them automatically. Login: admin, the password "
         "is on the router's label.\n"
         "\n"
         "Common errors and what to do:\n"
         "• 108006 — wrong username or password.\n"
         "• 108007 — too many failed attempts: the router has temporarily "
         "blocked logins. Wait a few minutes.\n"
         "• 108003 — already logged in from another device. Close the router's "
         "web interface.\n"
         "• 100002 — not supported by this model or firmware. Some features "
         "will be unavailable — this is normal.\n"
         "• 100003 / 125002 / 125003 — session expired. The app logs in again "
         "by itself.\n"
         "• No response — make sure the phone is connected to this router's "
         "Wi-Fi and the IP is correct.\n"
         "\n"
         "Test mode shows how the app works without a router."),
    ("«Переподключить связь» заставляет модем заново выбрать лучшую соту — "
     "быстрее перезагрузки. После перезагрузки программа переподключится "
     "сама."):
        ("“Reconnect link” makes the modem pick the best cell again — faster "
         "than a reboot. After a reboot the app reconnects by itself."),
    "Белые списки (РФ)":
        "Whitelist (RU)",
    "Во весь экран":
        "Fullscreen",
    "Все (AUTO)":
        "All (AUTO)",
    "Диагностика скопирована в буфер обмена. Личные номера замаскированы.":
        "Diagnostics copied to the clipboard. Personal identifiers are masked.",
    "Звук недоступен на этом устройстве":
        "Sound is not available on this device",
    "Информация":
        "Information",
    "Лучше — продолжайте":
        "Better — keep going",
    "Меню":
        "Menu",
    "Нажмите «Назад» ещё раз, чтобы отключиться":
        "Press “Back” again to disconnect",
    "Отключиться":
        "Disconnect",
    "Отключиться от роутера?":
        "Disconnect from the router?",
    "Перезагрузить роутер":
        "Reboot router",
    "Переподключить":
        "Reconnect",
    "Пики сброшены":
        "Peaks reset",
    "Подключиться":
        "Connect",
    "Подсказка":
        "Help",
    "Портативный монитор LTE/5G Huawei":
        "Portable Huawei LTE/5G monitor",
    "Проверить сейчас":
        "Check now",
    "Сеть":
        "Network",
    "Состояние":
        "Status",
    ("Список взят из модема; замеченные в эфире бэнды — первыми. Меняются "
     "только LTE-бэнды; «Как было» восстановит настройки, прочитанные при "
     "подключении."):
        ("The list comes from the modem; bands seen on air come first. Only LTE "
         "bands are changed; “As before” restores the settings read at "
         "connection time."),
    "Стабильно — фиксируйте":
        "Stable — fix it here",
    "Тестовый режим (без модема)":
        "Test mode (no modem)",
    "Хуже — поверните обратно":
        "Worse — turn back",
    "ℹ Информация":
        "ℹ Information",
    "← Назад":
        "← Back",
    "↩ Как было":
        "↩ As before",
    "⏏ Отключиться":
        "⏏ Disconnect",
    "☀ Солнце: вкл":
        "☀ Sun mode: on",
    "☀ Солнце: выкл":
        "☀ Sun mode: off",
    "☰ Меню":
        "☰ Menu",
    ("⚠ Телефон должен быть подключён к Wi-Fi именно этого роутера — иначе "
     "тест измерит чужой канал. Применимо только для РФ."):
        ("⚠ The phone must be connected to the Wi-Fi of this exact router — "
         "otherwise the test measures a different link. Applies to Russia only."),
    "⟲ Пики":
        "⟲ Peaks",
    "🎛 Сеть":
        "🎛 Network",
    "🔊 Звук":
        "🔊 Sound",
    "🔎 Найти":
        "🔎 Find",
    "🧾 Скопировать диагностику":
        "🧾 Copy diagnostics",
}


def set_language(lang: str) -> None:
    """Устанавливает текущий язык ('ru' или 'en'). Неизвестный — игнор."""
    global _current_lang
    if lang in LANGUAGES:
        _current_lang = lang


def current_language() -> str:
    """Возвращает код текущего языка."""
    return _current_lang


def available_languages() -> list[str]:
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
