

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_price_history(tmp_path, monkeypatch):
    """Тесты бота не должны писать в настоящую историю цен и подписки state/*.sqlite."""
    import avia.bot
    from avia.history import PriceHistory
    from avia.subscriptions import SubscriptionStore

    monkeypatch.setattr(avia.bot, "PriceHistory", lambda _path: PriceHistory(tmp_path / "history.sqlite"))
    from avia.users import UserStore

    monkeypatch.setattr(avia.bot, "UserStore", lambda _path: UserStore(tmp_path / "users.sqlite"))
    try:
        from avia.metrics import MetricsStore

        monkeypatch.setattr(avia.bot, "MetricsStore", lambda _path: MetricsStore(tmp_path / "metrics.sqlite"), raising=False)
    except ImportError:
        pass
    monkeypatch.setattr(avia.bot, "SubscriptionStore", lambda _path: SubscriptionStore(tmp_path / "subscriptions.sqlite"))
    try:
        from avia.phones import PhoneRegistry

        def _make_isolated_registry(*args, **kwargs):
            kwargs["storage_dir"] = tmp_path
            kwargs["path"] = tmp_path / "phones.json"
            return PhoneRegistry(**kwargs)

        monkeypatch.setattr(
            avia.bot,
            "PhoneRegistry",
            _make_isolated_registry,
            raising=False,
        )
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _no_real_droidhub(monkeypatch):
    """Тесты не ходят на телефоны: пустые значения из окружения load_dotenv из .env не перетирает."""
    for name in ("DROIDHUB_URL", "DROIDHUB_TOKEN", "DROIDHUB_DEVICES", "PHONES_HOLD", "PHONES_NEW_POLICY"):
        monkeypatch.setenv(name, "")


@pytest.fixture(autouse=True)
def _cwd_in_tmp(tmp_path, monkeypatch):
    """Относительные пути (state/...) в тестах ведут во временный каталог, а не в боевые базы."""
    monkeypatch.chdir(tmp_path)
    import avia.config as _cfg

    from avia.metrics import MetricsStore

    real_state = (_cfg.ROOT / "state").resolve()
    orig_init = MetricsStore.__init__

    def _init(self, path):
        pp = Path(path)
        if str(pp) != ":memory:" and real_state in pp.resolve().parents:
            pp = tmp_path / "isolated_metrics" / pp.name
        orig_init(self, pp)

    monkeypatch.setattr(MetricsStore, "__init__", _init)


@pytest.fixture(autouse=True)
def _no_system_proxy(monkeypatch):
    """Тесты не ходят через прокси машины разработчика.

    httpx берёт прокси из urllib.request.getproxies(): переменные *_proxy в любом регистре
    (ALL_PROXY=socks5h://... без socksio роняет уже httpx.AsyncClient()), а если их нет —
    на macOS из системных настроек сети. Поэтому убираем переменные и ставим NO_PROXY=*.
    """
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
