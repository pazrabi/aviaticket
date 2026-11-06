from datetime import date

import pytest

from avia.config import SiteConfig
from avia.extract import ExtractError, parse_offers
from avia.formatting import format_report, money
from avia.models import Query, SiteResult
from avia.parsing import ParseError, parse_date, parse_query
from avia.sites import build_url

TODAY = date(2026, 10, 27)


def test_parse_query_russian_names():
    q = parse_query("Москва Алматы 10.12", TODAY)
    assert (q.origin, q.destination, q.depart, q.adults) == ("MOW", "ALA", date(2026, 12, 10), 1)


def test_parse_query_iata_and_passengers():
    q = parse_query("mow tas 2026-12-10 2", TODAY)
    assert q.destination == "TAS" and q.adults == 2


def test_date_rolls_to_next_year():
    assert parse_date("05.01", TODAY) == date(2027, 1, 5)


@pytest.mark.parametrize("bad", ["Москва", "Москва Москва 10.12", "Москва Атлантида 10.12", "MOW ALA 31.02"])
def test_parse_errors(bad):
    with pytest.raises(ParseError):
        parse_query(bad, TODAY)


def test_aviasales_url():
    site = SiteConfig("aviasales", "https://www.aviasales.ru/search/{origin}{ddmm}{destination}{adults}")
    assert build_url(site, Query("MOW", "ALA", date(2026, 12, 10))) == "https://www.aviasales.ru/search/MOW1012ALA1"


def test_parse_offers_with_fences_and_junk():
    raw = '```json\n[{"price": "12 340", "currency": "rub", "airline": "S7", "transfers": 0},\n {"price": null}, {"price": 0}]\n```'
    offers = parse_offers(raw, "aviasales")
    assert len(offers) == 1
    assert offers[0].price == 12340 and offers[0].currency == "RUB" and offers[0].transfers == 0


def test_parse_offers_rejects_prose():
    with pytest.raises(ExtractError):
        parse_offers("Извините, не получилось", "x")


def test_format_report_escapes_and_reports_errors():
    q = Query("MOW", "ALA", date(2026, 12, 10))
    ok = SiteResult("aviasales", True, parse_offers('[{"price": 9990, "airline": "A<b>"}]', "aviasales"), page_url="https://x/?a=1&b=2")
    bad = SiteResult("tutu", False, [], "сайт показал капчу")
    text = format_report(q, [ok, bad])
    assert "A&lt;b&gt;" in text
    assert "9 990 ₽" in text
    assert "пока недоступен" in text  # капчу человеку не показываем, детали про телефоны ему не нужны
    assert "капч" not in text and "телефон" not in text
    assert "a=1&amp;b=2" in text


def _settings(**kw):
    from avia.config import Settings

    base = dict(sites=[], ssh_host="root@host", ssh_key="/k/key", agy_account="acc4",
                agy_proxy="http://127.0.0.1:3105", gemini_model="gemini-3.8-flash-low")
    base.update(kw)
    return Settings(**base)


def test_ssh_command_shape():
    from avia.extract import ssh_command

    cmd = ssh_command(_settings())
    assert cmd[0] == "ssh" and "BatchMode=yes" in cmd
    assert cmd[cmd.index("-i") + 1] == "/k/key"
    assert cmd[-2] == "root@host"
    assert cmd[-1] == (
        "runuser -u pool -- /opt/gemini-pool/bin/avia-llm acc4 http://127.0.0.1:3105 gemini-3.8-flash-low"
    )


def test_ssh_command_without_model_uses_dash():
    from avia.extract import ssh_command

    assert ssh_command(_settings(gemini_model=None))[-1].endswith("http://127.0.0.1:3105 -")


@pytest.mark.parametrize("field,value", [("agy_account", "acc4; rm -rf /"), ("agy_proxy", "http://x:1 $(id)"), ("gemini_model", "m`id`")])
def test_ssh_command_rejects_shell_metacharacters(field, value):
    from avia.extract import ssh_command

    with pytest.raises(ExtractError):
        ssh_command(_settings(**{field: value}))


def test_ssh_command_requires_host_and_proxy():
    from avia.extract import ssh_command

    with pytest.raises(ExtractError):
        ssh_command(_settings(ssh_host=""))


def test_parse_agy_output_json_and_plain():
    from avia.extract import parse_agy_output

    assert parse_agy_output('{"response": "[1]", "stats": {}}') == "[1]"
    assert parse_agy_output('{"result": "[2]"}') == "[2]"
    assert parse_agy_output("[3]") == "[3]"


def test_fit_bytes_cuts_cyrillic_safely():
    from avia.extract import fit_bytes

    s = "Москва" * 50_000
    out = fit_bytes(s, 100_000)
    assert len(out.encode("utf-8")) <= 100_000
    assert s.startswith(out)


def test_money():
    assert money(1234567, "KZT") == "1 234 567 ₸"


def test_travelpayouts_parse():
    from avia.parsers.travelpayouts import parse_prices_for_dates

    q = Query("MOW", "ALA", date(2026, 12, 10), adults=2)
    payload = {
        "success": True,
        "currency": "rub",
        "data": [
            {"price": 15000, "airline": "KC", "departure_at": "2026-12-10T10:05:00+03:00",
             "duration": 245, "transfers": 0, "link": "/search/MOW1012ALA1?t=abc"},
            {"price": 9000, "airline": "DV", "departure_at": "2026-12-10T23:40:00+03:00", "transfers": 1},
            {"price": None},
        ],
    }
    offers = parse_prices_for_dates(payload, q, marker="12345")
    assert [o.price for o in offers] == [18000, 30000]  # цена за одного × 2 пассажира, по возрастанию
    cheap, other = offers
    assert cheap.currency == "RUB" and cheap.depart_time == "23:40" and cheap.transfers == 1
    assert other.duration == "4ч 05м" and other.depart_time == "10:05"
    assert other.url.startswith("https://www.aviasales.ru/search/MOW1012ALA1") and "marker=12345" in other.url
    assert "marker=12345" in cheap.url


def test_travelpayouts_error_payload():
    from avia.parsers.travelpayouts import parse_prices_for_dates

    with pytest.raises(RuntimeError):
        parse_prices_for_dates({"success": False, "error": "bad token"}, Query("MOW", "ALA", date(2026, 12, 10)))


def test_registry_has_no_unregistered_parser_by_default():
    from avia.parsers import PARSERS

    assert "travelpayouts" not in PARSERS  # api-источник, не парсер страницы


def test_pipeline_reports_missing_parser_and_missing_token():
    import asyncio

    from avia.config import Settings
    from avia.pipeline import search

    api = SiteConfig("cache", "", source="api", parser="travelpayouts")
    s = Settings(sites=[api])
    res = asyncio.run(search(Query("MOW", "ALA", date(2026, 12, 10)), s))
    assert not res[0].ok and "TP_TOKEN" in res[0].error


def test_unavailable_sites_go_last_without_warning_sign():
    from avia.formatting import is_unavailable

    q = Query("MOW", "ALA", date(2026, 12, 10))
    cap = SiteResult("onetwotrip", False, [], "сайт показал капчу на всех телефонах, владельцу нужно пройти её руками", captcha=True)
    other = SiteResult("tutu", False, [], "таймаут")
    ok = SiteResult("aviasales", True, parse_offers('[{"price": 9990}]', "aviasales"))
    assert is_unavailable(cap) and not is_unavailable(other) and not is_unavailable(ok)
    text = format_report(q, [cap, other, ok])
    assert text.index("таймаут") < text.index("пока недоступен")
    assert "⚠️" in text and "💤" in text
