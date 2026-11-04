from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


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
    phone_capture_json: list[str] = field(default_factory=list)
    phone_wait_seconds: int = 40
    # подстрока в ответе, по которой видно конец выдачи (хаб ждёт её появления)
    phone_wait_for: str = ""


@dataclass
class Settings:
    sites: list[SiteConfig]
    tp_token: str = ""            # Travelpayouts API token
    tp_marker: str = ""           # партнёрский marker для ссылок
    droidhub_url: str = ""        # адрес сервиса droidhub (HTTP API)
    droidhub_token: str = ""      # токен авторизации droidhub
    droidhub_devices: list[str] = field(default_factory=list)  # пул телефонов droidhub
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
    tg_token: str = ""
    tg_allowed_ids: set[int] = field(default_factory=set)   # администраторы; без лимитов поисков
    tg_open: bool = False          # True: бот отвечает всем, пользователи регистрируются в users.sqlite
    daily_search_limit: int = 40   # поисков в сутки на человека (0 = без лимита)
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

    return Settings(
        sites=sites,
        tp_token=os.getenv("TP_TOKEN", ""),
        tp_marker=os.getenv("TP_MARKER", ""),
        droidhub_url=os.getenv("DROIDHUB_URL", ""),
        droidhub_token=os.getenv("DROIDHUB_TOKEN", ""),
        droidhub_devices=droidhub_devices,
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
        tg_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_allowed_ids=allowed,
        tg_open=os.getenv("TG_OPEN", "0") == "1",
        daily_search_limit=int(os.getenv("DAILY_SEARCH_LIMIT", "40")),
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
    )
