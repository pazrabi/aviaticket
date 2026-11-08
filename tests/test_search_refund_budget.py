"""П29 в kt1-all: возврат попытки рядом с бюджетом поисков (kt1-budget) и общим try (kt1-p0).

Признак «списано» ставится там, где try_use_search стоит под общим try из kt1-p0:
- отказ по бюджету бота — до списания: попытка не списана и не возвращается;
- SQLite упал в самом try_use_search — ничего не списано, возвращать нечего;
- списали, а поиск не начался (исключение дальше в том же try) — попытка возвращается;
- возврат попытки не возвращает место в часовом бюджете бота: телефоны работу сделали.

Живет только в kt1-all: в kt1-refund нет бюджета, в kt1-budget нет возврата.
"""
from __future__ import annotations

import sqlite3

import pytest

from avia.dialog import Step
from tests.test_search_refund import (  # noqa: F401 — clock: autouse-часы на полдень, нужны и здесь
    PERSON_ID,
    SPENT_BEFORE,
    FakeSearch,
    all_down,
    clock,
    deep,
    live,
    make_bot,
    sent_texts,
    today_count,
)

BUDGET_TEXT = "дневной лимит не списан"
START_FAILED_TEXT = "⚠️ Не получилось начать поиск, попробуйте ещё раз."
OTHER_ID = 777


def spy_refunds(bot, monkeypatch) -> list[int]:
    """Подменяет users.refund_search: пишет вызовы и вызывает настоящий возврат."""
    calls: list[int] = []
    real = bot.users.refund_search

    def spy(uid, *args, **kwargs):
        calls.append(uid)
        return real(uid, *args, **kwargs)

    monkeypatch.setattr(bot.users, "refund_search", spy)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_budget_refusal_neither_charges_nor_refunds(tmp_path, monkeypatch, run):
    bot = make_bot(tmp_path, search_budget_per_hour=1)
    bot._note_search_budget(OTHER_ID)  # бюджет на час занят другим человеком
    search = FakeSearch(all_down)
    monkeypatch.setattr("avia.bot.search", search)
    refunds = spy_refunds(bot, monkeypatch)

    await run(bot)

    assert search.calls == []
    assert any(BUDGET_TEXT in t for t in sent_texts(bot))
    assert today_count(bot) == SPENT_BEFORE
    assert refunds == []


@pytest.mark.asyncio
async def test_db_error_in_charge_under_common_try_refunds_nothing(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr("avia.bot.search", FakeSearch(all_down))
    refunds = spy_refunds(bot, monkeypatch)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bot.users, "try_use_search", locked)

    await live(bot)

    assert START_FAILED_TEXT in sent_texts(bot)
    assert refunds == []
    assert today_count(bot) == SPENT_BEFORE
    assert bot.dialog.sessions[PERSON_ID].step != Step.SEARCHING


@pytest.mark.asyncio
async def test_charged_but_search_not_started_refunds_once(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, search_budget_per_hour=5)
    search = FakeSearch(all_down)
    monkeypatch.setattr("avia.bot.search", search)
    refunds = spy_refunds(bot, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError("бюджет сломался")

    monkeypatch.setattr(bot, "_note_search_budget", broken)

    await live(bot)

    assert search.calls == []
    assert START_FAILED_TEXT in sent_texts(bot)
    assert refunds == [PERSON_ID]
    assert today_count(bot) == SPENT_BEFORE
    assert bot.dialog.sessions[PERSON_ID].step != Step.SEARCHING


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [live, deep], ids=["live", "deep"])
async def test_refund_keeps_hourly_budget_spent(tmp_path, monkeypatch, run):
    bot = make_bot(tmp_path, search_budget_per_hour=5)
    monkeypatch.setattr("avia.bot.search", FakeSearch(all_down))
    refunds = spy_refunds(bot, monkeypatch)

    await run(bot)

    assert refunds == [PERSON_ID]
    assert today_count(bot) == SPENT_BEFORE
    assert len(bot._budget_times) == 1
