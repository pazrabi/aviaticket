"""Тесты чтения полей хронологии (t, woke, t_first_match) из ответов droidhub."""
from __future__ import annotations

import httpx
import pytest

from avia.collector import Snapshot
from avia.config import Settings
from avia.droidhub import _fetch_device_snapshot


@pytest.mark.asyncio
async def test_snapshot_default_fields():
    """collector.Snapshot инициализируется с корректными значениями по умолчанию."""
    snap = Snapshot(url="https://example.com", text="test")
    assert snap.payload_times == []
    assert snap.woke is None
    assert snap.t_first_match is None


@pytest.mark.asyncio
async def test_droidhub_timing_fields_present():
    """Поддельный ответ хаба с t, woke, t_first_match корректно сохраняется в Snapshot."""
    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://example.com/search",
                "text": "Билеты",
                "finished": True,
                "woke": True,
                "t_first_match": 1.25,
                "json_responses": [
                    {
                        "url": "https://example.com/api/part1",
                        "body": '{"tickets": [{"price": 12000}]}',
                        "t": 2.1,
                    },
                    {
                        "url": "https://example.com/api/part2",
                        "body": '{"tickets": [{"price": 9000}]}',
                        "t": 5.4,
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    snap, finished = await _fetch_device_snapshot(
        settings, "realme", "https://example.com/search", client=client
    )

    assert finished is True
    assert snap.woke is True
    assert snap.t_first_match == 1.25
    assert len(snap.json_payloads) == 2
    assert snap.payload_times == [2.1, 5.4]


@pytest.mark.asyncio
async def test_droidhub_timing_fields_absent():
    """Ответ хаба без полей t, woke, t_first_match даёт None и не падает."""
    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "url": "https://example.com/search",
                "text": "Билеты",
                "finished": True,
                "json_responses": [
                    {
                        "url": "https://example.com/api/part1",
                        "body": '{"tickets": [{"price": 12000}]}',
                    },
                    {
                        "url": "https://example.com/api/part2",
                        "body": '{"tickets": [{"price": 9000}]}',
                        "t": "not_a_number",
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    snap, finished = await _fetch_device_snapshot(
        settings, "realme", "https://example.com/search", client=client
    )

    assert finished is True
    assert snap.woke is None
    assert snap.t_first_match is None
    assert len(snap.json_payloads) == 2
    assert snap.payload_times == [None, None]
