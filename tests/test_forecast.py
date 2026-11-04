"""Тесты модуля прогнозирования цен (src/avia/forecast.py)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys

import pytest

from avia.advice import Advice, format_advice
from avia import cli
from avia import forecast
from avia.history import PriceHistory
from avia.models import Query


def _insert_daily_row(
    history: PriceHistory,
    origin: str,
    destination: str,
    depart: str,
    return_date: str,
    adults: int,
    day: str,
    min_price: int,
    p25: float | None = None,
    median: float | None = None,
    samples: int = 1,
) -> None:
    p25_val = float(p25 if p25 is not None else min_price)
    med_val = float(median if median is not None else min_price)
    with history._connect() as db:
        db.execute(
            "INSERT INTO route_daily "
            "(origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(origin, destination, depart, return_date, adults, day) DO UPDATE SET "
            "min_price = excluded.min_price, p25_price = excluded.p25_price, "
            "median_price = excluded.median_price, samples = excluded.samples",
            (origin, destination, depart, return_date, adults, day, min_price, p25_val, med_val, samples),
        )


def _insert_price_raw(
    history: PriceHistory,
    origin: str,
    destination: str,
    depart: str,
    return_date: str | None,
    adults: int,
    price: int,
    obs_ts: float,
    search_id: str = "s1",
) -> None:
    with history._connect() as db:
        db.execute(
            "INSERT INTO price_history "
            "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at, days_to_departure, search_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"off_{obs_ts}", "test_site", origin, destination, depart, return_date, adults, price, "RUB", obs_ts, 10, search_id),
        )


class TestForecastDatasetAndLabels:
    def test_calculate_labels_and_departed_flights_filter(self, tmp_path):
        """Проверка расчёта ответов: label=1 при падении >= 5%, только depart < today."""
        hist = PriceHistory(tmp_path / "history.sqlite")
        today = date(2026, 11, 4)

        # Рейс 1: вылетел в прошлом (2026-10-10 < today), цена упала с 10000 до 9000 (10% >= 5%)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-01", 10000)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-03", 9000)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-06", 12000)

        # Рейс 2: вылетел в прошлом (2026-10-15 < today), цена упала только на 3% (10000 -> 9700)
        _insert_daily_row(hist, "MOW", "AER", "2026-10-15", "", 1, "2026-10-01", 10000)
        _insert_daily_row(hist, "MOW", "AER", "2026-10-15", "", 1, "2026-10-05", 9700)

        # Рейс 3: будущий рейс (2026-12-01 >= today) — не должен попасть в обучение
        _insert_daily_row(hist, "MOW", "SVX", "2026-12-01", "", 1, "2026-10-01", 10000)
        _insert_daily_row(hist, "MOW", "SVX", "2026-12-01", "", 1, "2026-10-05", 5000)

        obs_list = forecast.prepare_dataset(hist, today=today)

        # Проверяем, что будущий рейс не попал в датасет
        assert not any(obs.destination == "SVX" for obs in obs_list)

        # Рейс 1:
        r1_obs = [o for o in obs_list if o.destination == "LED"]
        assert len(r1_obs) == 3
        # Замер 1: min_price 10000, последующий min 9000 -> label=1
        assert r1_obs[0].obs_date == date(2026, 10, 1)
        assert r1_obs[0].label == 1
        assert r1_obs[0].subsequent_min == 9000
        # Замер 2: min_price 9000, последующий 12000 -> label=0
        assert r1_obs[1].obs_date == date(2026, 10, 3)
        assert r1_obs[1].label == 0
        # Замер 3: последний замер перед вылетом -> label=0
        assert r1_obs[2].obs_date == date(2026, 10, 6)
        assert r1_obs[2].label == 0

        # Рейс 2:
        r2_obs = [o for o in obs_list if o.destination == "AER"]
        assert len(r2_obs) == 2
        # Замер 1: 10000 -> 9700 (3% < 5%) -> label=0
        assert r2_obs[0].label == 0
        assert r2_obs[1].label == 0

    def test_no_future_leakage_in_features(self, tmp_path):
        """Отсутствие утечки будущего: признаки замера зависят только от данных до замера."""
        hist = PriceHistory(tmp_path / "history.sqlite")
        dep_str = "2026-10-20"
        d_dep = date(2026, 10, 20)

        # День 1 (2026-10-01)
        ts1 = datetime(2026, 10, 1, 12, 0).timestamp()
        _insert_price_raw(hist, "MOW", "LED", dep_str, None, 1, 5000, ts1, "s1")
        _insert_daily_row(hist, "MOW", "LED", dep_str, "", 1, "2026-10-01", 5000)

        obs1 = forecast.Observation(
            origin="MOW",
            destination="LED",
            depart=d_dep,
            return_date=None,
            adults=1,
            obs_date=date(2026, 10, 1),
            min_price=5000,
            p25_price=5000.0,
            median_price=5000.0,
            samples=1,
        )

        feat_before = forecast.extract_features(obs1, history=hist)

        # Теперь добавляем будущие данные на день 2 (2026-10-05) с огромной ценой
        ts2 = datetime(2026, 10, 5, 12, 0).timestamp()
        _insert_price_raw(hist, "MOW", "LED", dep_str, None, 1, 50000, ts2, "s2")
        _insert_daily_row(hist, "MOW", "LED", dep_str, "", 1, "2026-10-05", 50000)

        feat_after = forecast.extract_features(obs1, history=hist)

        # Признаки для замера obs1 (день 1) должны быть абсолютно идентичны!
        assert feat_before == feat_after


class TestForecastSplit:
    def test_split_no_flight_leakage_and_temporal_order(self, tmp_path):
        """Разбивка без пересечения маршрутов+вылетов, в валидации более поздние вылеты."""
        hist = PriceHistory(tmp_path / "history.sqlite")
        today = date(2026, 11, 4)

        # Создаём 24 различных рейса с вылетами от 2026-08-01 до 2026-10-24
        for i in range(24):
            dep_date = date(2026, 8, 1) + timedelta(days=i * 3)
            dep_str = dep_date.isoformat()
            dest = f"D{i:02d}"
            # 3 замера на рейс
            for d_offset in (10, 5, 2):
                obs_d = dep_date - timedelta(days=d_offset)
                _insert_daily_row(
                    hist, "MOW", dest, dep_str, "", 1, obs_d.isoformat(), 5000 + i * 100
                )

        obs_list = forecast.prepare_dataset(hist, today=today)
        assert len(obs_list) == 24 * 3

        train_obs, val_obs = forecast.split_train_val(obs_list, train_ratio=0.75)

        train_flights = {o.flight_key for o in train_obs}
        val_flights = {o.flight_key for o in val_obs}

        # 1. Ни один рейс не пересекается
        assert train_flights.isdisjoint(val_flights)
        assert len(train_flights) + len(val_flights) == 24

        # 2. Вылеты в train строго не позже вылетов в val
        max_train_depart = max(o.depart for o in train_obs)
        min_val_depart = min(o.depart for o in val_obs)
        assert max_train_depart <= min_val_depart


class TestForecastVerdictAndSmallData:
    def test_verdict_early_on_empty_and_small_data(self, tmp_path):
        """Вердикт «рано» при малом объёме данных или пустой базе."""
        hist = PriceHistory(tmp_path / "history.sqlite")
        f_dir = tmp_path / "forecast"

        # Пустая база
        rep_empty = forecast.train_and_eval(hist, f_dir)
        assert rep_empty["verdict"].startswith("рано")
        assert rep_empty["metrics"] is None
        assert not (f_dir / "model.pkl").exists()
        assert (f_dir / "report.json").exists()

        # Мало строк (3 строки)
        today = date(2026, 11, 4)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-01", 10000)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-03", 9000)
        _insert_daily_row(hist, "MOW", "LED", "2026-10-10", "", 1, "2026-10-06", 12000)

        rep_small = forecast.train_and_eval(hist, f_dir, today=today)
        assert rep_small["verdict"].startswith("рано")
        assert "нужно ещё" in rep_small["verdict"]
        assert rep_small["train_rows"] < forecast.MIN_TRAIN_ROWS
        assert rep_small["metrics"] is None

    def test_work_without_sklearn(self, tmp_path, monkeypatch):
        """Без scikit-learn модуль не падает и возвращает статус «sklearn не установлен»."""
        hist = PriceHistory(tmp_path / "history.sqlite")
        f_dir = tmp_path / "forecast"

        # Симулируем отсутствие sklearn
        monkeypatch.setitem(sys.modules, "sklearn", None)
        monkeypatch.setitem(sys.modules, "sklearn.ensemble", None)

        rep = forecast.train_and_eval(hist, f_dir)
        assert rep["verdict"] == "sklearn не установлен"
        assert rep["metrics"] is None

        # Прогноз также возвращает None без падений
        prob = forecast.predict_drop_prob(hist, Query(origin="MOW", destination="LED", depart="2026-12-01"), forecast_dir=f_dir)
        assert prob is None


class TestForecastTrainingAndEvaluation:
    def test_model_better_on_strong_synthetic_signal(self, tmp_path):
        """Обучение на искусственном сигнале: модель обыгрывает базовые стратегии -> «лучше правил»."""
        pytest.importorskip("sklearn")
        hist = PriceHistory(tmp_path / "history.sqlite")
        f_dir = tmp_path / "forecast"
        today = date(2026, 11, 4)

        # Генерируем синтетическую историю: 30 рейсов, по 25 замеров каждый (750 строк)
        # Сигнал: если days_to_departure <= 8, цена гарантированно падает на 40% (label=1).
        # Если days_to_departure > 8, цена стабильна или растёт (label=0).
        for i in range(30):
            dep_date = date(2026, 8, 1) + timedelta(days=i * 3)
            dep_str = dep_date.isoformat()
            dest = f"CIT{i:02d}"

            # Замеры от 25 дней до 1 дня до вылета
            for d_left in range(25, 0, -1):
                obs_d = dep_date - timedelta(days=d_left)
                # До 8 дней цена 10000, после 8 дней падает до 5000
                price = 10000 if d_left > 8 else 5000
                _insert_daily_row(
                    hist, "MOW", dest, dep_str, "", 1, obs_d.isoformat(), price, median=10000
                )

        rep = forecast.train_and_eval(hist, f_dir, today=today)

        # Выборка достаточна: >= 500 train, >= 20 routes, >= 100 test
        assert rep["train_rows"] >= forecast.MIN_TRAIN_ROWS
        assert rep["train_routes"] >= forecast.MIN_TRAIN_ROUTES
        assert rep["test_rows"] >= forecast.MIN_TEST_ROWS

        # Модель должна легко уловить чёткий порог days_to_departure
        assert rep["verdict"] in ("лучше правил", "модель лучше")
        assert (f_dir / "model.pkl").exists()
        assert rep["metrics"] is not None

        m = rep["metrics"]["model"]
        b1 = rep["metrics"]["baseline_always_buy"]
        assert m["accuracy"] > b1["accuracy"]

        # Проверяем predict_drop_prob: модель загружается и выдаёт вероятность
        q = Query(origin="MOW", destination="CIT01", depart=date(2026, 11, 10))
        stats = {"last_price": 10000, "median": 10000, "samples": 20, "p25": 9000}
        prob = forecast.predict_drop_prob(hist, q, stats, forecast_dir=f_dir)
        assert prob is not None
        assert 0.0 <= prob <= 1.0

        # Проверяем формат совета людям: появляется строка с вероятностью
        adv = Advice(verdict="buy", text="Тест", confidence="высокая", model_prob=prob)
        lines = format_advice(adv)
        pct = int(round(prob * 100))
        assert f"Модель: вероятность, что подешевеет, {pct}%" in lines

    def test_model_not_better_on_constant_data(self, tmp_path):
        """Когда сигнал отсутствует (цена никогда не падает), модель не обыгрывает «всегда брать»."""
        pytest.importorskip("sklearn")
        hist = PriceHistory(tmp_path / "history.sqlite")
        f_dir = tmp_path / "forecast"
        today = date(2026, 11, 4)

        # 30 рейсов по 25 замеров: цена везде строго постоянна (10000), label всегда 0
        for i in range(30):
            dep_date = date(2026, 8, 1) + timedelta(days=i * 3)
            dep_str = dep_date.isoformat()
            dest = f"CON{i:02d}"
            for d_left in range(25, 0, -1):
                obs_d = dep_date - timedelta(days=d_left)
                _insert_daily_row(hist, "MOW", dest, dep_str, "", 1, obs_d.isoformat(), 10000)

        rep = forecast.train_and_eval(hist, f_dir, today=today)

        # Базовая стратегия 1 имеет 100% точность, модель не может быть строго лучше
        assert rep["verdict"] in ("не лучше правил", "модель не лучше правил")
        assert not (f_dir / "model.pkl").exists()

        # predict_drop_prob возвращает None
        q = Query(origin="MOW", destination="CON01", depart=date(2026, 11, 10))
        stats = {"last_price": 10000, "median": 10000, "samples": 20}
        prob = forecast.predict_drop_prob(hist, q, stats, forecast_dir=f_dir)
        assert prob is None


class TestForecastCLI:
    def test_cli_forecast_train_command(self, tmp_path, monkeypatch, capsys):
        """CLI-подкоманда forecast-train печатает отчёт в JSON."""
        monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("FORECAST_DIR", str(tmp_path / "forecast"))
        monkeypatch.setattr(sys, "argv", ["avia.cli", "forecast-train"])

        cli.main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "verdict" in data
        assert "train_rows" in data


class TestForecastBotIntegration:
    def test_should_run_forecast_training(self, tmp_path):
        """Проверка логики запуска обучения модели в боте по расписанию."""
        from avia.bot import Bot
        from avia.config import Settings
        settings = Settings(
            sites=[],
            tg_token="dummy",
            tg_allowed_ids={1},
            storage_dir=tmp_path / "storage",
            forecast_dir=tmp_path / "forecast",
            forecast_enabled=True,
            forecast_retrain_days=7,
        )
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        bot = Bot(settings)

        # Отчёта ещё нет -> запуск при старте
        assert bot._should_run_forecast_training() is True

        # Создаём свежий отчёт
        f_dir = settings.forecast_dir
        f_dir.mkdir(parents=True, exist_ok=True)
        report_file = f_dir / "report.json"
        report_file.write_text("{}", encoding="utf-8")

        # Отчёт свежий (< 7 дней) -> не запускаем
        assert bot._should_run_forecast_training() is False

        # Выключенный forecast_enabled -> False
        bot.s.forecast_enabled = False
        assert bot._should_run_forecast_training() is False
