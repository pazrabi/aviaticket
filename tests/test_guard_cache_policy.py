"""Пустые и неполные ответы сайта не кэшируются; глубокий поиск не делит кэш с быстрым."""
import asyncio

from avia.guard import SearchGuard


def _run(coro):
    return asyncio.run(coro)


def _calls_factory(results):
    calls = {"n": 0}

    async def factory():
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return r

    return factory, calls


def test_empty_result_not_cached():
    g = SearchGuard(min_interval=0)
    factory, calls = _calls_factory([([], "u", False), (["o"], "u", False)])
    cache_if = lambda r: bool(r[0]) and not r[2]

    async def go():
        a, ca = await g.run("s", "q", factory, live=True, cache_if=cache_if)
        b, cb = await g.run("s", "q", factory, live=True, cache_if=cache_if)
        return a, ca, b, cb

    a, ca, b, cb = _run(go())
    assert a[0] == [] and not ca
    assert b[0] == ["o"] and not cb
    assert calls["n"] == 2


def test_partial_result_not_cached_but_full_is():
    g = SearchGuard(min_interval=0)
    factory, calls = _calls_factory([(["o"], "u", True), (["o", "p"], "u", False)])
    cache_if = lambda r: bool(r[0]) and not r[2]

    async def go():
        await g.run("s", "q", factory, live=True, cache_if=cache_if)
        _, c2 = await g.run("s", "q", factory, live=True, cache_if=cache_if)
        _, c3 = await g.run("s", "q", factory, live=True, cache_if=cache_if)
        return c2, c3

    c2, c3 = _run(go())
    assert not c2 and c3
    assert calls["n"] == 2


def test_default_still_caches_everything():
    g = SearchGuard(min_interval=0)
    factory, calls = _calls_factory([([], "u", False)])

    async def go():
        await g.run("s", "q", factory, live=True)
        return await g.run("s", "q", factory, live=True)

    _, cached = _run(go())
    assert cached and calls["n"] == 1
