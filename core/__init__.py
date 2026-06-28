"""
Hua4GMon core: чистая логика, общая для всех frontend-ов.

Модули этого пакета:
    constants          — справочные таблицы (PLMN, BANDS, EARFCN, пороги
                          сигнала и т.д.) — никаких функций, только данные.
    parsers            — разбор и форматирование сырых строк от роутера
                          (IP, числа, cell_id, LTE-band, EARFCN, скорости).
    signal_analysis    — оценка качества сигнала (RSRP/SINR/RSSI/RSRQ).
    whitelist          — TCP-пробы для определения режима «белых списков».

Ни один модуль здесь НЕ импортирует tkinter, kivy или какую-либо
библиотеку UI. Можно безопасно использовать из любого frontend:
    * main.py            — desktop (Tkinter)
    * android_main.py    — будущий Android UI (Kivy)
    * любые скрипты автоматизации и тесты

Эквивалентные пути импорта:
    from core import evaluate_signal, format_band_label, PLMN_MAP   # короткий
    from core.signal_analysis import evaluate_signal                # явный
"""
from core.constants import (
    ANTENNA_MODES,
    BAND_FREQ_MAP,
    BANDS,
    CONTROL_HOSTS_NEUTRAL,
    DIRECTION_LOOKBACK,
    EARFCN_RANGES,
    GRAPH_HISTORY,
    JITTER_WINDOW,
    LTEBAND_AUTO_ALL,
    NETBAND_AUTO_MASK,
    NETMODE_AUTO,
    NETMODE_LTE_ONLY,
    PARAM_RANGES,
    PLMN_MAP,
    RECONNECT_DELAY_INITIAL,
    RECONNECT_DELAY_MAX,
    SESSION_LOG_MAX,
    SIGNAL_THRESHOLDS,
    WHITELIST_HOSTS_RU,
    WL_CHECK_TIMEOUT,
)
from core.i18n import (
    LANGUAGES,
    available_languages,
    current_language,
    set_language,
    t,
)
from core.parsers import (
    earfcn_to_band,
    extract_number,
    first_present,
    format_band_label,
    format_bytes_mb,
    format_rate_mbps,
    is_valid_ip,
    mcs_to_modulation,
    parse_antenna_value,
    parse_cell_id,
)
from core.signal_analysis import (
    calculate_overall_health,
    evaluate_signal,
)
from core.whitelist import (
    analyze_whitelist_results,
    tcp_reachable,
)

__all__ = [
    # constants
    "ANTENNA_MODES", "BAND_FREQ_MAP", "BANDS", "CONTROL_HOSTS_NEUTRAL",
    "DIRECTION_LOOKBACK", "EARFCN_RANGES", "GRAPH_HISTORY", "JITTER_WINDOW",
    "LTEBAND_AUTO_ALL", "NETBAND_AUTO_MASK", "NETMODE_AUTO", "NETMODE_LTE_ONLY",
    "PARAM_RANGES", "PLMN_MAP", "RECONNECT_DELAY_INITIAL",
    "RECONNECT_DELAY_MAX", "SESSION_LOG_MAX", "SIGNAL_THRESHOLDS",
    "WHITELIST_HOSTS_RU", "WL_CHECK_TIMEOUT",
    # parsers
    "earfcn_to_band", "extract_number", "first_present", "format_band_label",
    "format_bytes_mb", "format_rate_mbps", "is_valid_ip", "mcs_to_modulation",
    "parse_antenna_value", "parse_cell_id",
    # signal_analysis
    "calculate_overall_health", "evaluate_signal",
    # whitelist
    "analyze_whitelist_results", "tcp_reachable",
    # i18n
    "LANGUAGES", "available_languages", "current_language",
    "set_language", "t",
]
