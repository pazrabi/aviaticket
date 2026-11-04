

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
