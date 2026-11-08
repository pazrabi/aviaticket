# КТ4, работа 4. Плохие снимки и одна политика пропуска кривых элементов

**Задание:** приёмщик, 2026-11-08, КТ4, работы 3–4 (`docs/audit/07_plan.md`, раздел П33 и таблица проверок КТ4).
**База:** `kt1-all` `74854fa` (весь КТ1 поверх `main` `9ba5ae3`). Ветка `kt4-parsers`, worktree `~/avia-scale/kt4-parsers`. Только локально: без push и PR.
**Порог плана:** на каждом плохом снимке — понятная ошибка, и `parser_watch` считает сбор неудачным, а не «0 вариантов» молча.

## Итог

| Коммит | Что |
|---|---|
| `434bc98` | `tests/test_parsers_negative.py` — RED |
| `8a61695` | политика пропуска: `ParsedOffers` и одна строка WARNING на разбор |
| `0ded938` | тесты по ревью: дубли, второй этап yandex, порции aviasales — RED на `8a61695`: 1 failed (tutu) |
| `c6cd49a` | tutu: дубль `id` не считается пропуском |
| этот | отчёт |

`git diff --stat 74854fa -- src tests` — 9 файлов, +686 −17. В том числе `src` — 8 файлов, +52 −17:

| Файл | Строк | Что |
|---|---|---|
| `src/avia/parsers/__init__.py` | +15 −1 | `ParsedOffers(list)`: офферы, `total`, `skipped` |
| `src/avia/pipeline.py` | +3 | `_process_snapshot`: строка WARNING, если `skipped > 0` |
| `src/avia/parsers/aviasales.py` | +5 −2 | счётчик билетов, `return ParsedOffers(...)` |
| `src/avia/parsers/kupibilet.py` | +4 −2 | то же, варианты |
| `src/avia/parsers/ozon_travel.py` | +5 −4 | то же, карточки; три выхода после JSON (с запасным разбором текста) |
| `src/avia/parsers/ttravel.py` | +4 −2 | то же, офферы всех ответов `startStreaming` |
| `src/avia/parsers/tutu.py` | +5 −2 | то же, предложения; дубль `id` — не пропуск |
| `src/avia/parsers/yandex_travel.py` | +11 −4 | то же, тарифы; два этапа (тариф → лучший по маршруту → билет) |
| `tests/test_parsers_negative.py` | +634 | 123 теста: 112 обычных, 11 `xfail(strict=True)` |

`parsers/travelpayouts.py` не тронут (`git diff 74854fa -- src/avia/parsers/travelpayouts.py` пуст).

## Как проверяли

Снимки — из `tests/fixtures/<сайт>/`, портятся в памяти: свежий `json.loads` фикстуры на каждый тест. Путь от снимка до `site_runs` — через `pipeline.search` (живой поиск, один сайт, один телефон `dev1`): `fetch_page_snapshot_any` подменён и отдаёт готовый снимок. Дальше — `MetricsStore.record_results("user", results)`, как в `bot._record_metrics`. База — временная `metrics.sqlite`, `debug_dir` — во временном каталоге. Статус `parser_watch` — через `get_parser_statuses` с `parser_watch_min_runs = 1`, то есть по одному сбору. Без сети и без телефонов.

## Что нашлось по каждому парсеру

Строки 1–3 одинаковы для всех шести сайтов (проверено на каждом):

| Снимок | Что сейчас | Порог |
|---|---|---|
| пустое содержимое: нет ответов; тело `""`, `{}`, `[]`, `null` | `[]`, без исключения | ✅ |
| капча (`snap.blocked = True`) | `site_runs`: `ok = 0`, `captcha = 1`; `parser_watch` — «сломан»; человек — «💤 <сайт>: пока недоступен · открыть выдачу» | ✅ |
| изменённый формат: страница > 200 символов, ключевые поля переименованы во всех элементах | `ParserEmptyError` «формат выдачи мог измениться, 0 вариантов при непустой странице»; `site_runs`: `ok = 0`; `parser_watch` — «сломан»; человек — «⚠️ <сайт>: формат выдачи мог измениться… · открыть выдачу». Теперь перед этим — строка «пропущено N из N элементов выдачи» | ✅ |
| один элемент без цены среди нормальных | парсер пропускает его, остальные на месте, сбор удачный (`ok = 1`). Раньше молча, теперь — одна строка WARNING | ✅ |
| короткая страница без капчи (`502 Bad Gateway\nnginx`, без JSON) | **«0 вариантов», сбор удачный** — см. ниже | ❌ `xfail` |

Что переименовано в «изменённом формате»: aviasales `proposals`, kupibilet `price`, ozon_travel `price`, ttravel `price`, tutu `offerVariants`, yandex_travel `prices`.

Мусор вместо времени у одного элемента. Чтобы испортить только его, рейс элемента скопирован в справочнике под новым ключом:

| Парсер | Что сейчас | Что увидит человек (после `_process_snapshot`) |
|---|---|---|
| aviasales | билет остаётся, `depart_time` и `depart_at` = «мусор» | билет отброшен `filter_wrong_dates` как «неверные даты» ✅ (по чужой причине) |
| yandex_travel | билет остаётся, `depart_at` = «мусор», `depart_time` = None | то же ✅ (по чужой причине) |
| kupibilet | билет остаётся, `depart_time` = «мусор» (и WARNING `ticket_hash` «ошибка парсинга departure_datetime») | **билет с временем «мусор»** ❌ `xfail` |
| ttravel | билет остаётся, `depart_time` = «мусор» | **билет с временем «мусор»** ❌ `xfail` |
| tutu | билет остаётся, `depart_time` = «мусор» | **билет с временем «мусор»** ❌ `xfail` |
| ozon_travel | билет остаётся без времени (`depart_time` = None) | **билет без времени** ❌ `xfail` |

Ни один парсер не теряет сайт из-за одного кривого элемента: исключение внутри элемента везде ловится и элемент пропускается. Чистого багфикса по этому пункту нет.

### Короткая страница без капчи (п. 3 задания — не правил)

Сейчас (`test_short_page_without_captcha_current_behaviour`, все 6 сайтов):

- `_process_snapshot`: 0 офферов, текст ≤ 200 символов — `ParserEmptyError` нет. `results_complete` = False, отсюда `partial = True` (`pipeline.py:339–342` на `74854fa`).
- `_from_browser`: ветка «неполная выдача без офферов» (`pipeline.py:826–871`). Повтор на другом телефоне — если он есть и укладывается в `live_safety_seconds`, иначе `return [], url, True`.
- `SiteResult(ok=True, offers=[], partial=True, error=None)` → `site_runs`: `ok = 1, partial = 1, offers = 0`.
- `parser_watch` (`watch_stats` → runs 1, ok 1, partial 1): статус «деградирует» (неполных 100 % > 60 %), но не «сломан». Доля «ок» растёт.
- `speed.py` считает такой сбор провалом («partial без билетов»): 3 подряд — карантин пары 600 с.
- Предохранитель сайта (`dispatcher._fuse_decide`) видит в `site_recent_outcomes` удачный сбор (`ok = 1`) и не срабатывает, пока идут такие страницы.
- Человек видит: «▫️ <сайт>: билетов не нашлось · открыть выдачу», ниже курсивом — «неполная выдача: сайт ещё догружал варианты».

### Находка: отладочный снимок не доживает до владельца

`_process_snapshot` сохраняет снимок в `debug_dir` перед `ParserEmptyError`. Но `_fetch` первым делом удаляет его (`pipeline.py:1205–1209` на `74854fa`, в ветке — `1208–1212`), ещё до решения о повторе. Удаляет и тогда, когда повтора не будет. На всех шести «изменённых форматах» `debug_dir` после поиска пуст. Тест — `test_renamed_format_keeps_debug_snapshot_when_no_retry` (`xfail`).

## Политика пропуска кривых элементов

**Одна политика для всех шести сайтов.** Кривой элемент — нет цены, битая структура, исключение при сборке — пропускается, остальные остаются. Так парсеры работали и раньше; поведение не менялось, менялся только лог. Новое — каждый парсер считает элементы выдачи и сколько из них не стало билетом. `pipeline._process_snapshot` пишет одну строку WARNING на полный разбор:

```
Сайт kupibilet: пропущено 1 из 8 элементов выдачи
```

Все элементы кривые на длинной странице: строка «пропущено N из N», дальше `ParserEmptyError`, как сейчас. Дубли (тот же билет дважды) — не пропуск.

**Почему общий помощник, а не шесть строк лога по месту.** Счётчик нужен в каждом парсере при любом варианте. Разница — где писать строку. В самом парсере она повторялась бы при каждом быстром разборе: при FAST_STOP `_evaluate_cb` → `_quick_parse_offers` зовёт парсер на каждом новом ответе, не чаще раза в секунду (`PARSER_MIN_INTERVAL`), то есть многократно за один сбор. Поэтому парсер только возвращает `ParsedOffers` — список с полями `total` и `skipped` (+15 строк в `parsers/__init__.py`). Строку пишет одно место — `_process_snapshot` (+3 строки). Быстрый разбор молчит (`test_quick_parse_does_not_log_skip_line`). `ParsedOffers` — подкласс `list`: `== []`, `isinstance(..., list)` и `filter_wrong_dates` работают как раньше. В кэши (`guard`, `fresh_cache`) он не попадает: туда идёт список после `filter_wrong_dates`.

Что считается элементом:

| Парсер | Элемент | N |
|---|---|---|
| aviasales | билет чанка `/search/v3.2/results`, включая `cheapest_ticket`, `filtered_cheapest_ticket` | по доставкам: билет, повторённый в порциях, считается в каждой (`test_aviasales_counts_each_delivery_of_a_crooked_ticket`) |
| kupibilet | вариант лучшего ответа `/frontend_search` | один ответ. Варианты с поездом (`transport_kind` ≠ `airplane`) тоже считаются пропущенными: парсер отбрасывает их тем же путём, что и сегмент без рейсов |
| ozon_travel | карточка лучшего ответа виджета | один ответ. Если JSON не дал ни одного билета и сработал запасной разбор текста, строка всё равно скажет «пропущено N из N» по JSON — это сигнал, что JSON сменился |
| ttravel | оффер каждого ответа `startStreaming` | по ответам. Свой WARNING на исключение внутри оффера («Ошибка обработки оффера ttravel») остался |
| tutu | предложение `offers.actual` итоговой порции | одна порция |
| yandex_travel | тариф порции; маршрут, из которого не собрался билет, — один пропуск | по порциям; запасной разбор текста — как у ozon |

Шума на целых данных нет: на всех 21 фикстуре из `tests/fixtures/` (туда и туда-обратно, плюс три порции aviasales вместе) `skipped = 0`.

## RED / GREEN

RED — итоговый `tests/test_parsers_negative.py` на нетронутом коде `74854fa` (`git archive 74854fa src` в scratchpad, `pytest -o pythonpath=…`):

```
FAILED tests/test_parsers_negative.py::test_all_elements_crooked_logs_one_skip_line_then_parser_empty_error[aviasales]
… [kupibilet] [ozon_travel] [ttravel] [tutu] [yandex_travel]
FAILED tests/test_parsers_negative.py::test_one_crooked_element_logs_one_warning_with_site_name[aviasales]
… [kupibilet] [ozon_travel] [ttravel] [tutu] [yandex_travel]
FAILED tests/test_parsers_negative.py::test_yandex_route_missing_from_reference_counted_as_skipped
FAILED tests/test_parsers_negative.py::test_aviasales_counts_each_delivery_of_a_crooked_ticket
14 failed, 98 passed, 11 xfailed in 0.87s
```

Причина у всех 14 одна — строки нет:

```
E       AssertionError: assert [] == [(30, 'Сайт t...нтов выдачи')]
E         Right contains one more item: (30, 'Сайт tutu: пропущено 1 из 4 элементов выдачи')
```

RED тестов по ревью на `8a61695`:

```
E       assert not [<LogRecord: avia.pipeline, 30, …/pipeline.py, 333, "Сайт %s: пропущено %d из %d элементов выдачи">]
FAILED tests/test_parsers_negative.py::test_duplicate_element_not_counted_as_skipped[tutu]
1 failed, 7 passed, 115 deselected in 0.28s
```

GREEN на `c6cd49a`: `112 passed, 11 xfailed in 1.03s`. Тесты парсеров владельца (`test_aviasales`, `test_kupibilet`, `test_ozon_travel`, `test_ttravel`, `test_tutu*`, `test_yandex_travel`) проходят без изменений.

`xfail(strict=True)` — 11 тестов, все падают по причине из reason (проверено `--runxfail`):

| Тест | Сколько | Что не так сейчас |
|---|---|---|
| `test_short_page_without_captcha_counted_as_failed_run` | 6 (все сайты) | `SiteResult.ok` = True, `watch_stats.ok` = 1 |
| `test_one_element_with_garbage_time_not_shown` | 4 (kupibilet, ozon_travel, ttravel, tutu) | билет с мусором вместо времени у человека |
| `test_renamed_format_keeps_debug_snapshot_when_no_retry` | 1 | `debug_dir` пуст |

## pytest

Без прокси, `~/avia-scale/upstream/.venv`, `PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider`; `pgrep -f '[l]oadtest.run|[-]m pytest'` перед каждым запуском пуст.

| Код | Результат | Время |
|---|---|---|
| `kt1-all` `74854fa` (отчёт КТ1, `4bc6e4d`) | 1080 passed, 3 skipped | — |
| `kt4-parsers` `8a61695` | 1184 passed, 3 skipped, 11 xfailed in 31,42 s | 20:09:47–20:10:18 |
| `kt4-parsers` `c6cd49a` | **1192 passed, 3 skipped, 11 xfailed** in 32,00 s | 20:11:37–20:12:09 |

## gate quick

`cd ~/avia-scale/kt0-up && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=~/avia-scale/kt4-parsers/src:~/avia-scale/kt0-up ~/avia-scale/kt0-up/.venv/bin/python -m loadtest.gate --profile quick --tag _kt4p --results ~/avia-scale/kt4-parsers/.sim_results`, без прокси; JSON — в `.sim_results/` (не в git). `kt0-up` после прогонов не изменился (`git status` пуст).

| Код | Код выхода | Время |
|---|---|---|
| `8a61695` | 0, 8/8 ok | 20:10:33–20:10:55 |
| `c6cd49a` | **0, 8/8 ok** | 20:12:14–20:12:45 |

```
профиль quick, база 9ba5ae37bc14af588021af11b5c2e3d23287d1b4, эталон *_v8base.json, код /Users/a1/avia-scale/kt4-parsers/src (c6cd49a), JSON /Users/a1/avia-scale/kt4-parsers/.sim_results/*_kt4p.json
| прогон | метрика | порог | факт | ok |
|---|---|---|---|---|
| A 10/2 | card_latency.n | ≥ 10 | 10 | ok |
| A 10/2 | card_latency.p95 | ≤ 103 | 90.6 | ok |
| A 10/2 | card_latency.max | ≤ 103 | 90.6 | ok |
| A 10/2 | card_sites.mean | ≥ 0.81 | 1.4 | ok |
| A 50/4 | card_latency.n | ≥ 50 | 50 | ok |
| A 50/4 | card_latency.p95 | ≤ 104 | 90.7 | ok |
| A 50/4 | card_latency.max | ≤ 110 | 91 | ok |
| A 50/4 | card_sites.mean | ≥ 0.55 | 0.6 | ok |
```

## Предложения (решает приёмщик; кода в ветке нет)

**1. Короткая страница без капчи — неудачный сбор.** Флаг `SHORT_PAGE_FAIL` (0/1, по умолчанию 0 — как сейчас): поле в `Settings` и строка чтения env в `config.py`. В `_process_snapshot`, рядом с проверкой «> 200 символов», ещё одна ветка:

```python
elif settings.short_page_fail and not is_captcha and not snap.json_payloads:
    raise ParserEmptyError(f"сайт не отдал выдачу: «{text_stripped[:60]}»", page_url=snap.url)
```

- Порог: 0 билетов, не капча, текст ≤ 200 символов и **ни одного JSON-ответа**. Если JSON пришёл, но билетов в нём нет («рейсов нет»), — как сейчас.
- Человек увидит: «⚠️ <сайт>: сайт не отдал выдачу: «502 Bad Gateway nginx» · открыть выдачу» вместо «билетов не нашлось / неполная выдача».
- `site_runs`: `ok = 0, partial = 0`; `parser_watch` — «сломан», а не «деградирует».
- `speed.py`: без изменений. Такой сбор уже провал («partial без билетов», `speed.py:192–193`): 3 подряд — карантин пары 600 с, и после правки так же.
- Предохранитель сайта: сейчас короткая страница для него удачный сбор и мешает ему сработать. После правки она перестаёт быть удачей. С текстом страницы в ошибке (`502`, `429`) она попадает в `site_recent_outcomes` как провал: при провале на 2 телефонах за 300 с сайт уходит в «временно недоступен» на 600 с. Без текста страницы в ошибке предохранитель её просто не видит. Выбор — вместе с флагом.
- Повторы: путь `ParserEmptyError`, как у длинной страницы. Следующий телефон в `_from_browser` — как сейчас. Плюс `_fetch` может сделать ещё один повтор без этого телефона после `EMPTY_RETRY_DELAY`, если `_can_retry_on_phone` разрешает.
- Решение — по `site_runs` за 1–2 дня до и после (доля `ok = 1, offers = 0` по сайтам).

**2. Мусор вместо времени — кривой элемент.** Одна проверка в `_process_snapshot`, сразу после парсера, для всех сайтов. Билет, у которого `depart_time` или `arrive_time` не вида `ЧЧ:ММ`, отбрасывается и идёт в ту же строку «пропущено K из N» (около 6 строк). На всех 21 фикстуре и 4 текстовых страницах ozon/yandex таких билетов 0, то есть целые данные правка не задевает. После неё kupibilet, ttravel, tutu и ozon_travel пропускают такой билет, а aviasales и yandex_travel — по правильной причине, а не как «неверные даты». Чистый багфикс или флаг (`OFFER_TIME_CHECK`, по умолчанию 0) — на выбор. Обратное плечо (`back`) — так же или нет: тоже на выбор.

**3. Отладочный снимок — сохранять, когда повтора не будет.** В `_fetch` (`pipeline.py:1205–1209` на `74854fa`) перенести удаление снимка в ветку повтора — после того как `_can_retry_on_phone` разрешил повтор, перед `asyncio.sleep(EMPTY_RETRY_DELAY)`. Перенос строк, без новой логики. Чем платим: `debug_dir` в `src` никто не чистит. Пока парсер сломан, каждый сбор этого сайта оставляет JSON со всеми ответами. Нужен предел (например, 20 последних на сайт) или ручная чистка.

## Что не сделано

- Предложения 1–3 — только в отчёте. Код — после решения приёмщика.
- Короткую страницу, капчу и изменённый формат проверял на живом поиске (`priority = 0`, один телефон). Подписки и фон (`priority > 0`) идут тем же `_process_snapshot`, отдельно не проверял.
- `gate --profile full` не гонял: по П33 он один раз перед передачей пачки владельцу.
