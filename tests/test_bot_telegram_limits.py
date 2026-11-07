from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from avia.bot import Bot, WaitMessageUpdater
from avia.config import Settings
from avia.criteria import SearchCriteria
from avia.dialog import Reply, SearchRequest
from avia.models import Offer, Query, SiteResult
from avia.scheduler import build_notification
from avia.subscriptions import Subscription

# по заданию КТ1: после 429 не больше 3 повторов
MAX_RETRIES = 3
SLOW_CHAT = 101
FAST_CHAT = 202


def tg_ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def tg_429(retry_after: float) -> httpx.Response:
    return httpx.Response(429, json={"ok": False, "error_code": 429,
                                     "description": f"Too Many Requests: retry after {retry_after}",
                                     "parameters": {"retry_after": retry_after}})


def method_of(req: httpx.Request) -> str:
    return req.url.path.rsplit("/", 1)[-1]


def make_bot(handler, tmp_path, sleep=None, **settings_kw) -> tuple[Bot, list[float]]:
    settings = Settings(sites=[], tg_token="T0KEN", tg_allowed_ids={1, SLOW_CHAT, FAST_CHAT},
                        storage_dir=tmp_path, **settings_kw)
    bot = Bot(settings)
    bot.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)
        if sleep is not None:
            await sleep(sec)

    bot._sleep = fake_sleep
    return bot, slept


@pytest.mark.asyncio
async def test_send_message_delivered_after_retry_after_pause(tmp_path):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return tg_429(7)
        return tg_ok({"message_id": 5})

    bot, slept = make_bot(handler, tmp_path)
    res = await bot.send(1, "<b>12 345 ₽</b>", reply_markup={"inline_keyboard": []})
    assert res == {"message_id": 5}
    assert slept == [7.0]
    assert [b["text"] for b in bodies] == ["<b>12 345 ₽</b>", "<b>12 345 ₽</b>"]
    assert bodies[-1]["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.asyncio
async def test_edit_message_text_delivered_after_retry_after_pause(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((method_of(req), json.loads(req.content)))
        if len(seen) == 1:
            return tg_429(3)
        return tg_ok(True)

    bot, slept = make_bot(handler, tmp_path)
    await bot._process_actions(1, [Reply(text="<b>Ещё варианты</b>", edit=True)], message_id=77)
    assert slept == [3.0]
    # правка доставлена после паузы, а не заменена новым сообщением
    assert [m for m, _ in seen] == ["editMessageText", "editMessageText"]
    assert seen[-1][1]["message_id"] == 77
    assert seen[-1][1]["text"] == "<b>Ещё варианты</b>"


@pytest.mark.asyncio
async def test_gives_up_after_three_retries_and_logs_error(tmp_path, caplog):
    methods = []

    def handler(req: httpx.Request) -> httpx.Response:
        methods.append(method_of(req))
        return tg_429(1)

    bot, slept = make_bot(handler, tmp_path)
    with caplog.at_level(logging.WARNING, logger="avia.bot"):
        with pytest.raises(RuntimeError, match="429"):
            await bot.call("sendMessage", chat_id=1, text="hi")
    assert methods == ["sendMessage"] * (1 + MAX_RETRIES)
    assert slept == [1.0] * MAX_RETRIES
    errors = [r for r in caplog.records if r.name == "avia.bot" and r.levelno >= logging.ERROR]
    assert errors and "sendMessage" in errors[-1].getMessage()


@pytest.mark.asyncio
async def test_retry_after_is_capped(tmp_path):
    n = []

    def handler(req: httpx.Request) -> httpx.Response:
        n.append(1)
        if len(n) == 1:
            return tg_429(3600)
        return tg_ok(True)

    bot, slept = make_bot(handler, tmp_path)
    await bot.call("answerCallbackQuery", callback_query_id="x")
    assert slept == [60.0]


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    bot, slept = make_bot(handler, tmp_path)
    with pytest.raises(RuntimeError, match="chat not found"):
        await bot.call("sendMessage", chat_id=1, text="hi")
    assert slept == []


@pytest.mark.asyncio
async def test_retry_pause_does_not_hold_other_peoples_search(tmp_path, monkeypatch):
    """Пока один человек ждёт retry_after, поиск другого проходит до карточки."""
    delivered: list[tuple[str, int, str]] = []
    card_429 = []

    def handler(req: httpx.Request) -> httpx.Response:
        method, body = method_of(req), json.loads(req.content)
        text = body.get("text", "")
        if method == "sendMessage" and body["chat_id"] == SLOW_CHAT and text.startswith("КАРТОЧКА") and not card_429:
            card_429.append(1)
            return tg_429(30)
        delivered.append((method, body.get("chat_id"), text))
        if method == "sendMessage":
            return tg_ok({"message_id": len(delivered)})
        return tg_ok(True)

    paused, resume = asyncio.Event(), asyncio.Event()

    async def hold(_sec: float) -> None:
        paused.set()
        await resume.wait()

    async def mock_search(q, s):
        return [SiteResult("aviasales", True, [Offer("aviasales", 5000)])]

    monkeypatch.setattr("avia.bot.search", mock_search)
    monkeypatch.setattr("avia.bot.build_compact_report",
                        lambda q, res, criteria, **kw: ([f"КАРТОЧКА {q.origin}"], [], []))
    bot, slept = make_bot(handler, tmp_path, sleep=hold)

    def request(origin: str) -> SearchRequest:
        q = Query(origin=origin, destination="LED", depart=date(2026, 12, 10))
        return SearchRequest(query=q, baggage_mode="any", criteria=SearchCriteria())

    slow = asyncio.create_task(bot._run_search(SLOW_CHAT, request("MOW")))
    pause_wait = asyncio.create_task(paused.wait())
    try:
        await asyncio.wait({slow, pause_wait}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert paused.is_set(), "карточка после 429 не ждёт retry_after"

        await asyncio.wait_for(bot._run_search(FAST_CHAT, request("KZN")), timeout=5)
        assert ("sendMessage", FAST_CHAT, "КАРТОЧКА KZN") in delivered
        assert not slow.done()
        assert ("sendMessage", SLOW_CHAT, "КАРТОЧКА MOW") not in delivered

        resume.set()
        await asyncio.wait_for(slow, timeout=5)
        assert ("sendMessage", SLOW_CHAT, "КАРТОЧКА MOW") in delivered
        assert slept == [30.0]
    finally:
        resume.set()
        pause_wait.cancel()
        if not slow.done():
            slow.cancel()


@pytest.mark.asyncio
async def test_wait_message_progress_edit_does_not_wait_out_429(tmp_path):
    """Правка «Ищу билеты…» косметическая: на 429 не ждём, иначе стоит on_site_done и весь поиск."""
    methods = []

    def handler(req: httpx.Request) -> httpx.Response:
        methods.append(method_of(req))
        return tg_429(30)

    bot, slept = make_bot(handler, tmp_path)
    updater = WaitMessageUpdater(bot=bot, chat_id=1, message_id=10, sites=[SimpleNamespace(name="aviasales")],
                                 header="h", footer="f")
    await asyncio.wait_for(updater.on_site_done(SiteResult("aviasales", True, [Offer("aviasales", 5000)])), timeout=5)
    assert slept == []
    assert methods == ["editMessageText"]


@pytest.mark.asyncio
async def test_api_base_override_and_default(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return tg_ok(True)

    bot, _ = make_bot(handler, tmp_path)
    await bot.call("getMe")
    bot2, _ = make_bot(handler, tmp_path, tg_api_base="http://127.0.0.1:8787/")
    await bot2.call("getMe")
    await bot2.send_photo(1, b"png")
    assert seen == [
        "https://api.telegram.org/botT0KEN/getMe",
        "http://127.0.0.1:8787/botT0KEN/getMe",
        "http://127.0.0.1:8787/botT0KEN/sendPhoto",
    ]


@pytest.mark.asyncio
async def test_send_splits_long_html_into_balanced_chunks(tmp_path):
    texts = []

    def handler(req: httpx.Request) -> httpx.Response:
        texts.append(json.loads(req.content)["text"])
        return tg_ok({"message_id": len(texts)})

    bot, _ = make_bot(handler, tmp_path)
    text = "<blockquote>" + "\n".join(f"{i}. <b>12 345 ₽</b> &amp; багаж" for i in range(600)) + "</blockquote>"
    await bot.send(1, text, reply_markup={"inline_keyboard": []})
    assert len(texts) > 1
    assert all(len(t) <= 4096 for t in texts)
    assert all(t.startswith("<blockquote>") and t.endswith("</blockquote>") for t in texts)


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_html_rejected(tmp_path):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: can't parse entities: unsupported start tag"})
        return tg_ok({"message_id": 9})

    bot, _ = make_bot(handler, tmp_path)
    res = await bot.send(1, "<b>Цена</b> 5 000 ₽ &amp; <x>")
    assert res == {"message_id": 9}
    assert bodies[-1]["text"] == "Цена 5 000 ₽ & "
    assert "parse_mode" not in bodies[-1]


def _broken_markup(text: str) -> str | None:
    for tag in ("b", "i", "a", "blockquote"):
        if len(re.findall(rf"<{tag}[ >]", text)) != text.count(f"</{tag}>"):
            return f"<{tag}> не закрыт"
    if re.search(r"<[^>]*$", text):
        return "обрезан тег"
    if re.search(r"&[#0-9A-Za-z]*$", text):
        return "обрезана сущность"
    return None


def test_long_notification_is_cut_on_tag_boundary():
    sub = Subscription(id=1, chat_id=1, origin="MOW", destination="LED", depart=date(2026, 12, 1))
    offer = Offer(site="aviasales", price=5000, airline="Победа", url="https://x.test/?a=1&b=2")
    # причина такой длины, что граница 4096 проходит по каждому символу карточки билета
    for n in range(3600, 4100):
        text = build_notification(sub, offer, ["x" * n])
        assert len(text) <= 4096
        assert _broken_markup(text) is None, (n, _broken_markup(text), text[-200:])
