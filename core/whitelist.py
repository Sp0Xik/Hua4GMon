"""
Проверка режима «белых списков» БС в России через TCP-пробы.

Подход:
  1. Открываем TCP-соединение на порт 443 каждой цели (без HTTP-запроса).
     Если соединение устанавливается — оператор пропускает SNI/IP.
  2. Сравниваем результаты для двух групп:
      * WHITELIST_HOSTS_RU  — точно в белых списках всех ОпСоС РФ;
      * CONTROL_HOSTS_NEUTRAL — не блокированы РКН, не в белых списках.
  3. Вывод по таблице истинности:
      white = ✔, neutral = ✔   →  фильтр ВЫКЛ (обычный режим);
      white = ✔, neutral = ✘   →  фильтр ВКЛ (только белые!);
      white = ✘, neutral = ✔   →  Wi-Fi/VPN не через 4G (странно);
      white = ✘, neutral = ✘   →  нет интернета вообще / DNS лежит.

Почему TCP-сокет, а не HTTP/ping:
  * ICMP-пинг операторы часто фильтруют отдельно — он ничего не скажет
    о наличии HTTPS-фильтра по SNI.
  * Полноценный HTTPS-handshake тяжелее и медленнее. Открытый TCP-syn
    даёт всё, что нужно: дошёл ли пакет до 443/tcp на удалённом хосте.
  * Современные DPI РФ блокируют именно на L4/L7 по host/SNI — TCP-
    соединение в этом случае всё равно не установится (RST или таймаут).
"""
from __future__ import annotations

import socket
from typing import List, Tuple

from core.constants import WL_CHECK_TIMEOUT


def tcp_reachable(host: str, port: int,
                   timeout: float = WL_CHECK_TIMEOUT) -> Tuple[bool, str]:
    """Пытается открыть TCP-соединение. Возвращает (доступен, описание)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "OK"
    except socket.timeout:
        return False, "таймаут"
    except socket.gaierror:
        return False, "DNS не отвечает"
    except ConnectionRefusedError:
        return False, "соединение отклонено"
    except OSError as e:
        return False, f"ошибка ({e.errno})"


def analyze_whitelist_results(
        white_results: List[Tuple[str, bool]],
        neutral_results: List[Tuple[str, bool]]) -> Tuple[str, str, str]:
    """По таблице истинности возвращает (заголовок, описание, цвет).

    Строки переводятся на текущий язык (RU/EN) через core.i18n.
    """
    from core.i18n import t
    white_ok = sum(1 for _, ok in white_results if ok)
    neutral_ok = sum(1 for _, ok in neutral_results if ok)
    wt, nt = len(white_results), len(neutral_results)
    white_any = white_ok > 0
    neutral_any = neutral_ok > 0

    if white_any and neutral_any:
        return (t("Белые списки ВЫКЛЮЧЕНЫ"),
                t("Обычный режим — открыт весь интернет "
                  "(белых: {w}/{wt}, нейтральных: {n}/{nt}).").format(
                      w=white_ok, wt=wt, n=neutral_ok, nt=nt),
                "#00b894")
    if white_any and not neutral_any:
        return (t("⚠ Белые списки ВКЛЮЧЕНЫ"),
                t("Сейчас на БС работают ТОЛЬКО разрешённые сайты "
                  "(белых: {w}/{wt}, нейтральных: 0/{nt}). "
                  "Обычные сайты заблокированы оператором.").format(
                      w=white_ok, wt=wt, nt=nt),
                "#d63031")
    if not white_any and neutral_any:
        return (t("Аномалия"),
                t("Нейтральные сайты доступны, но «белые» не отвечают. "
                  "Скорее всего, вы вышли в интернет не через 4G "
                  "(другой Wi-Fi, провод, VPN). Подключитесь к Wi-Fi роутера "
                  "и повторите."),
                "#fdcb6e")
    return (t("Нет интернета"),
            t("Ни одна цель не отвечает. Либо у роутера нет связи с БС, "
              "либо проблема с DNS/маршрутом. Проверьте RSRP и трафик."),
            "#636e72")
