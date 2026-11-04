"""Тесты для модуля src/avia/samples.py (сохранение образцов, суточный лимит, ротация, gzip, parser-check)."""
from __future__ import annotations

from datetime import date
import gzip
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.parsers import PARSERS
from avia.samples import (
    check_samples,
    check_samples_with_cooldown,
    load_sample,
    run_parser_check_cli,
    save_sample,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures"


def test_save_sample_daily_limit_and_search_type(tmp_path):
    """Суточный лимит: не чаще одного раза в сутки на сайт и тип поиска (one-way / round-trip)."""
    samples_dir = tmp_path / "samples"
    snap = Snapshot(url="https://example.com/search", text="Билеты", json_payloads=[("url1", '{"ok": 1}')])

    q_ow = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1), return_date=None)
    q_rt = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1), return_date=date(2026, 12, 10))

    t0 = 1000000.0

    # 1. Сохраняем one-way
    p1 = save_sample("aviasales", snap, q_ow, samples_dir, now=t0)
    assert p1 is not None and p1.exists()

    # 2. Повторное сохранение one-way через 2 часа -> должно пропустить (None)
    p2 = save_sample("aviasales", snap, q_ow, samples_dir, now=t0 + 7200)
    assert p2 is None

    # 3. Сохранение round-trip в тот же день -> должно сохраниться (другой тип поиска)
    p3 = save_sample("aviasales", snap, q_rt, samples_dir, now=t0 + 7200)
    assert p3 is not None and p3.exists()

    # 4. Сохранение one-way спустя 25 часов (> 24 ч) -> должно сохраниться
    p4 = save_sample("aviasales", snap, q_ow, samples_dir, now=t0 + 25 * 3600)
    assert p4 is not None and p4.exists()


def test_save_sample_max_three_rotation(tmp_path):
    """Хранить не больше 3 последних образцов на сайт (ротация)."""
    samples_dir = tmp_path / "samples"
    snap = Snapshot(url="https://example.com", text="Поиск", json_payloads=[])
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    t0 = 1000000.0
    saved_paths = []
    # Сохраняем 5 образцов с интервалом в 2 дня
    for i in range(5):
        t = t0 + i * 2 * 86400
        p = save_sample("tutu", snap, q, samples_dir, now=t)
        assert p is not None
        saved_paths.append(p)

    site_files = sorted((samples_dir / "tutu").glob("sample_*.json.gz"))
    assert len(site_files) == 3

    # Самые старые 2 файла должны быть удалены
    assert not saved_paths[0].exists()
    assert not saved_paths[1].exists()
    # Последние 3 файла должны существовать
    assert saved_paths[2].exists()
    assert saved_paths[3].exists()
    assert saved_paths[4].exists()


def test_save_sample_gzip_compression_and_load(tmp_path):
    """Образцы сохраняются в формате gzip (.json.gz) и корректно восстанавливаются load_sample."""
    samples_dir = tmp_path / "samples"
    snap = Snapshot(
        url="https://example.com/avia",
        text="Выдача авиабилетов Москва Сочи",
        json_payloads=[("https://example.com/api", '{"price": 5000}')],
    )
    q = Query(origin="MOW", destination="AER", depart=date(2026, 12, 15), return_date=date(2026, 12, 22), adults=2)

    p = save_sample("kupibilet", snap, q, samples_dir)
    assert p is not None
    assert p.name.endswith(".json.gz")

    # Проверяем, что файл действительно сжат gzip (магические байты \x1f\x8b)
    raw = p.read_bytes()
    assert raw[:2] == b"\x1f\x8b"

    # Загружаем через load_sample
    site, loaded_snap, loaded_q = load_sample(p)
    assert site == "kupibilet"
    assert loaded_snap.url == snap.url
    assert loaded_snap.text == snap.text
    assert loaded_snap.json_payloads == [("https://example.com/api", '{"price": 5000}')]
    assert loaded_q.origin == "MOW"
    assert loaded_q.destination == "AER"
    assert loaded_q.depart == date(2026, 12, 15)
    assert loaded_q.return_date == date(2026, 12, 22)
    assert loaded_q.adults == 2


def test_save_sample_redacts_tokens(tmp_path):
    """В сохранённых образцах токен заменяется на [REDACTED]."""
    samples_dir = tmp_path / "samples"
    snap = Snapshot(
        url="https://example.com/api?token=SECRET_12345&flight=10",
        text="Текст с access_token=SECRET_ACCESS_KEY внутри",
        json_payloads=[("https://example.com/res?api_key=SECRET_API_KEY", '{"data": 1}')],
    )
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    p = save_sample("onetwotrip", snap, q, samples_dir)
    assert p is not None

    decompressed = gzip.decompress(p.read_bytes()).decode("utf-8")
    assert "SECRET_12345" not in decompressed
    assert "SECRET_ACCESS_KEY" not in decompressed
    assert "SECRET_API_KEY" not in decompressed
    assert "[REDACTED]" in decompressed


def test_save_sample_size_limit(tmp_path):
    """Ограничение размера: при слишком большом тексте он обрезается."""
    samples_dir = tmp_path / "samples"
    huge_text = "A" * 4_000_000  # 4 МБ текста
    snap = Snapshot(url="https://example.com", text=huge_text, json_payloads=[])
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 1))

    p = save_sample("aviasales", snap, q, samples_dir, max_bytes=100_000)
    assert p is not None
    # Размер несжатых данных не должен превышать 100_000 байт
    decompressed = gzip.decompress(p.read_bytes())
    assert len(decompressed) <= 100_000


def test_parser_check_on_fixtures(tmp_path, capsys):
    """parser-check на реальных фикстурах из tests/fixtures проверяет парсеры и печатает результаты."""
    samples_dir = tmp_path / "samples"

    # 1. Aviasales
    avi_json = (FIXTURES_DIR / "aviasales" / "results_mow_ala.json").read_text(encoding="utf-8")
    avi_snap = Snapshot(
        url="https://www.aviasales.ru/search/MOW1012ALA1",
        text="Выдача Aviasales",
        json_payloads=[("https://www.aviasales.ru/search/v3.2/results?search_id=test", avi_json)],
    )
    save_sample("aviasales", avi_snap, Query("MOW", "ALA", date(2026, 12, 10)), samples_dir)

    # 2. Kupibilet
    kupi_json = (FIXTURES_DIR / "kupibilet" / "search_quick_mow_ala.json").read_text(encoding="utf-8")
    kupi_snap = Snapshot(
        url="https://www.kupibilet.ru/search",
        text="Выдача Kupibilet",
        json_payloads=[("https://api-rs-lb.kupibilet.ru/frontend_search", kupi_json)],
    )
    save_sample("kupibilet", kupi_snap, Query("MOW", "ALA", date(2026, 12, 10)), samples_dir)

    # 3. OneTwoTrip
    ott_json = (FIXTURES_DIR / "onetwotrip" / "search_ow_kuf_evn_0212.json").read_text(encoding="utf-8")
    ott_snap = Snapshot(
        url="https://www.onetwotrip.com/ru/f/search/0212KUFEVN?sc=E&p=1_0_0",
        text="Выдача OneTwoTrip",
        json_payloads=[("https://www.onetwotrip.com/_avia-search-proxy/search/v3", ott_json)],
    )
    save_sample("onetwotrip", ott_snap, Query("KUF", "EVN", date(2026, 12, 2)), samples_dir)

    # 4. Tutu
    tutu_json = (FIXTURES_DIR / "tutu" / "offers_first_mow_ala.json").read_text(encoding="utf-8")
    tutu_snap = Snapshot(
        url="https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=491-12122026-117",
        text="Выдача Tutu",
        json_payloads=[("https://offers-api.tutu.ru/avia/offers/v2", tutu_json)],
    )
    save_sample("tutu", tutu_snap, Query("MOW", "ALA", date(2026, 12, 12)), samples_dir)

    # 5. Yandex
    ya_json = (FIXTURES_DIR / "yandex_travel" / "results_cont0_mow_ala.json").read_text(encoding="utf-8")
    ya_snap = Snapshot(
        url="https://travel.yandex.ru/avia/search/result/?adult_seats=1&fromId=c213&toId=c22177&when=2026-12-12",
        text="Выдача Яндекс",
        json_payloads=[("https://travel.yandex.ru/api/avia/search/results?qid=test&cont=0", ya_json)],
    )
    save_sample("yandex_travel", ya_snap, Query("MOW", "ALA", date(2026, 12, 12)), samples_dir)

    # Проверяем check_samples
    results = check_samples(samples_dir)
    assert "aviasales" in results and results["aviasales"]["ok"] is True
    assert results["aviasales"]["offers_count"] > 0
    assert results["aviasales"]["min_price"] is not None

    assert "kupibilet" in results and results["kupibilet"]["ok"] is True
    assert results["kupibilet"]["offers_count"] > 0

    assert "onetwotrip" in results and results["onetwotrip"]["ok"] is True
    assert "tutu" in results and results["tutu"]["ok"] is True
    assert "yandex_travel" in results and results["yandex_travel"]["ok"] is True

    # Проверяем CLI вывод и код возврата 0
    code = run_parser_check_cli(samples_dir)
    assert code == 0

    out = capsys.readouterr().out
    assert "aviasales:" in out
    assert "kupibilet:" in out
    assert "вар." in out
    assert "мин. цена" in out


def test_parser_check_failure_empty_variants(tmp_path, capsys):
    """Сайт с 0 вариантов отмечается как провал, и CLI возвращает код 1."""
    samples_dir = tmp_path / "samples"

    # Образец с пустым JSON, из которого парсер вернёт 0 вариантов
    bad_snap = Snapshot(url="https://example.com", text="Пусто", json_payloads=[("url", "{}")])
    save_sample("aviasales", bad_snap, Query("MOW", "ALA", date(2026, 12, 1)), samples_dir)

    results = check_samples(samples_dir)
    assert "aviasales" in results
    assert results["aviasales"]["ok"] is False
    assert results["aviasales"]["offers_count"] == 0

    code = run_parser_check_cli(samples_dir)
    assert code == 1

    out = capsys.readouterr().out
    assert "aviasales: 0 вариантов (провал)" in out


def test_check_samples_with_cooldown(tmp_path):
    """check_samples_with_cooldown при сбое сообщает админам с кулдауном и шлёт восстановление."""
    from avia.metrics import MetricsStore
    metrics = MetricsStore(tmp_path / "metrics.sqlite")
    samples_dir = tmp_path / "samples"

    # Создаём плохой образец (0 вариантов)
    bad_snap = Snapshot(url="https://example.com", text="Пусто", json_payloads=[("url", "{}")])
    save_sample("aviasales", bad_snap, Query("MOW", "ALA", date(2026, 12, 1)), samples_dir)

    t0 = 500000.0
    # 1. Первый сбой -> формирует сообщение
    fails1 = check_samples_with_cooldown(samples_dir, metrics, cooldown_hours=6, now=t0)
    assert len(fails1) == 1
    assert "aviasales" in fails1[0]
    assert "0 вариантов (провал)" in fails1[0]

    # 2. Повторная проверка в пределах кулдауна (через 1 час) -> сообщений нет
    fails2 = check_samples_with_cooldown(samples_dir, metrics, cooldown_hours=6, now=t0 + 3600)
    assert len(fails2) == 0

    # 3. Заменяем плохой образец на валидный фикстурный
    avi_json = (FIXTURES_DIR / "aviasales" / "results_mow_ala.json").read_text(encoding="utf-8")
    good_snap = Snapshot(
        url="https://www.aviasales.ru/search/MOW1012ALA1",
        text="Выдача Aviasales",
        json_payloads=[("https://www.aviasales.ru/search/v3.2/results?search_id=test", avi_json)],
    )
    # Удаляем старый образец и пишем новый
    for f in (samples_dir / "aviasales").glob("*"):
        f.unlink()
    save_sample("aviasales", good_snap, Query("MOW", "ALA", date(2026, 12, 10)), samples_dir, now=t0 + 7200)

    # Проверка должна вернуть сообщение о восстановлении
    rec_msgs = check_samples_with_cooldown(samples_dir, metrics, cooldown_hours=6, now=t0 + 7300)
    assert len(rec_msgs) == 1
    assert "восстановился" in rec_msgs[0]

    # Следующая проверка -> нет сообщений
    rec_msgs2 = check_samples_with_cooldown(samples_dir, metrics, cooldown_hours=6, now=t0 + 7400)
    assert len(rec_msgs2) == 0
