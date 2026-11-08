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
| `85ee7cc` | отчёт |
| `2da972e` | доработка: тесты — RED на `85ee7cc` |
| `535035f` | доработка: время, `SHORT_PAGE_FAIL`, отладочный снимок и предел `debug_dir` |
| этот | отчёт: раздел «Доработка» |

Разделы ниже, до «Доработки», описывают код `c6cd49a`. Что изменила доработка — в разделе «Доработка» в конце.

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

Приёмщик принял все три с уточнениями: 1 — под флагом, 2 и 3 — багфикс без флага, к 3 — предел `debug_dir`. Код — в разделе «Доработка».

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

- Предложения 1–3 — только в отчёте. Код — после решения приёмщика. *(Сделано в доработке.)*
- Короткую страницу, капчу и изменённый формат проверял на живом поиске (`priority = 0`, один телефон). Подписки и фон (`priority > 0`) идут тем же `_process_snapshot`, отдельно не проверял.
- `gate --profile full` не гонял: по П33 он один раз перед передачей пачки владельцу.

## Доработка

**Задание:** приёмщик, 2026-11-08, доработка работы 4 поверх `85ee7cc`, та же ветка `kt4-parsers`. Три пункта: мусор вместо времени — кривой элемент (багфикс), флаг `SHORT_PAGE_FAIL` (по умолчанию 0), отладочный снимок не теряется и `debug_dir` ограничен (багфикс).

| Коммит | Что |
|---|---|
| `2da972e` | `tests/test_parsers_negative.py` — RED на `85ee7cc` |
| `535035f` | код: все три пункта |
| этот | отчёт |

`git diff --numstat 85ee7cc 535035f`:

| Файл | Строк | Что и где (строки — на `535035f`) |
|---|---|---|
| `src/avia/pipeline.py` | +89 −7 | константы `DEBUG_SNAPSHOT_NAME`, `_CLOCK_TIME`, `_ISO_MINUTE`, `SHORT_PAGE_EXCERPT_CHARS` (49–54); `_save_empty_snapshot` зовёт `_prune_debug_snapshots` (245); `_drop_debug_snapshot`, `_prune_debug_snapshots` (252–278); `_has_bad_time`, `_has_json_answer`, `_page_excerpt` (314–342); `_process_snapshot`: проверка времени (400–405) и ветка `SHORT_PAGE_FAIL` (436–443); `_quick_parse_offers` (485); `_fetch`: когда удалять снимок (1288–1318) |
| `src/avia/config.py` | +11 | поля `short_page_fail`, `debug_snapshots_max` (155–158); чтение env (339–343); передача в `Settings` (482–483) |
| `src/avia/parsers/ozon_travel.py` | +7 −1 | `_parse_leg`: признак `bad_time` (145–146); карточка с `bad_time` туда или обратно — пропуск (493–494, 501–502); запасной разбор текста: N с текстовыми карточками (585) |
| `src/avia/parsers/yandex_travel.py` | +1 −1 | запасной разбор текста: N с текстовыми карточками (1161) |
| `tests/test_parsers_negative.py` | +386 −61 | 196 тестов (было 123), `xfail` нет |

`parsers/travelpayouts.py`, `speed.py`, `sites.yaml`, пороги и решения владельца не тронуты.

### 1. Мусор вместо времени — кривой элемент (без флага)

- **Правило.** Время билета (`depart_time`, `arrive_time`) есть и непустое, но не `Ч:ММ` / `ЧЧ:ММ` (часы 0–23, минуты 00–59) — элемент кривой. Дата-время (`depart_at`, `arrive_at`) есть, но не начинается с `ГГГГ-ММ-ДДTЧЧ:ММ` — тоже (секунды и пояс после минут допустимы). Проверяются оба плеча: туда и `back`. Время `None` или `""` — время неизвестно, билет остаётся.
- **Где.** ozon_travel — в самом парсере: мусор там превращается в `None` и до `pipeline` доходит как «время неизвестно». Поэтому `_parse_leg` помечает плечо `bad_time` (текст времени в карточке непустой, а разобрать его не вышло), и карточка пропускается тем же путём, что и другие кривые карточки. Остальные пять сайтов — одна проверка `_has_bad_time` в `_process_snapshot` сразу после парсера. Отброшенные идут в ту же строку «Сайт X: пропущено K из N элементов выдачи».
- **aviasales и yandex_travel.** Билет с мусором уходит как кривой элемент: строка «пропущено 1 из N» есть, строки «отброшено … с неверными датами» нет. Тест проверяет оба условия.
- **FAST_STOP.** `_quick_parse_offers` отбрасывает такие билеты так же, но без строки лога. Иначе «выдача стабилизировалась» считалась бы по билетам, которых человек не увидит.
- **Запасной разбор текста ozon и yandex.** В N теперь входят и текстовые карточки (`total + len(text_offers)`). Иначе при отброшенной текстовой карточке K было бы больше N.
- **Целые данные.** На 21 JSON-фикстуре и 4 текстовых страницах ozon/yandex `skipped = 0` и билетов с плохим временем 0 (`test_intact_fixtures_have_no_skips_and_no_bad_times`, 25 случаев, у каждой фикстуры свой запрос).
- **Граница.** Офферы API (`_from_api`, travelpayouts) и LLM-разбор (`use_llm`) идут мимо проверки. Ни у одного сайта в `sites.yaml` `use_llm` нет, travelpayouts в этой ветке не трогаем.
- **Откат.** Флага нет. Откатывается `git revert 535035f`, вместе с пунктами 2 и 3.

Тесты: `test_one_element_with_garbage_time_skipped_as_crooked` (все 6 сайтов), `test_quick_parse_drops_garbage_time` (kupibilet, ozon_travel, ttravel, tutu), `test_time_check_drops_only_present_malformed_times`, `test_ozon_card_without_time_kept`. Последний проверяет, что карточка ozon без `tripTime` или с пустым `tripTime` остаётся.

Последний тест проверяет правило на поддельном парсере, 9 билетов:

| Билет | Время | Итог |
|---|---|---|
| 1000 | `07:05`, `9:10` | остаётся |
| 1100 | `None` | остаётся |
| 1200 | `""` | остаётся |
| 1300 | ISO с секундами и поясом | остаётся |
| 1800 | обратное плечо с нормальным временем | остаётся |
| 1400 | `мусор` | отброшен |
| 1500 | `25:00` | отброшен |
| 1600 | `depart_at` = `мусор` | отброшен |
| 1700 | обратное плечо с `мусор` | отброшен |

Строка лога: «Сайт fake_times: пропущено 4 из 9 элементов выдачи».

### 2. `SHORT_PAGE_FAIL` (по умолчанию 0)

- **Включить:** `SHORT_PAGE_FAIL=1` в окружении бота (принимает `1`, `true`, `yes`, `on`) и перезапуск: флаг читается в `load_settings` при старте. **Откатить:** `SHORT_PAGE_FAIL=0` или убрать переменную, затем перезапуск.
- **Когда срабатывает.** Все условия сразу:
  - парсер дал 0 билетов (после проверки времени);
  - не капча (`snap.blocked` = False);
  - текст страницы не длиннее 200 символов;
  - нет ни одного JSON-ответа.

  «Не длиннее 200» — дополнение к правилу владельца `len > 200`. В задании сказано «меньше 200». Страница ровно в 200 символов не попадает под правило владельца, и при «< 200» она осталась бы «0 вариантов». Поэтому порог `≤ 200`. Если нужно строго `< 200`, это одна строка.
- **Что такое JSON-ответ.** Элемент `snap.json_payloads`, то есть ответ сайта, перехваченный при сборе (телефон — по `capture_json` сайта, браузер — по `should_save_response`), тело которого разбирается `json.loads`.
  - Считаются JSON-ответом: `{}`, `[]`, `null`. Сайт ответил, рейсов нет — поведение как сейчас (`test_short_page_flag_on_with_json_answer_behaves_as_before`).
  - Не считаются: пустое тело и HTML в перехваченном ответе (`<html>502 Bad Gateway…</html>`). Это неудачный сбор (`test_short_page_flag_on_with_html_in_captured_answer_fails`).
- **С флагом 1.**
  - Ошибка: `ParserEmptyError("сайт не отдал выдачу: «<первые 60 символов страницы, пробелы схлопнуты>»")`. Для пустой страницы — «пустая страница».
  - Снимок — в `debug_dir` с тегом `short`: `<сайт>_short_<ГГГГММДД_ЧЧММСС>.json`.
  - `SiteResult.ok = False`. В `site_runs`: `ok = 0, partial = 0`.
  - `parser_watch` считает провал: по одному сбору в тесте — «сломан».
  - Человек видит ту же строку, что и при других ошибках сайта: «⚠️ <сайт>: сайт не отдал выдачу: «502 Bad Gateway nginx» · открыть выдачу», а не «билетов не нашлось».
  - Капча остаётся капчей (`test_short_page_flag_on_captcha_still_captcha`).
  - Повторы — путь `ParserEmptyError`, как у длинной страницы.
- **С флагом 0.** Ветка не выполняется. `test_short_page_flag_off_behaves_as_before` (флаг по умолчанию и явный `0`, 6 сайтов) сверяет всё, что сверял прежний тест текущего поведения: `SiteResult`, `site_runs`, статус `parser_watch`, строку человеку. Плюс проверяет, что `debug_dir` пуст.
- **Предохранитель сайта.** Если в тексте страницы есть слова, по которым `dispatcher._fuse_decide` узнаёт провал (`502`, `429`, «недоступ», «таймаут», …), предохранитель видит провал. Провал на 2 телефонах за 300 с уводит сайт в «временно недоступен» на 600 с. Без таких слов предохранитель такой сбор не учитывает: ни удачей, ни провалом. Раньше короткая страница была для него удачей.
- **`speed.py`.** Без изменений. Такой сбор и раньше был провалом («partial без билетов»), теперь он провал как `ok = 0`.

### 3. Отладочный снимок (без флага) и предел `debug_dir`

**Когда удаляется снимок.** В `_fetch` удаления в начале `except ParserEmptyError` больше нет.

- **Путь с телефонами.** Снимок удаляется, только когда повтор точно пойдёт на другой телефон: `_can_retry_on_phone` разрешил повтор и список других телефонов (`cand_list`) непуст. Во всех остальных случаях снимок остаётся владельцу:
  - нет повтора — последний кандидат, повтор не разрешён или упёрлись в лимит времени;
  - повтор пойдёт на тот же телефон — фон, `priority > 0`, один телефон.

  Во втором случае повтор не «на другом телефоне», это буквальное правило задания. Если повтор тоже пустой, будет второй снимок, его ограничивает предел ниже.
- **Повтор на следующем телефоне внутри `_from_browser`.** Как было: снимок первого телефона удаляется перед `continue` (`test_in_loop_retry_on_other_phone_removes_debug_snapshot`). Повтор там идёт всегда.
- **Путь без телефонов (браузер).**
  - Снимок удаляется перед повтором тем же браузером, как раньше. Причина — тест владельца `tests/test_pipeline.py::test_pipeline_empty_long_text_creates_debug_snapshot`: он ждёт ровно 1 снимок после неудачного повтора, а без удаления их 2. Повтор при неудаче оставляет свой снимок, так что снимок у владельца есть.
  - Упёрлись в лимит времени (`_hit_time_cap`, повтора не будет) — снимок остаётся.

**Предел `DEBUG_SNAPSHOTS_MAX`.**

- **Значение.** Env, по умолчанию 100. `0` или меньше — без предела. Не число — 100.
- **Как работает.** После записи нового снимка `_save_empty_snapshot` зовёт `_prune_debug_snapshots`. Тот берёт файлы в самом `debug_dir` (`iterdir`, без рекурсии): обычные файлы, не симлинки, с именем снимка `<сайт>_(empty|wrongdates|short)_ГГГГММДД_ЧЧММСС.json`. Сортирует их по `mtime`, затем по имени, и удаляет самые старые сверх предела.
- **Что не трогает.** Скриншоты и тексты сборщика (`<сайт>.png`, `<сайт>.txt`), файлы с похожими, но другими именами и подкаталоги (`recon/` и любые другие).
- **Ошибка ФС** — WARNING; новый снимок к этому моменту уже записан.
- **Охват.** Предел общий на весь `debug_dir`, не на сайт.

Тесты:

- `test_renamed_format_keeps_debug_snapshot_when_no_retry` — 6 сайтов, бывший `xfail`;
- `test_fetch_no_retry_keeps_debug_snapshot`;
- `test_fetch_retry_on_other_phone_removes_debug_snapshot`;
- `test_fetch_retry_on_same_phone_keeps_debug_snapshot`;
- `test_in_loop_retry_on_other_phone_removes_debug_snapshot`;
- `test_debug_snapshots_capped_oldest_removed` — 105 старых снимков, файлы с другими именами, подкаталог `recon/` с тремя снимками внутри (их не трогает); предел 100;
- `test_debug_snapshots_not_capped_when_limit_off` — предел 0;
- `test_load_settings_short_page_fail`, `test_load_settings_debug_snapshots_max`.

### RED / GREEN

RED — итоговый `tests/test_parsers_negative.py` (`2da972e`) на коде `85ee7cc`:

```
............................................................FFFFFF.FF.FF [ 36%]
..................FFFFFF.FFFF.F..FFFFFFFFFFFFFFFFFFFFFFFFF.............. [ 73%]
.............F.F.F.F.F.FFFFFFFFFFFFFFFFFFFFFFFFFFFFF                     [100%]
…
80 failed, 116 passed in 1.68s
```

Причины (`grep '^E ' | sort | uniq -c`, без строк diff):

```
  25 E       TypeError: Settings.__init__() got an unexpected keyword argument 'short_page_fail'
  25 E       AttributeError: module 'avia.pipeline' has no attribute '_has_bad_time'
   8 E        +  where 0 = len([])
   6 E       assert (0 == 1)
   5 E       AttributeError: 'Settings' object has no attribute 'short_page_fail'
   4 E       AttributeError: 'Settings' object has no attribute 'debug_snapshots_max'
   2 E       TypeError: Settings.__init__() got an unexpected keyword argument 'debug_snapshots_max'
   2 E       AssertionError: assert 8 == (8 - 1)
   2 E       AssertionError: assert 60 == (60 - 1)
   2 E       AssertionError: assert 4 == (4 - 1)
   2 E       AssertionError: assert 30 == (30 - 1)
   2 E       AssertionError: assert 0 == 1
   1 E       assert [1000, 1100, ...00, 1500, ...] == [1000, 1100, 1200, 1300, 1800]
   1 E       AssertionError: assert [] == ['Сайт yandex...ентов выдачи']
   1 E       AssertionError: assert [] == ['Сайт aviasa...ентов выдачи']
```

Что значат эти причины:

- Нет полей и функций доработки: `short_page_fail`, `debug_snapshots_max`, `_has_bad_time`.
- Билет с мусором остаётся у человека: `8 == 8 - 1` (kupibilet), `60 == 60 - 1` (ttravel), `4 == 4 - 1` (tutu), `30 == 30 - 1` (ozon_travel).
- `debug_dir` пуст: `len([]) == 0`.
- Нет строки «пропущено» у aviasales и yandex_travel.
- Правило времени не отбрасывает ни одного билета из 9.

GREEN на `535035f`: `tests/test_parsers_negative.py` — **196 passed in 1.10s**.

### pytest

Тем же способом, что выше: без прокси, `~/avia-scale/upstream/.venv`, `-B`, `-p no:cacheprovider`. Перед запуском `pgrep -f '[l]oadtest.run|[-]m pytest'` пуст: ждал, пока закончится чужой pytest из другого проекта.

| Код | Результат | Время |
|---|---|---|
| `kt4-parsers` `c6cd49a` | 1192 passed, 3 skipped, 11 xfailed in 32,00 s | — |
| `kt4-parsers` `535035f` | **1276 passed, 3 skipped** in 31,39 s | 20:32:31–20:33:03 |

1276 = 1192 + 11 бывших `xfail` + 73 новых теста. Три skipped — владельца, те же, что раньше: `tests/test_forecast.py:344, 397, 425`, «could not import 'sklearn'».

### gate quick

Та же команда, что выше, `--tag _kt4p2`. Флаг `SHORT_PAGE_FAIL` не задан, то есть 0. Перед прогоном `pgrep` пуст. `kt0-up` после прогона не изменился (`git status` пуст). JSON — в `.sim_results/*_kt4p2.json`, не в git.

| Код | Код выхода | Время |
|---|---|---|
| `535035f` | **0, 8/8 ok** | 20:33:17–20:33:39 |

```
профиль quick, база 9ba5ae37bc14af588021af11b5c2e3d23287d1b4, эталон *_v8base.json, код /Users/a1/avia-scale/kt4-parsers/src (535035f), JSON /Users/a1/avia-scale/kt4-parsers/.sim_results/*_kt4p2.json
| прогон | метрика | порог | факт | ok |
|---|---|---|---|---|
| A 10/2 | card_latency.n | ≥ 10 | 10 | ok |
| A 10/2 | card_latency.p95 | ≤ 103 | 90.1 | ok |
| A 10/2 | card_latency.max | ≤ 103 | 90.1 | ok |
| A 10/2 | card_sites.mean | ≥ 0.81 | 1 | ok |
| A 50/4 | card_latency.n | ≥ 50 | 50 | ok |
| A 50/4 | card_latency.p95 | ≤ 104 | 90.7 | ok |
| A 50/4 | card_latency.max | ≤ 110 | 91 | ok |
| A 50/4 | card_sites.mean | ≥ 0.55 | 0.6 | ok |
```

### Что осталось `xfail` и что не сделано

- **`xfail` в ветке: 0.** Все 11 стали обычными тестами:
  - короткая страница — с флагом 1 и с флагом 0;
  - время — 4 сайта;
  - снимок — 1 тест.
- **Короткая страница** проверена на живом поиске (`priority = 0`, один телефон). Подписки и фон (`priority > 0`) идут тем же `_process_snapshot`, отдельно не проверял.
- **Порог «≤ 200», а не «< 200»** — см. пункт 2; решает приёмщик.
- **Проверка времени** не касается офферов API (travelpayouts) и LLM-разбора.
- **`gate --profile full`** не гонял: по П33 он один раз, перед передачей пачки владельцу.
