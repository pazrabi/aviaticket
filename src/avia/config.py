from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("avia.config")


@dataclass
class SiteConfig:
    name: str
    url_template: str
    enabled: bool = True
    wait_selector: str | None = None
    settle_ms: int = 8000
    scrolls: int = 3
    note: str = ""
    # source: "browser" (Playwright + парсер) или "api" (HTTP-клиент без браузера)
    source: str = "browser"
    # parser: имя зарегистрированного парсера (для api это имя клиента)
    parser: str = ""
    # use_llm: если парсера нет или он ничего не нашёл, отдать страницу в LLM (по умолчанию выключено)
    use_llm: bool = False
    # id_resolver: имя резолвера id (например, 'tutu' включает разрешение id городов)
    id_resolver: str = ""
    url_template_rt: str = ""
    # replay_post: конфигурация повтора POST-запроса {"url_contains": "...", "set": {...}}
    replay_post: dict | None = None
    phone: str = ""
    phone_extend: bool = True
    phone_capture_json: list[str] = field(default_factory=list)
    phone_wait_seconds: int = 40
    # подстрока в ответе, по которой видно конец выдачи (хаб ждёт её появления)
    phone_wait_for: str = ""
    # сколько секунд ждать предпочтительный (первый в списке phone) телефон, прежде чем брать запасной
    phone_patience_seconds: int = 0
    # для подписок и фона (priority > 0): другое время ожидания и признак конца выдачи
    phone_full_wait_seconds: int | None = None
    phone_full_wait_for: str | None = None
    stage: str = "live"

    def __post_init__(self) -> None:
        if self.phone_patience_seconds < 0:
            raise ValueError("phone_patience_seconds must be >= 0")
        if not isinstance(self.phone_extend, bool):
            raise TypeError("phone_extend must be a boolean")
        if not isinstance(self.stage, str) or self.stage not in ("live", "trial"):
            log.warning(
                "Неизвестное значение stage=%r для сайта %s, сброс в 'live'",
                self.stage,
                getattr(self, "name", "?"),
            )
            self.stage = "live"


@dataclass
class Settings:
    sites: list[SiteConfig]
    tp_token: str = ""            # Travelpayouts API token
    tp_marker: str = ""           # партнёрский marker для ссылок
    droidhub_url: str = ""        # адрес сервиса droidhub (HTTP API)
    droidhub_token: str = ""      # токен авторизации droidhub
    droidhub_devices: list[str] = field(default_factory=list)  # пул телефонов droidhub
    phones_new_policy: str = "check"  # "check" (ожидает проверки), "active" (сразу в работу) или "hold"
    phones_hold: list[str] = field(default_factory=list)      # телефоны на жёсткой заморозке
    phones_refresh_seconds: int = 60   # интервал опроса хаба (сек)
    phones_snapshot_ttl_seconds: int = 180  # TTL актуальности снимка состояния (сек)
    phones: Any = None                 # объект PhoneRegistry (опционально)
    # llm_backend нужен только сайтам с use_llm: true
    # "agy_ssh" (аккаунт пула на VPS через SSH) или "gemini_local" (локальный gemini CLI)
    llm_backend: str = "gemini_local"
    gemini_cmd: str = "gemini"
    gemini_model: str | None = None
    gemini_timeout: int = 180
    ssh_host: str = ""            # например root@193.41.86.92
    ssh_key: str = ""             # например ~/.ssh/aeza_pool
    agy_account: str = "acc4"
    agy_proxy: str = ""           # http://127.0.0.1:3105, исходящий канал аккаунта на сервере
    agy_wrapper: str = "/opt/gemini-pool/bin/avia-llm"
    headless: bool = True
    storage_dir: Path = field(default_factory=lambda: ROOT / "state")
    debug_dir: Path = field(default_factory=lambda: ROOT / "debug")
    top_n: int = 5
    balance_max_premium_pct: float = 30.0
    balance_min_saving_minutes: int = 120
    balance_min_saving_share: float = 0.25
    site_top_cheap: int = 2
    site_top_balance: int = 2
    site_top_fast: int = 1
    tg_token: str = ""
    tg_api_base: str = ""          # пусто = https://api.telegram.org; для моков и локального шлюза
    tg_allowed_ids: set[int] = field(default_factory=set)   # администраторы; без лимитов поисков
    tg_open: bool = False          # True: бот отвечает всем, пользователи регистрируются в users.sqlite
    daily_search_limit: int = 40   # поисков в сутки на человека (0 = без лимита)
    search_budget_per_hour: int = 0  # поисков на весь бот за скользящий час, админы не в счёт (0 = выключено)
    new_users_per_day: int = 0     # новых людей в сутки при TG_OPEN=1, админы не в счёт (0 = выключено)
    search_parallel_live: int = 6  # сайтов одного живого поиска одновременно
    search_parallel_bg: int = 2  # сайтов одного фонового/подписочного поиска одновременно
    max_parallel_searches: int = 2  # одновременных поисков во всём боте (телефоны одни на всех)
    bg_collect: bool = False       # фоновый сбор цен по популярным маршрутам
    bg_collect_interval_minutes: int = 20  # интервал между фоновыми поисками (мин)
    bg_collect_max_per_day: int = 120      # максимум фоновых поисков в сутки
    bg_collect_recheck_hours: int = 24     # минимальная пауза повтора маршрута (ч)
    bg_collect_top_routes: int = 12        # число популярных маршрутов из истории
    bg_collect_seed_routes: list[str] = field(
        default_factory=lambda: ["MOW-LED", "MOW-AER", "MOW-KUF", "MOW-OVB", "MOW-SVX", "MOW-KZN"]
    )
    bg_collect_horizons_days: list[int] = field(
        default_factory=lambda: [7, 14, 28, 56, 90]
    )
    backup_dir: Path | None = None
    backup_keep: int = 14
    backup_hour: int = 4
    forecast_enabled: bool = True
    forecast_retrain_days: int = 7
    forecast_dir: Path | None = None
    history_max_offers_per_site: int = 15
    parser_watch_enabled: bool = True
    parser_watch_hours: int = 6
    parser_watch_min_runs: int = 8
    parser_watch_min_ok_pct: int = 60
    parser_watch_max_incomplete_pct: int = 60
    parser_watch_cooldown_hours: int = 6
    speed_routing: bool = True
    speed_window_days: int = 7
    speed_min_samples: int = 5
    speed_slow_ratio: float = 3.0
    speed_bg_patience_seconds: int = 60
    speed_quarantine_failures: int = 3
    speed_quarantine_seconds: int = 600
    speed_refresh_seconds: int = 300
    speed_live_min_p_lower: float = 0.6
    phone_min_battery: int = 15
    live_assignment: bool = True
    phone_wake_seconds: float = 5.0
    live_safety_seconds: float = 90.0
    deep_safety_seconds: float = 150.0
    deep_cache_ttl_seconds: int = 900
    parser_timeout_seconds: float = 5.0
    site_fuse_enabled: bool = True
    site_fuse_window_seconds: int = 300
    site_fuse_min_phones: int = 2
    site_fuse_probe_seconds: int = 600
    soft_allow_enabled: bool = True
    soft_allow_interval_hours: float = 24.0
    soft_allow_max_pairs_per_run: int = 2
    metrics: Any = None
    price_timeline: bool = True
    fast_stop: bool = True
    stop_stable_seconds: float = 8.0
    stop_calibrate: bool = True
    preempt: bool = True
    hedge: bool = True
    hedge_fail_share: float = 0.15
    hedge_min_attempts: int = 5
    hedge_max_per_search: int = 2
    hedge_delay_seconds: float = 0.0
    live_reserve: bool = True
    trial_stage: bool = True
    history_hint: bool = True
    fresh_max_age_seconds: float = 600.0
    kupibilet_ticket_link: bool = True
    # П31: карточка живого поиска не ждёт самый медленный сайт (CARD_EARLY=1; по умолчанию выключено)
    card_early: bool = False
    card_early_seconds: float = 30.0  # в этот момент карточка уходит, если цены уже есть
    card_early_sites: int = 4  # карточка уходит, как только сайтов с ценами столько

    def __post_init__(self) -> None:
        if self.backup_dir is None:
            self.backup_dir = self.storage_dir / "backups"
        else:
            self.backup_dir = Path(self.backup_dir)
        if self.forecast_dir is None:
            self.forecast_dir = self.storage_dir / "forecast"
        else:
            self.forecast_dir = Path(self.forecast_dir)


def load_settings(path: Path | None = None) -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    path = path or ROOT / "config" / "sites.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    sites = [SiteConfig(**s) for s in raw.get("sites", [])]
    allowed = {
        int(x) for x in os.getenv("TG_ALLOWED_IDS", "").replace(" ", "").split(",") if x
    }
    droidhub_devices = [
        x.strip() for x in os.getenv("DROIDHUB_DEVICES", "").split(",") if x.strip()
    ]
    phones_new_policy = os.getenv("PHONES_NEW_POLICY", "check").strip() or "check"
    phones_hold = [
        x.strip() for x in os.getenv("PHONES_HOLD", "").split(",") if x.strip()
    ]
    try:
        phones_refresh_sec = int(os.getenv("PHONES_REFRESH_SECONDS", "60"))
    except ValueError:
        phones_refresh_sec = 60
    try:
        phones_snapshot_ttl = int(os.getenv("PHONES_SNAPSHOT_TTL_SECONDS", "180"))
    except ValueError:
        phones_snapshot_ttl = 180
    env_seed = os.getenv("BG_COLLECT_SEED_ROUTES")
    if env_seed is not None:
        bg_seed_routes = [x.strip() for x in env_seed.split(",") if x.strip()]
    else:
        bg_seed_routes = ["MOW-LED", "MOW-AER", "MOW-KUF", "MOW-OVB", "MOW-SVX", "MOW-KZN"]

    env_horizons = os.getenv("BG_COLLECT_HORIZONS_DAYS")
    if env_horizons is not None:
        bg_horizons = [int(x.strip()) for x in env_horizons.split(",") if x.strip()]
    else:
        bg_horizons = [7, 14, 28, 56, 90]

    speed_routing_env = os.getenv("SPEED_ROUTING", "1").strip().lower()
    speed_routing = speed_routing_env not in ("0", "false", "False", "no", "off")
    try:
        speed_window_days = int(os.getenv("SPEED_WINDOW_DAYS", "7"))
    except ValueError:
        speed_window_days = 7
    try:
        speed_min_samples = int(os.getenv("SPEED_MIN_SAMPLES", "5"))
    except ValueError:
        speed_min_samples = 5
    try:
        speed_slow_ratio = float(os.getenv("SPEED_SLOW_RATIO", "3.0"))
    except ValueError:
        speed_slow_ratio = 3.0
    try:
        speed_bg_patience_seconds = int(os.getenv("SPEED_BG_PATIENCE_SECONDS", "60"))
    except ValueError:
        speed_bg_patience_seconds = 60
    try:
        speed_quarantine_failures = int(os.getenv("SPEED_QUARANTINE_FAILURES", "3"))
    except ValueError:
        speed_quarantine_failures = 3
    try:
        speed_quarantine_seconds = int(os.getenv("SPEED_QUARANTINE_SECONDS", "600"))
    except ValueError:
        speed_quarantine_seconds = 600
    try:
        speed_refresh_seconds = int(os.getenv("SPEED_REFRESH_SECONDS", "300"))
    except ValueError:
        speed_refresh_seconds = 300
    try:
        speed_live_min_p_lower = float(os.getenv("SPEED_LIVE_MIN_P_LOWER", "0.6"))
    except ValueError:
        speed_live_min_p_lower = 0.6
    try:
        phone_min_battery = int(os.getenv("PHONE_MIN_BATTERY", "15"))
    except ValueError:
        phone_min_battery = 15
    live_assignment_env = os.getenv("LIVE_ASSIGNMENT", "1").strip().lower()
    live_assignment = live_assignment_env not in ("0", "false", "no", "off")
    try:
        phone_wake_seconds = float(os.getenv("PHONE_WAKE_SECONDS", "5.0"))
    except ValueError:
        phone_wake_seconds = 5.0
    try:
        live_safety_seconds = float(os.getenv("LIVE_SAFETY_SECONDS", "90.0"))
    except ValueError:
        live_safety_seconds = 90.0
    try:
        deep_safety_seconds = float(os.getenv("DEEP_SAFETY_SECONDS", "150.0"))
    except ValueError:
        deep_safety_seconds = 150.0
    try:
        deep_cache_ttl_seconds = int(os.getenv("DEEP_CACHE_TTL_SECONDS", "900"))
    except ValueError:
        deep_cache_ttl_seconds = 900
    try:
        parser_timeout_seconds = float(os.getenv("PARSER_TIMEOUT_SECONDS", "5.0"))
    except ValueError:
        parser_timeout_seconds = 5.0
    site_fuse_env = os.getenv("SITE_FUSE", "1").strip().lower()
    site_fuse_enabled = site_fuse_env not in ("0", "false", "no", "off")
    try:
        site_fuse_window_seconds = int(os.getenv("SITE_FUSE_WINDOW_SECONDS", "300"))
    except ValueError:
        site_fuse_window_seconds = 300
    try:
        site_fuse_min_phones = int(os.getenv("SITE_FUSE_MIN_PHONES", "2"))
    except ValueError:
        site_fuse_min_phones = 2
    try:
        site_fuse_probe_seconds = int(os.getenv("SITE_FUSE_PROBE_SECONDS", "600"))
    except ValueError:
        site_fuse_probe_seconds = 600
    try:
        balance_max_premium_pct = float(os.getenv("BALANCE_MAX_PREMIUM_PCT", "30"))
    except ValueError:
        balance_max_premium_pct = 30.0
    try:
        balance_min_saving_minutes = int(os.getenv("BALANCE_MIN_SAVING_MINUTES", "120"))
    except ValueError:
        balance_min_saving_minutes = 120
    try:
        balance_min_saving_share = float(os.getenv("BALANCE_MIN_SAVING_SHARE", "0.25"))
    except ValueError:
        balance_min_saving_share = 0.25
    try:
        site_top_cheap = int(os.getenv("SITE_TOP_CHEAP", "2"))
    except ValueError:
        site_top_cheap = 2
    try:
        site_top_balance = int(os.getenv("SITE_TOP_BALANCE", "2"))
    except ValueError:
        site_top_balance = 2
    try:
        site_top_fast = int(os.getenv("SITE_TOP_FAST", "1"))
    except ValueError:
        site_top_fast = 1

    soft_allow_env = os.getenv("SOFT_ALLOW", "1").strip().lower()
    soft_allow_enabled = soft_allow_env not in ("0", "false", "False", "no", "off")
    try:
        soft_allow_interval_hours = float(os.getenv("SOFT_ALLOW_INTERVAL_HOURS", "24"))
    except ValueError:
        soft_allow_interval_hours = 24.0
    try:
        soft_allow_max_pairs = int(os.getenv("SOFT_ALLOW_MAX_PAIRS", "2"))
    except ValueError:
        soft_allow_max_pairs = 2

    price_timeline_env = os.getenv("PRICE_TIMELINE", "1").strip().lower()
    price_timeline = price_timeline_env not in ("0", "false", "False", "no", "off")

    fast_stop_env = os.getenv("FAST_STOP", "1").strip().lower()
    fast_stop = fast_stop_env not in ("0", "false", "False", "no", "off")
    try:
        stop_stable_seconds = float(os.getenv("STOP_STABLE_SECONDS", "8.0"))
    except ValueError:
        stop_stable_seconds = 8.0

    stop_calibrate_env = os.getenv("STOP_CALIBRATE", "1").strip().lower()
    stop_calibrate = stop_calibrate_env not in ("0", "false", "False", "no", "off")

    preempt_env = os.getenv("PREEMPT", "1").strip().lower()
    preempt = preempt_env not in ("0", "false", "False", "no", "off")

    hedge_env = os.getenv("HEDGE", "1").strip().lower()
    hedge = hedge_env not in ("0", "false", "False", "no", "off")
    try:
        hedge_fail_share = float(os.getenv("HEDGE_FAIL_SHARE", "0.15"))
    except ValueError:
        hedge_fail_share = 0.15
    try:
        hedge_min_attempts = int(os.getenv("HEDGE_MIN_ATTEMPTS", "5"))
    except ValueError:
        hedge_min_attempts = 5
    try:
        hedge_max_per_search = int(os.getenv("HEDGE_MAX_PER_SEARCH", "2"))
    except ValueError:
        hedge_max_per_search = 2
    try:
        hedge_delay_seconds = float(os.getenv("HEDGE_DELAY_SECONDS", "0.0"))
    except ValueError:
        hedge_delay_seconds = 0.0

    live_reserve_env = os.getenv("LIVE_RESERVE", "1").strip().lower()
    live_reserve = live_reserve_env not in ("0", "false", "False", "no", "off")

    trial_stage_env = os.getenv("TRIAL_STAGE", "1").strip().lower()
    trial_stage = trial_stage_env not in ("0", "false", "False", "no", "off")

    history_hint_env = os.getenv("HISTORY_HINT", "1").strip().lower()
    history_hint = history_hint_env not in ("0", "false", "False", "no", "off")

    try:
        fresh_max_age_seconds = float(os.getenv("FRESH_MAX_AGE_SECONDS", "600"))
    except ValueError:
        fresh_max_age_seconds = 600.0
    kupibilet_ticket_link_env = os.getenv("KUPIBILET_TICKET_LINK", "1").strip().lower()
    kupibilet_ticket_link = kupibilet_ticket_link_env not in ("0", "false", "False", "no", "off")

    card_early = os.getenv("CARD_EARLY", "0").strip() == "1"
    try:
        card_early_seconds = float(os.getenv("CARD_EARLY_SECONDS", "30"))
    except ValueError:
        card_early_seconds = 30.0
    if not card_early_seconds > 0:  # ноль, минус и nan — по умолчанию
        card_early_seconds = 30.0
    try:
        card_early_sites = int(os.getenv("CARD_EARLY_SITES", "4"))
    except ValueError:
        card_early_sites = 4
    if card_early_sites < 1:
        card_early_sites = 4

    try:
        search_budget_per_hour = max(0, int(os.getenv("SEARCH_BUDGET_PER_HOUR", "0")))
    except ValueError:
        search_budget_per_hour = 0
    try:
        new_users_per_day = max(0, int(os.getenv("NEW_USERS_PER_DAY", "0")))
    except ValueError:
        new_users_per_day = 0

    return Settings(
        sites=sites,
        tp_token=os.getenv("TP_TOKEN", ""),
        tp_marker=os.getenv("TP_MARKER", ""),
        droidhub_url=os.getenv("DROIDHUB_URL", ""),
        droidhub_token=os.getenv("DROIDHUB_TOKEN", ""),
        droidhub_devices=droidhub_devices,
        phones_new_policy=phones_new_policy,
        phones_hold=phones_hold,
        phones_refresh_seconds=phones_refresh_sec,
        phones_snapshot_ttl_seconds=phones_snapshot_ttl,
        llm_backend=os.getenv("LLM_BACKEND", "gemini_local"),
        gemini_cmd=os.getenv("GEMINI_CMD", "gemini"),
        gemini_model=os.getenv("GEMINI_MODEL") or None,
        gemini_timeout=int(os.getenv("GEMINI_TIMEOUT", "180")),
        ssh_host=os.getenv("AGY_SSH_HOST", ""),
        ssh_key=os.path.expanduser(os.getenv("AGY_SSH_KEY", "")),
        agy_account=os.getenv("AGY_ACCOUNT", "acc4"),
        agy_proxy=os.getenv("AGY_PROXY", ""),
        agy_wrapper=os.getenv("AGY_WRAPPER", "/opt/gemini-pool/bin/avia-llm"),
        headless=os.getenv("HEADLESS", "1") != "0",
        top_n=int(os.getenv("TOP_N", "5")),
        balance_max_premium_pct=balance_max_premium_pct,
        balance_min_saving_minutes=balance_min_saving_minutes,
        balance_min_saving_share=balance_min_saving_share,
        site_top_cheap=site_top_cheap,
        site_top_balance=site_top_balance,
        site_top_fast=site_top_fast,
        tg_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_api_base=os.getenv("TG_API_BASE", ""),
        tg_allowed_ids=allowed,
        tg_open=os.getenv("TG_OPEN", "0") == "1",
        daily_search_limit=int(os.getenv("DAILY_SEARCH_LIMIT", "40")),
        search_budget_per_hour=search_budget_per_hour,
        new_users_per_day=new_users_per_day,
        search_parallel_live=max(1, int(os.getenv("SEARCH_PARALLEL_LIVE", "6"))),
        search_parallel_bg=max(1, int(os.getenv("SEARCH_PARALLEL_BG", "2"))),
        max_parallel_searches=max(1, int(os.getenv("MAX_PARALLEL_SEARCHES", "2"))),
        bg_collect=os.getenv("BG_COLLECT", "0") == "1",
        bg_collect_interval_minutes=int(os.getenv("BG_COLLECT_INTERVAL_MIN", "20")),
        bg_collect_max_per_day=int(os.getenv("BG_COLLECT_MAX_PER_DAY", "120")),
        bg_collect_recheck_hours=int(os.getenv("BG_COLLECT_RECHECK_HOURS", "24")),
        bg_collect_top_routes=int(os.getenv("BG_COLLECT_TOP_ROUTES", "12")),
        bg_collect_seed_routes=bg_seed_routes,
        bg_collect_horizons_days=bg_horizons,
        backup_dir=Path(os.environ["BACKUP_DIR"]) if os.getenv("BACKUP_DIR") else None,
        backup_keep=int(os.getenv("BACKUP_KEEP", "14")),
        backup_hour=int(os.getenv("BACKUP_HOUR", "4")),
        forecast_enabled=os.getenv("FORECAST_ENABLED", "1") != "0",
        forecast_retrain_days=int(os.getenv("FORECAST_RETRAIN_DAYS", "7")),
        forecast_dir=Path(os.environ["FORECAST_DIR"]) if os.getenv("FORECAST_DIR") else None,
        history_max_offers_per_site=int(os.getenv("HISTORY_MAX_OFFERS_PER_SITE", "15")),
        parser_watch_enabled=os.getenv("PARSER_WATCH_ENABLED", "1") not in ("0", "false", "False", "no"),
        parser_watch_hours=int(os.getenv("PARSER_WATCH_HOURS", "6")),
        parser_watch_min_runs=int(os.getenv("PARSER_WATCH_MIN_RUNS", "8")),
        parser_watch_min_ok_pct=int(os.getenv("PARSER_WATCH_MIN_OK_PCT", "60")),
        parser_watch_max_incomplete_pct=int(os.getenv("PARSER_WATCH_MAX_INCOMPLETE_PCT", "60")),
        parser_watch_cooldown_hours=int(os.getenv("PARSER_WATCH_COOLDOWN_HOURS", "6")),
        speed_routing=speed_routing,
        speed_window_days=speed_window_days,
        speed_min_samples=speed_min_samples,
        speed_slow_ratio=speed_slow_ratio,
        speed_bg_patience_seconds=speed_bg_patience_seconds,
        speed_quarantine_failures=speed_quarantine_failures,
        speed_quarantine_seconds=speed_quarantine_seconds,
        speed_refresh_seconds=speed_refresh_seconds,
        speed_live_min_p_lower=speed_live_min_p_lower,
        phone_min_battery=phone_min_battery,
        live_assignment=live_assignment,
        phone_wake_seconds=phone_wake_seconds,
        live_safety_seconds=live_safety_seconds,
        deep_safety_seconds=deep_safety_seconds,
        deep_cache_ttl_seconds=deep_cache_ttl_seconds,
        parser_timeout_seconds=parser_timeout_seconds,
        site_fuse_enabled=site_fuse_enabled,
        site_fuse_window_seconds=site_fuse_window_seconds,
        site_fuse_min_phones=site_fuse_min_phones,
        site_fuse_probe_seconds=site_fuse_probe_seconds,
        soft_allow_enabled=soft_allow_enabled,
        soft_allow_interval_hours=soft_allow_interval_hours,
        soft_allow_max_pairs_per_run=soft_allow_max_pairs,
        price_timeline=price_timeline,
        fast_stop=fast_stop,
        stop_stable_seconds=stop_stable_seconds,
        stop_calibrate=stop_calibrate,
        preempt=preempt,
        hedge=hedge,
        hedge_fail_share=hedge_fail_share,
        hedge_min_attempts=hedge_min_attempts,
        hedge_max_per_search=hedge_max_per_search,
        hedge_delay_seconds=hedge_delay_seconds,
        live_reserve=live_reserve,
        trial_stage=trial_stage,
        history_hint=history_hint,
        fresh_max_age_seconds=fresh_max_age_seconds,
        kupibilet_ticket_link=kupibilet_ticket_link,
        card_early=card_early,
        card_early_seconds=card_early_seconds,
        card_early_sites=card_early_sites,
    )


# Добавляем распознавание «временно недоступен» в formatting.is_unavailable
try:
    from . import formatting as _fmt

    if hasattr(_fmt, "is_unavailable") and not getattr(_fmt.is_unavailable, "_fuse_patched", False):
        _orig_is_unavail = _fmt.is_unavailable

        def _fuse_is_unavailable(r: Any) -> bool:
            if r.ok:
                return False
            err = (getattr(r, "error", "") or "").lower()
            if "временно недоступен" in err:
                return True
            return _orig_is_unavail(r)

        _fuse_is_unavailable._fuse_patched = True  # type: ignore[attr-defined]
        _fmt.is_unavailable = _fuse_is_unavailable
except Exception:
    pass
