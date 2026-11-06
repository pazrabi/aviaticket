"""Сбор страницы выдачи через Playwright.

Отдаём наружу два источника данных:
  * видимый текст страницы после того как выдача догрузилась;
  * JSON-ответы, которые страница сама запрашивала (если сайт отдаёт цены XHR-ом,
    это надёжнее и дешевле, чем разбирать текст).
"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
import re
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

from .config import SiteConfig

logger = logging.getLogger(__name__)

JSON_URL_HINT = re.compile(r"search|ticket|price|offer|fare|avia|flight|result", re.I)
MAX_JSON_BYTES = 400_000
MAX_RESULTS_BYTES = 8_000_000
RESULTS_URL_MARKERS = (
    "/search/v3.2/results",
    "/api/avia/search/results",
)
BLOCK_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "вы не робот",
    "access denied",
    "are you a human",
    "fab_chlg",  # Ozon: страница-заглушка «нет соединения» с номером инцидента
)
IS_OVER_RE = re.compile(r'"is_over"\s*:\s*(true\b|"true"|1\b)', re.I)
LAST_UPDATE_ZERO_RE = re.compile(r'"last_update_timestamp"\s*:\s*0\b')
YANDEX_CONT_NULL_RE = re.compile(r'"cont"\s*:\s*null')
YANDEX_PROGRESS_RE = re.compile(r'"progress"\s*:\s*\{[^}]*"current"\s*:\s*(\d+)[^}]*"all"\s*:\s*(\d+)')
CAPTCHA_URL_RE = re.compile(r"/captcha/|smartcaptcha", re.I)

CAPTCHA_DOM_JS = """() => {
    const pattern = /captcha|smartcaptcha/i;
    for (const el of document.querySelectorAll('*')) {
        const isFrame = el.tagName.toLowerCase() === 'iframe';
        for (const attr of el.attributes) {
            const name = attr.name.toLowerCase();
            const val = attr.value;
            if (isFrame || name === 'src' || name === 'class' || name === 'id' || name.startsWith('data-')) {
                if (pattern.test(name) || pattern.test(val)) {
                    return true;
                }
            }
        }
    }
    return false;
}"""

CAPTCHA_DOM_SELECTOR = (
    "iframe[src*='captcha' i], iframe[src*='smartcaptcha' i], "
    "[class*='captcha' i], [class*='smartcaptcha' i], "
    "[id*='captcha' i], [id*='smartcaptcha' i], "
    "[src*='captcha' i], [src*='smartcaptcha' i]"
)


@dataclass
class Snapshot:
    url: str
    text: str
    json_payloads: list[tuple[str, str]] = field(default_factory=list)
    blocked: bool = False
    payload_times: list[float | None] = field(default_factory=list)
    woke: bool | None = None
    t_first_match: float | None = None


def is_blocked(text: str) -> bool:
    """Определяет, заблокирована ли страница капчей или сообщением об ограничении доступа."""
    low = text.lower()
    return any(m in low for m in BLOCK_MARKERS) and len(text) < 3000


def should_save_response(url: str, length: int) -> bool:
    """Решает, нужно ли сохранять JSON-ответ по URL и длине тела.

    Для URL из RESULTS_URL_MARKERS сохраняются ответы от 0 до MAX_RESULTS_BYTES.
    Для остальных URL проверяется совпадение с ключевыми словами и длина более 200 байт
    (не превышающая MAX_JSON_BYTES).
    """
    if any(marker in url for marker in RESULTS_URL_MARKERS):
        return 0 <= length <= MAX_RESULTS_BYTES
    if not (0 <= length <= MAX_JSON_BYTES):
        return False
    return bool(JSON_URL_HINT.search(url)) and length > 200


def has_is_over(body: str) -> bool:
    """Проверяет наличие признака завершения выдачи ('is_over': true/1) в теле ответа."""
    return bool(IS_OVER_RE.search(body))


def is_search_over(payloads: list[tuple[str, str]]) -> bool:
    r"""Проверяет, пришел ли завершающий ответ для Aviasales или Яндекс Путешествий среди payloads.

    Aviasales: ответ для '/search/v3.2/results', где last_update_timestamp == 0 или is_over == true/1.
    Яндекс: ответ для '/api/avia/search/results', где "cont": null и (если есть progress) current == all.
    Поиск выполняется по строке без json.loads.
    """
    for url, body in payloads:
        if "/search/v3.2/results" in url:
            if LAST_UPDATE_ZERO_RE.search(body) or has_is_over(body):
                return True
        elif "/api/avia/search/results" in url:
            if YANDEX_CONT_NULL_RE.search(body):
                prog = YANDEX_PROGRESS_RE.search(body)
                if prog is None or int(prog.group(1)) == int(prog.group(2)):
                    return True
    return False


def is_captcha_url(url: str) -> bool:
    """Проверяет, содержит ли URL '/captcha/' или 'smartcaptcha' (без учёта регистра)."""
    return bool(CAPTCHA_URL_RE.search(url))


def has_search_tickets(payloads: list[tuple[str, str]] | tuple[str, str] | str) -> bool:
    """Проверяет, содержит ли хотя бы один ответ /search/v3.2/results непустой список билетов."""
    if isinstance(payloads, str):
        payloads = [("/search/v3.2/results", payloads)]
    elif isinstance(payloads, tuple) and len(payloads) == 2 and isinstance(payloads[0], str):
        payloads = [payloads]

    for url, body in payloads:
        if "/search/v3.2/results" not in url:
            continue
        if '"tickets"' not in body and "'tickets'" not in body:
            continue
        try:
            data = json.loads(body)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    tickets = item.get("tickets")
                    if isinstance(tickets, (list, dict)) and len(tickets) > 0:
                        return True
        except Exception:
            # Запасной критерий при некорректном/обрезанном JSON
            if re.search(r'"tickets"\s*:\s*\[\s*\{', body):
                return True
    return False


has_tickets = has_search_tickets


def decide_blocked(
    text: str | bool,
    captcha_seen: bool,
    captcha_dom: bool,
    has_tickets: bool,
) -> bool:
    """Чистая функция принятия решения о блокировке страницы.

    Snapshot.blocked = is_blocked(text) or captcha_seen_without_results or captcha_dom,
    где при этом если уже получен непустой список билетов, капча не считается блокировкой
    (могла быть пройдена автоматически или показана поверх готовой выдачи).
    """
    text_blocked = text if isinstance(text, bool) else is_blocked(text)
    return bool(text_blocked or ((captcha_seen or captcha_dom) and not has_tickets))


async def check_captcha_dom(page: object) -> bool:
    """Проверяет в DOM наличие iframe или элементов со свойствами captcha/smartcaptcha."""
    if hasattr(page, "evaluate") and callable(getattr(page, "evaluate")):
        try:
            res = page.evaluate(CAPTCHA_DOM_JS)
            if inspect.isawaitable(res):
                res = await res
            return bool(res)
        except Exception:
            pass
    if hasattr(page, "query_selector_all") and callable(getattr(page, "query_selector_all")):
        try:
            res = page.query_selector_all(CAPTCHA_DOM_SELECTOR)
            if inspect.isawaitable(res):
                res = await res
            return bool(res)
        except Exception:
            pass
    return False


def compact_json(payload: str, limit: int = 30_000) -> str:
    """Минифицируем JSON, чтобы он влез в промпт."""
    try:
        return json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))[:limit]
    except ValueError:
        return payload[:limit]


async def _safe_close(resource: object | None) -> None:
    """Безопасно закрывает ресурс (контекст или браузер), не маскируя исходную ошибку."""
    if resource is None or not hasattr(resource, "close"):
        return
    try:
        close_fn = getattr(resource, "close")
        if callable(close_fn):
            res = close_fn()
            if inspect.isawaitable(res):
                await res
    except Exception:
        pass


REPLAY_FETCH_JS = """async ({ url, method, headers, body }) => {
    try {
        const resp = await fetch(url, {
            method: method,
            headers: headers,
            body: body,
            credentials: 'include',
        });
        const text = await resp.text();
        return { status: resp.status, text: text };
    } catch (err) {
        return { status: 0, error: String(err) };
    }
}"""


async def _replay_post_request(
    page: object,
    replay_cfg: dict,
    captured_request: dict | None,
    payloads: list[tuple[str, str]],
) -> None:
    """Выполняет один повтор POST-запроса из контекста страницы с изменённым телом."""
    if not captured_request:
        logger.warning("Повтор запроса пропущен: подходящий POST-запрос не был перехвачен")
        return

    req_url = captured_request.get("url")
    req_method = captured_request.get("method", "POST")
    post_data = captured_request.get("post_data")
    content_type = captured_request.get("content_type", "")

    if not req_url:
        logger.warning("Повтор запроса пропущен: отсутствует URL")
        return

    if not post_data:
        logger.warning("Повтор запроса пропущен: отсутствует post_data")
        return

    try:
        data = json.loads(post_data)
    except Exception:
        logger.warning("Повтор запроса пропущен: post_data не является валидным JSON")
        return

    if not isinstance(data, dict):
        logger.warning("Повтор запроса пропущен: JSON в post_data не является словарём")
        return

    set_fields = replay_cfg.get("set")
    if isinstance(set_fields, dict):
        data.update(set_fields)

    try:
        new_body = json.dumps(data, ensure_ascii=False)
    except Exception:
        logger.warning("Повтор запроса пропущен: сбой сериализации JSON")
        return

    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type

    try:
        if not hasattr(page, "evaluate") or not callable(getattr(page, "evaluate")):
            logger.warning("Повтор запроса пропущен: page.evaluate недоступен")
            return

        res = page.evaluate(
            REPLAY_FETCH_JS,
            {
                "url": req_url,
                "method": req_method,
                "headers": headers,
                "body": new_body,
            },
        )
        if inspect.isawaitable(res):
            res = await res

        if not isinstance(res, dict):
            logger.warning("Повтор запроса: неожиданный результат evaluate")
            return

        status = res.get("status")
        if status != 200:
            logger.warning("Повтор запроса: HTTP-статус %s != 200", status)
            return

        text = res.get("text")
        if not isinstance(text, str):
            logger.warning("Повтор запроса: ответ не является строкой")
            return

        body_len = len(text.encode("utf-8"))
        if body_len > MAX_RESULTS_BYTES:
            logger.warning("Повтор запроса: размер ответа %d превышает MAX_RESULTS_BYTES", body_len)
            return

        payloads.append((req_url, text))
    except Exception as e:
        logger.warning("Повтор запроса: исключение при выполнении fetch (%s)", type(e).__name__)


def _window_args(headless: bool) -> list[str]:
    """Видимое окно уводим за край экрана свёрнутым, чтобы оно не мешало работе."""
    if headless:
        return []
    return ["--window-position=-3000,-3000", "--start-minimized"]


async def fetch_snapshot(
    url: str,
    site: SiteConfig,
    *,
    headless: bool = True,
    profile_dir: Path | None = None,
    debug_dir: Path | None = None,
) -> Snapshot:
    payloads: list[tuple[str, str]] = []
    text = ""
    final_url = url
    browser = None
    ctx = None
    captcha_seen = False
    captcha_dom = False

    async with async_playwright() as pw:
        try:
            if profile_dir is not None:
                p_dir = Path(profile_dir)
                p_dir.mkdir(parents=True, exist_ok=True)
                ctx = await pw.chromium.launch_persistent_context(
                    str(p_dir),
                    headless=headless,
                    args=_window_args(headless),
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1366, "height": 900},
                )
                browser = getattr(ctx, "browser", None)
            else:
                browser = await pw.chromium.launch(headless=headless, args=_window_args(headless))
                ctx = await browser.new_context(
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1366, "height": 900},
                )

            page = ctx.pages[0] if getattr(ctx, "pages", None) else await ctx.new_page()

            captured_request: dict[str, Any] | None = None
            if site.replay_post:
                url_contains = site.replay_post.get("url_contains", "")

                def on_request(req):
                    nonlocal captured_request
                    if captured_request is not None:
                        return
                    method = getattr(req, "method", "")
                    if callable(method):
                        method = method()
                    if str(method).upper() != "POST":
                        return
                    req_url = getattr(req, "url", "")
                    if callable(req_url):
                        req_url = req_url()
                    if url_contains and url_contains not in str(req_url):
                        return
                    headers = getattr(req, "headers", {})
                    if callable(headers):
                        headers = headers()
                    content_type = ""
                    if isinstance(headers, dict):
                        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
                    post_data = getattr(req, "post_data", None)
                    if callable(post_data):
                        post_data = post_data()
                    if isinstance(post_data, (bytes, bytearray)):
                        try:
                            post_data = post_data.decode("utf-8")
                        except Exception:
                            post_data = post_data.decode("utf-8", "replace")
                    captured_request = {
                        "url": str(req_url),
                        "method": str(method),
                        "post_data": post_data,
                        "content_type": str(content_type),
                    }

                page.on("request", on_request)

            async def on_response(resp):
                nonlocal captcha_seen
                try:
                    resp_url = getattr(resp, "url", "")
                    if is_captcha_url(resp_url):
                        captcha_seen = True
                        return
                    headers = getattr(resp, "headers", {})
                    ctype = headers.get("content-type", "") if isinstance(headers, dict) else ""
                    if "json" not in ctype:
                        return
                    body = await resp.body()
                    if should_save_response(resp_url, len(body)):
                        payloads.append((resp_url, body.decode("utf-8", "replace")))
                except Exception:
                    pass  # ответ мог закрыться вместе со страницей

            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if site.wait_selector:
                try:
                    await page.wait_for_selector(site.wait_selector, timeout=45_000)
                except Exception:
                    pass

            # Опрос результатов: для URL с '/search/v3.2/results' опрашиваем накопленные
            # payloads с интервалом poll_interval_ms до появления признака завершения
            # ("last_update_timestamp": 0 или 'is_over': true), либо до таймаута settle_ms.
            poll_interval_ms = 250
            elapsed_ms = 0
            while elapsed_ms < site.settle_ms:
                if is_search_over(payloads):
                    break
                step = min(poll_interval_ms, site.settle_ms - elapsed_ms)
                if step <= 0:
                    break
                await page.wait_for_timeout(step)
                elapsed_ms += step

            for _ in range(site.scrolls):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1200)

            text = await page.inner_text("body")
            final_url = page.url
            captcha_dom = await check_captcha_dom(page)

            if site.replay_post:
                has_t = has_search_tickets(payloads)
                blocked_now = decide_blocked(text, captcha_seen, captcha_dom, has_t)
                if not (blocked_now or captcha_seen or captcha_dom or is_blocked(text)):
                    await _replay_post_request(page, site.replay_post, captured_request, payloads)
                else:
                    logger.warning("Повтор запроса пропущен: обнаружена капча или блокировка")

            if debug_dir:
                d_dir = Path(debug_dir)
                d_dir.mkdir(parents=True, exist_ok=True)
                stem = re.sub(r"\W+", "_", site.name)
                await page.screenshot(path=str(d_dir / f"{stem}.png"), full_page=False)
                (d_dir / f"{stem}.txt").write_text(text, encoding="utf-8")

        finally:
            await _safe_close(ctx)
            if browser is not ctx:
                await _safe_close(browser)

    has_t = has_search_tickets(payloads)
    blocked = decide_blocked(text, captcha_seen, captcha_dom, has_t)
    return Snapshot(url=final_url, text=text, json_payloads=payloads, blocked=blocked)
