"""Модель прогнозирования: упадёт ли минимальная цена маршрута на 5% и больше до вылета."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
import json
import logging
from pathlib import Path
import pickle
import statistics
import time
from typing import Any

from .history import PriceHistory, _normalize_date
from .models import Query

log = logging.getLogger(__name__)

MIN_TRAIN_ROWS = 500
MIN_TRAIN_ROUTES = 20
MIN_TEST_ROWS = 100
PRICE_DROP_THRESHOLD = 0.05  # 5% и более

FEATURE_NAMES = [
    "days_to_departure",
    "depart_weekday",
    "obs_weekday",
    "depart_month",
    "obs_month",
    "is_weekend_depart",
    "is_weekend_obs",
    "price_rel_median",
    "trend_3d",
    "trend_7d",
    "spread_p25_median",
    "samples",
    "trip_duration_days",
    "is_round_trip",
]


@dataclass
class Observation:
    origin: str
    destination: str
    depart: date
    return_date: date | None
    adults: int
    obs_date: date
    min_price: int
    p25_price: float
    median_price: float
    samples: int
    label: int = 0
    subsequent_min: int | None = None
    subsequent_last: int | None = None

    @property
    def flight_key(self) -> tuple[str, str, str, str, int]:
        ret_str = self.return_date.isoformat() if self.return_date else ""
        return (self.origin, self.destination, self.depart.isoformat(), ret_str, self.adults)


def _query_route_stats(history: PriceHistory | None, obs: Observation) -> dict | None:
    """Запрашивает срез статистики маршрута на момент замера obs без утечки будущего."""
    if history is None:
        return None
    q = Query(
        origin=obs.origin,
        destination=obs.destination,
        depart=obs.depart,
        return_date=obs.return_date,
        adults=obs.adults,
    )
    obs_ts = datetime.combine(obs.obs_date, dt_time(23, 59, 59)).timestamp()
    try:
        return history.route_stats(q, now=obs_ts)
    except Exception:
        return None


_UNSET = object()


def extract_features(
    obs: Observation,
    history: PriceHistory | None = None,
    precomputed_stats: Any = _UNSET,
) -> list[float]:
    """Извлекает числовые признаки для замера строго без утечки будущих данных."""
    days_to_dep = (obs.depart - obs.obs_date).days
    dep_w = obs.depart.weekday()
    obs_w = obs.obs_date.weekday()
    dep_m = obs.depart.month
    obs_m = obs.obs_date.month
    is_w_dep = 1.0 if dep_w >= 5 else 0.0
    is_w_obs = 1.0 if obs_w >= 5 else 0.0

    # Медиана, тренды, разброс и число замеров маршрута на момент замера (без заглядывания в будущее)
    if precomputed_stats is not _UNSET:
        stats = precomputed_stats
    elif history is not None:
        stats = _query_route_stats(history, obs)
    else:
        stats = None

    if stats and stats.get("median") and stats["median"] > 0:
        price_rel_med = float(obs.min_price) / float(stats["median"])
        p25_val = stats.get("p25")
        if p25_val is None or p25_val <= 0:
            p25_val = stats["median"]
        spread = float(p25_val) / float(stats["median"])
    elif obs.median_price > 0:
        price_rel_med = float(obs.min_price) / float(obs.median_price)
        spread = float(obs.p25_price) / float(obs.median_price)
    else:
        price_rel_med = 1.0
        spread = 1.0

    t3 = stats.get("trend_3d") if stats else None
    trend_3d = 1.0 if t3 == "up" else (-1.0 if t3 == "down" else 0.0)

    t7 = stats.get("trend_7d") if stats else None
    trend_7d = 1.0 if t7 == "up" else (-1.0 if t7 == "down" else 0.0)

    if stats and stats.get("samples") is not None:
        samples_val = float(max(1, stats["samples"]))
    else:
        samples_val = float(max(1, obs.samples))

    if obs.return_date:
        trip_days = float(max(0, (obs.return_date - obs.depart).days))
        is_rt = 1.0
    else:
        trip_days = 0.0
        is_rt = 0.0

    return [
        float(days_to_dep),
        float(dep_w),
        float(obs_w),
        float(dep_m),
        float(obs_m),
        float(is_w_dep),
        float(is_w_obs),
        float(price_rel_med),
        float(trend_3d),
        float(trend_7d),
        float(spread),
        float(samples_val),
        float(trip_days),
        float(is_rt),
    ]


def prepare_dataset(
    history: PriceHistory,
    today: date | None = None,
) -> list[Observation]:
    """Строит учебную таблицу с динамическим расчётом ответов (только depart < today)."""
    today_d = _normalize_date(today) or date.today()
    today_str = today_d.isoformat()

    # Сворачиваем неагрегированные дни из price_history в route_daily
    try:
        history.rebuild_all_route_daily()
    except Exception:
        log.exception("ошибка обновления route_daily перед формированием датасета")

    with history._connect() as db:
        rows = db.execute(
            "SELECT origin, destination, depart, return_date, adults, day, min_price, p25_price, median_price, samples "
            "FROM route_daily "
            "WHERE depart < ? "
            "ORDER BY origin, destination, depart, return_date, adults, day ASC",
            (today_str,),
        ).fetchall()

    if not rows:
        return []

    # Группируем по рейсам (origin, destination, depart, return_date, adults)
    flights: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        dep_d = _normalize_date(r[2])
        if dep_d is None or dep_d >= today_d:
            continue
        obs_d = _normalize_date(r[5])
        if obs_d is None or obs_d >= dep_d:
            continue  # замеры в день вылета или позже не являются прогнозом до вылета

        ret_d = _normalize_date(r[3])
        key = (r[0], r[1], dep_d.isoformat(), ret_d.isoformat() if ret_d else "", int(r[4]))
        if key not in flights:
            flights[key] = []
        flights[key].append({
            "origin": r[0],
            "destination": r[1],
            "depart": dep_d,
            "return_date": ret_d,
            "adults": int(r[4]),
            "obs_date": obs_d,
            "min_price": int(r[6]),
            "p25_price": float(r[7]),
            "median_price": float(r[8]),
            "samples": int(r[9]),
        })

    observations: list[Observation] = []
    for flight_rows in flights.values():
        flight_rows.sort(key=lambda x: x["obs_date"])
        n = len(flight_rows)
        for i, row in enumerate(flight_rows):
            p_obs = row["min_price"]
            subsequent = [flight_rows[j]["min_price"] for j in range(i + 1, n)]

            if subsequent:
                sub_min = min(subsequent)
                sub_last = subsequent[-1]
                # Упадёт минимум на 5%: (p_obs - sub_min) / p_obs >= 0.05
                drop_ratio = (p_obs - sub_min) / float(p_obs) if p_obs > 0 else 0.0
                label = 1 if drop_ratio >= PRICE_DROP_THRESHOLD else 0
            else:
                sub_min = p_obs
                sub_last = p_obs
                label = 0

            observations.append(
                Observation(
                    origin=row["origin"],
                    destination=row["destination"],
                    depart=row["depart"],
                    return_date=row["return_date"],
                    adults=row["adults"],
                    obs_date=row["obs_date"],
                    min_price=p_obs,
                    p25_price=row["p25_price"],
                    median_price=row["median_price"],
                    samples=row["samples"],
                    label=label,
                    subsequent_min=sub_min,
                    subsequent_last=sub_last,
                )
            )

    return observations


def split_train_val(
    observations: list[Observation],
    train_ratio: float = 0.75,
) -> tuple[list[Observation], list[Observation]]:
    """Разбивка по времени/маршрутам: рейсы целиком в train или целиком в val, val — более поздние вылеты."""
    if not observations:
        return [], []

    flight_keys = sorted(
        list({obs.flight_key for obs in observations}),
        key=lambda k: (k[2], k[0], k[1], k[3], k[4]),
    )
    k_total = len(flight_keys)
    if k_total <= 1:
        return list(observations), []

    cutoff_idx = max(1, min(k_total - 1, int(round(k_total * train_ratio))))
    train_flights = set(flight_keys[:cutoff_idx])
    val_flights = set(flight_keys[cutoff_idx:])

    train_obs = [obs for obs in observations if obs.flight_key in train_flights]
    val_obs = [obs for obs in observations if obs.flight_key in val_flights]
    return train_obs, val_obs


def compute_metrics(
    y_true: list[int],
    y_pred: list[int],
    obs_prices: list[int],
    sub_min_prices: list[int],
    sub_last_prices: list[int],
) -> dict[str, float]:
    """Метрики классификации и средняя упущенная выгода при следовании совету."""
    total = len(y_true)
    if total == 0:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "missed_opportunity_pct": 0.0,
        }

    correct = sum(1 for yt, yp in zip(y_true, y_pred) if yt == yp)
    accuracy = correct / float(total)

    tp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 1)
    fp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 0 and yp == 1)
    fn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 0)

    precision = tp / float(tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / float(tp + fn) if (tp + fn) > 0 else 0.0

    regrets: list[float] = []
    for yt, yp, p_obs, p_sub_min, p_sub_last in zip(
        y_true, y_pred, obs_prices, sub_min_prices, sub_last_prices
    ):
        if p_obs <= 0:
            regrets.append(0.0)
            continue
        if yp == 0:
            # Совет: брать сейчас. Если цена упала (yt == 1), упустили скидку
            if yt == 1 and p_sub_min < p_obs:
                regrets.append(((p_obs - p_sub_min) / float(p_obs)) * 100.0)
            else:
                regrets.append(0.0)
        else:
            # Совет: подождать. Если цена не упала, а выросла, переплатили
            if yt == 0 and p_sub_last > p_obs:
                regrets.append(((p_sub_last - p_obs) / float(p_obs)) * 100.0)
            else:
                regrets.append(0.0)

    missed_opp = statistics.mean(regrets) if regrets else 0.0

    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "missed_opportunity_pct": round(missed_opp, 2),
    }


def is_model_better(
    model_m: dict[str, float],
    b1_m: dict[str, float],
    b2_m: dict[str, float],
) -> bool:
    """Модель лучше, только если обыгрывает обе базовые стратегии на отложенных данных."""
    # Обыгрывание по точности или по упущенной выгоде без просадки других метрик
    acc_m = model_m["accuracy"]
    loss_m = model_m["missed_opportunity_pct"]

    beats_b1 = (acc_m > b1_m["accuracy"] and loss_m <= b1_m["missed_opportunity_pct"]) or (
        loss_m < b1_m["missed_opportunity_pct"] and acc_m >= b1_m["accuracy"]
    )
    beats_b2 = (acc_m > b2_m["accuracy"] and loss_m <= b2_m["missed_opportunity_pct"]) or (
        loss_m < b2_m["missed_opportunity_pct"] and acc_m >= b2_m["accuracy"]
    )

    return beats_b1 and beats_b2


def train_and_eval(
    history: PriceHistory,
    forecast_dir: Path | str,
    today: date | None = None,
) -> dict[str, Any]:
    """Обучает модель HistGradientBoostingClassifier и сравнивает с базовыми стратегиями."""
    f_dir = Path(forecast_dir)
    f_dir.mkdir(parents=True, exist_ok=True)
    today_d = _normalize_date(today) or date.today()

    # 1. Собираем учебную выборку
    observations = prepare_dataset(history, today=today_d)
    train_obs, val_obs = split_train_val(observations)

    train_rows = len(train_obs)
    val_rows = len(val_obs)
    train_routes = len({obs.flight_key for obs in train_obs})
    val_routes = len({obs.flight_key for obs in val_obs})
    routes_count = len({(obs.origin, obs.destination) for obs in observations})
    departed_cnt = history.count_departed_flights(today_d)

    # 2. Проверка порогов достаточности данных
    if train_rows < MIN_TRAIN_ROWS or train_routes < MIN_TRAIN_ROUTES or val_rows < MIN_TEST_ROWS:
        if train_rows < MIN_TRAIN_ROWS:
            verdict = f"рано: нужно ещё {MIN_TRAIN_ROWS - train_rows} ответов"
        elif train_routes < MIN_TRAIN_ROUTES:
            verdict = f"рано: нужно ещё {MIN_TRAIN_ROUTES - train_routes} маршрутов+вылетов"
        elif val_rows < MIN_TEST_ROWS:
            verdict = f"рано: нужно ещё {MIN_TEST_ROWS - val_rows} ответов в проверке"
        else:
            verdict = "рано: недостаточно данных"
        report = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "samples_total": len(observations),
            "train_rows": train_rows,
            "test_rows": val_rows,
            "train_routes": train_routes,
            "test_routes": val_routes,
            "routes_count": routes_count,
            "departed_flights_count": departed_cnt,
            "verdict": verdict,
            "metrics": None,
        }
        _save_report(report, f_dir)
        _remove_model(f_dir)
        return report

    # 3. Проверяем наличие scikit-learn (ленивый импорт) ТОЛЬКО при достаточном объёме данных
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        report = {
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "samples_total": len(observations),
            "train_rows": train_rows,
            "test_rows": val_rows,
            "train_routes": train_routes,
            "test_routes": val_routes,
            "routes_count": routes_count,
            "departed_flights_count": departed_cnt,
            "verdict": "sklearn не установлен",
            "metrics": None,
        }
        _save_report(report, f_dir)
        _remove_model(f_dir)
        return report

    # 4. Извлекаем признаки (предварительный расчёт stats без дублирования запросов)
    train_stats = [_query_route_stats(history, obs) for obs in train_obs]
    X_train = [
        extract_features(obs, history=history, precomputed_stats=st)
        for obs, st in zip(train_obs, train_stats)
    ]
    y_train = [obs.label for obs in train_obs]

    val_stats = [_query_route_stats(history, obs) for obs in val_obs]
    X_val = [
        extract_features(obs, history=history, precomputed_stats=st)
        for obs, st in zip(val_obs, val_stats)
    ]
    y_val = [obs.label for obs in val_obs]

    # 5. Обучаем HistGradientBoostingClassifier
    model = HistGradientBoostingClassifier(
        random_state=42,
        max_iter=100,
        min_samples_leaf=10,
    )
    model.fit(X_train, y_train)

    # Предсказания модели
    y_pred_model = [int(p) for p in model.predict(X_val)]

    # 6. Базовые стратегии
    # Базовая 1: «всегда брать сейчас» (прогноз «не упадёт» -> 0)
    y_pred_b1 = [0] * len(val_obs)

    # Базовая 2: правила из advice.py (используем уже вычисленный val_stats)
    from . import advice

    y_pred_b2: list[int] = []
    for obs, st in zip(val_obs, val_stats):
        d = (obs.depart - obs.obs_date).days
        adv = advice.build_advice(st, d)
        # Переводим совет «ждать» в 1 («упадёт»), остальное — 0
        if adv and adv.verdict == "wait":
            y_pred_b2.append(1)
        else:
            y_pred_b2.append(0)

    # 7. Расчёт метрик
    obs_prices = [obs.min_price for obs in val_obs]
    sub_min_prices = [obs.subsequent_min or obs.min_price for obs in val_obs]
    sub_last_prices = [obs.subsequent_last or obs.min_price for obs in val_obs]

    m_model = compute_metrics(y_val, y_pred_model, obs_prices, sub_min_prices, sub_last_prices)
    m_b1 = compute_metrics(y_val, y_pred_b1, obs_prices, sub_min_prices, sub_last_prices)
    m_b2 = compute_metrics(y_val, y_pred_b2, obs_prices, sub_min_prices, sub_last_prices)

    better = is_model_better(m_model, m_b1, m_b2)
    verdict = "лучше правил" if better else "не лучше правил"

    report = {
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "samples_total": len(observations),
        "train_rows": train_rows,
        "test_rows": val_rows,
        "train_routes": train_routes,
        "test_routes": val_routes,
        "routes_count": routes_count,
        "departed_flights_count": departed_cnt,
        "verdict": verdict,
        "metrics": {
            "model": m_model,
            "baseline_always_buy": m_b1,
            "baseline_rules": m_b2,
        },
    }

    _save_report(report, f_dir)
    if better:
        _save_model(model, f_dir)
    else:
        _remove_model(f_dir)

    return report


def predict_drop_prob(
    history: PriceHistory,
    q: Query,
    stats: dict | None = None,
    forecast_dir: Path | str | None = None,
    now: float | None = None,
) -> float | None:
    """Возвращает вероятность падения цены (0.0..1.0), только если модель есть и verdict «лучше правил»."""
    now_ts = time.time() if now is None else float(now)
    obs_d = datetime.fromtimestamp(now_ts).date()
    dep_d = _normalize_date(q.depart)
    if dep_d is None or dep_d <= obs_d:
        return None

    if forecast_dir is None:
        return None
    f_dir = Path(forecast_dir)
    rep = load_report(f_dir)
    if not rep or rep.get("verdict") not in ("лучше правил", "модель лучше"):
        return None

    model_path = f_dir / "model.pkl"
    if not model_path.exists():
        return None

    try:
        import sklearn  # noqa: F401
    except ImportError:
        return None

    try:
        with open(model_path, "rb") as f:
            model = pickle.load(f)
    except Exception:
        log.exception("ошибка загрузки модели прогноза")
        return None

    ret_d = _normalize_date(q.return_date)

    if stats is None and history is not None:
        try:
            stats = history.route_stats(q, now=now_ts)
        except Exception:
            stats = None

    min_p = stats.get("last_price") or stats.get("min") or 0 if stats else 0
    p25_p = float(stats.get("p25") or min_p) if stats else float(min_p)
    med_p = float(stats.get("median") or min_p) if stats else float(min_p)
    samples = int(stats.get("samples", 1)) if stats else 1

    dummy_obs = Observation(
        origin=q.origin,
        destination=q.destination,
        depart=dep_d,
        return_date=ret_d,
        adults=q.adults,
        obs_date=obs_d,
        min_price=int(min_p),
        p25_price=p25_p,
        median_price=med_p,
        samples=samples,
    )

    feat = extract_features(dummy_obs, history=history, precomputed_stats=stats)
    try:
        probs = model.predict_proba([feat])
        return float(probs[0][1])
    except Exception:
        log.exception("ошибка предсказания модели прогноза")
        return None


def load_report(forecast_dir: Path | str | None) -> dict[str, Any] | None:
    """Загружает сохранённый отчёт report.json из каталога прогноза."""
    if not forecast_dir:
        return None
    p = Path(forecast_dir) / "report.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_report(report: dict[str, Any], forecast_dir: Path) -> None:
    p = forecast_dir / "report.json"
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    except Exception:
        log.exception("ошибка сохранения отчёта прогноза")


def _save_model(model: Any, forecast_dir: Path) -> None:
    p = forecast_dir / "model.pkl"
    try:
        with open(p, "wb") as f:
            pickle.dump(model, f)
    except Exception:
        log.exception("ошибка сохранения модели прогноза")


def _remove_model(forecast_dir: Path) -> None:
    p = forecast_dir / "model.pkl"
    if p.exists():
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
