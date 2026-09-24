"""
Hua4GMon core: логика, общая для Windows (Tkinter) и Android (Kivy).

Модули пакета:
    constants        — справочные таблицы (PLMN, LTE-бэнды, пороги, тайминги).
    parsers          — разбор сырых строк роутера (числа, EARFCN, маски, MCS).
    models           — типизированный снимок состояния модема (Snapshot).
    signal_analysis  — оценка качества сигнала.
    rf               — RF-аналитика: CA, MIMO, аплинк, советы монтажнику.
    state            — состояние сессии: пики, тренд, соты, лог, устаревание.
    band_lock        — безопасное планирование Band Lock (read-modify-write).
    errors           — классификация и понятные сообщения об ошибках.
    router           — сессия с роутером (единственный импорт huawei_lte_api).
    demo             — симулятор модема для тестового режима.
    discovery        — автопоиск роутера в локальной сети.
    whitelist        — проверка режима «белых списков» (TCP + TLS/SNI).
    export           — CSV для Excel и диагностический отчёт.
    i18n             — локализация RU/EN.

Ни один модуль не импортирует tkinter или kivy.
"""
from core.band_lock import (
    BandLockPlan,
    NetModeSettings,
    lock_warnings,
    lockable_bands,
    locked_bands,
    modem_supports_5g,
    plan_auto,
    plan_lock,
    plan_restore,
    verify,
)
from core.constants import (
    ANTENNA_MODES,
    BAND_FREQ_MAP,
    CONTROL_HOSTS_NEUTRAL,
    DIRECTION_LOOKBACK,
    DISCOVERY_CANDIDATES,
    EARFCN_RANGES,
    GRAPH_HISTORY,
    GRAPH_MIN_SPAN,
    JITTER_WINDOW,
    LTE_BAND_TABLE,
    LTEBAND_AUTO_ALL,
    NETBAND_AUTO_MASK,
    NETMODE_AUTO,
    NETMODE_LTE_ONLY,
    PARAM_RANGES,
    PLMN_MAP,
    REGION_BANDS,
    SESSION_LOG_MAX,
    SIGNAL_THRESHOLDS,
    WHITELIST_HOSTS_RU,
    WL_CHECK_TIMEOUT,
)
from core.demo import DemoModem, demo_angle_hint, demo_factory
from core.discovery import FoundRouter, discover_router, probe_huawei
from core.errors import ErrorKind, classify_error, humanize_error
from core.export import (
    build_diagnostics,
    csv_dialect,
    default_csv_name,
    diagnostics_json,
    mask_sensitive,
    write_session_csv,
)
from core.i18n import (
    LANGUAGES,
    available_languages,
    current_language,
    set_language,
    t,
)
from core.models import CellKey, Snapshot, build_snapshot
from core.parsers import (
    active_bands,
    bands_from_mask,
    bands_to_mask,
    dl_earfcn,
    earfcn_to_band,
    earfcn_to_freq_mhz,
    extract_number,
    first_present,
    format_band_label,
    format_band_list,
    format_bytes_mb,
    format_mimo,
    format_modulation,
    format_rate_mbps,
    is_valid_ip,
    lte_band_label,
    mask_to_bands,
    mcs_to_modulation,
    parse_access_modes,
    parse_antenna_response,
    parse_antenna_value,
    parse_cell_id,
    parse_nci,
    parse_pusch_power,
    parse_supported_bands,
)
from core.rf import (
    MIMO_LABELS,
    RAT_LABELS,
    UPLINK_LABELS,
    advice,
    classify_rat,
    detect_ca,
    mimo_status,
    uplink_status,
)
from core.router import (
    RouterConfig,
    RouterSession,
    SessionWorker,
    configure_library_logging,
    library_version,
)
from core.signal_analysis import (
    calculate_overall_health,
    curve_score,
    evaluate_signal,
)
from core.state import (
    TREND_COLLECTING,
    TREND_DOWN,
    TREND_FLAT,
    TREND_UP,
    CellChange,
    CellStats,
    SignalState,
    TrendTracker,
)
from core.whitelist import (
    ProbeResult,
    WhitelistReport,
    analyze_whitelist_results,
    probe_host,
    run_whitelist_check,
    tcp_reachable,
)

# Единственный источник версии: UI, buildozer (version.regex), CI и
# VERSIONINFO берут её отсюда.
__version__ = "1.4.0"

__all__ = [
    "__version__",
    # band_lock
    "BandLockPlan", "NetModeSettings", "lock_warnings", "lockable_bands",
    "locked_bands", "modem_supports_5g", "plan_auto", "plan_lock",
    "plan_restore", "verify",
    # constants
    "ANTENNA_MODES", "BAND_FREQ_MAP", "CONTROL_HOSTS_NEUTRAL",
    "DIRECTION_LOOKBACK", "DISCOVERY_CANDIDATES", "EARFCN_RANGES",
    "GRAPH_HISTORY", "GRAPH_MIN_SPAN", "JITTER_WINDOW", "LTE_BAND_TABLE",
    "LTEBAND_AUTO_ALL", "NETBAND_AUTO_MASK", "NETMODE_AUTO", "NETMODE_LTE_ONLY",
    "PARAM_RANGES", "PLMN_MAP", "REGION_BANDS", "SESSION_LOG_MAX",
    "SIGNAL_THRESHOLDS", "WHITELIST_HOSTS_RU", "WL_CHECK_TIMEOUT",
    # demo / discovery / errors / export
    "DemoModem", "demo_angle_hint", "demo_factory",
    "FoundRouter", "discover_router", "probe_huawei",
    "ErrorKind", "classify_error", "humanize_error",
    "build_diagnostics", "csv_dialect", "default_csv_name",
    "diagnostics_json", "mask_sensitive", "write_session_csv",
    # i18n
    "LANGUAGES", "available_languages", "current_language",
    "set_language", "t",
    # models
    "CellKey", "Snapshot", "build_snapshot",
    # parsers
    "active_bands", "bands_from_mask", "bands_to_mask", "dl_earfcn",
    "earfcn_to_band", "earfcn_to_freq_mhz", "extract_number",
    "first_present", "format_band_label", "format_band_list",
    "format_bytes_mb", "format_mimo", "format_modulation",
    "format_rate_mbps", "is_valid_ip", "lte_band_label", "mask_to_bands",
    "mcs_to_modulation", "parse_access_modes", "parse_antenna_response",
    "parse_antenna_value", "parse_cell_id", "parse_nci",
    "parse_pusch_power", "parse_supported_bands",
    # rf
    "MIMO_LABELS", "RAT_LABELS", "UPLINK_LABELS", "advice", "classify_rat",
    "detect_ca", "mimo_status", "uplink_status",
    # router
    "RouterConfig", "RouterSession", "SessionWorker",
    "configure_library_logging", "library_version",
    # signal_analysis
    "calculate_overall_health", "curve_score", "evaluate_signal",
    # state
    "TREND_COLLECTING", "TREND_DOWN", "TREND_FLAT", "TREND_UP",
    "CellChange", "CellStats", "SignalState", "TrendTracker",
    # whitelist
    "ProbeResult", "WhitelistReport", "analyze_whitelist_results",
    "probe_host", "run_whitelist_check", "tcp_reachable",
]
