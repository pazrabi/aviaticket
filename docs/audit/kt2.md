# КТ2, работа 9 (П31): ранняя карточка живого поиска — флаг `CARD_EARLY`

Ветка `kt2-card` от `kt1-all` `74854fa`. Коммиты: `8ac62f1` (тесты, RED), `e8a6f13` (код), этот файл.

## Что и где
- `src/avia/config.py` — флаги: `CARD_EARLY` (только `1` включает, по умолчанию `0`), `CARD_EARLY_SECONDS` (30; ноль, минус, мусор → 30), `CARD_EARLY_SITES` (4; меньше 1, мусор → 4).
- `src/avia/card_early.py` (новый) — `SiteWatch` (обёртка `on_site_done`: кто ответил, правило «пора»), `EarlyCard`, `CompactView`, `card_metrics`, минимум цены.
- `src/avia/bot.py`, `_run_search` — фаза ранней карточки (`_early_card_phase`, `_send_early_card`), правка на полную (`_finish_early_card`, `_edit_card`). Сборка карточки вынесена в помощники (`_compact_view`, `_history_hint_line`, `_card_session`, `_send_chunks`, `_put_view`) — порядок и аргументы прежние.
- `src/avia/formatting.py` — `build_compact_report(..., pending_sites=())`: строка «⏳ <сайт> ещё ищет» над сайтами с ошибками; без параметра вывод прежний.
- `src/avia/metrics.py` — колонки `searches`: `card_seconds`, `card_sites_ok`, `card_min_price`, `full_min_price` (NULL допускают; миграция `ALTER TABLE … ADD COLUMN`, как у `site_runs`).
- `tests/test_card_early.py` — 23 теста.

## Как работает при `CARD_EARLY=1`
- Карточка уходит, как только сайтов с ценами ≥ `CARD_EARLY_SITES`, или в момент `CARD_EARLY_SECONDS`, если цены к нему есть, — что раньше. Время проверяется один раз: к 30 с цен нет — по времени ранней карточки не будет (остаётся правило 4 сайтов), иначе одна карточка в конце, как сейчас.
- Ранней карточки нет, если ждать уже некого (ответили все сайты живого поиска), если после фильтров человека билетов нет или карточка не влезает в одно сообщение — тогда как сейчас.
- Неответившие сайты — строкой «⏳ <сайт> ещё ищет»; имя сайта — как в соседних строках карточки (`site_label`: aviasales → Aviasales). Сайты `stage: trial` в живой поиск не идут и в «ещё ищет» не попадают.
- Поиск идёт дальше; в конце (все сайты или предохранитель 90 с) та же карточка правится на полную `editMessageText` с клавиатурой того же диалога (ряд «➕ Показать ещё» — по полной выдаче). Не влезла — остальное новыми сообщениями после неё.
- Снимок выдачи: ранний сразу после ранней карточки, полный после правки.
- Ошибка правки: «not modified» — дальше; «can't parse entities» — повтор текстом без разметки (как `send`); остальное (сообщение удалено и т. п.) — в лог, полная уходит новой карточкой. Поиск не ломается.
- В ранней карточке нет строк истории цены у билетов: история пишется один раз, в конце, как сейчас (строка «ниже/выше обычного» есть).
- `busy`, SEARCHING и возврат попытки (П29) — как в `kt1-all`: снимает их `finally` в конце поиска. Исключение поиска после ранней карточки — «⚠️» и возврат попытки, как в `kt1-all`; ранняя карточка при этом остаётся со строкой «ещё ищет».
- Глубокий поиск, подписки и фон не тронуты.

## Метрики
- `card_seconds` — от старта поиска до момента «пора» (до сборки и отправки, как `total_seconds` снят до сборки полной); `card_sites_ok` — сайтов с ценами в карточке; `card_min_price` / `full_min_price` — минимум цены в карточке и в полной выдаче после фильтров и багажа человека.
- Флаг выключен или ранняя карточка не понадобилась: `card_seconds = total_seconds`, `card_sites_ok = sites_ok`, минимумы равны.
- Фон, подписки, глубокий поиск и строки до выкладки — NULL. Выгрузка `docs/review/prod_stats.py` (блок `early_card`) читает эти колонки.

Медиана и p90 до карточки (SQLite ≥ 3.25), за 2 суток:
```sql
WITH c AS (SELECT card_seconds AS s FROM searches
           WHERE source = 'user' AND COALESCE(cls, 'fast') = 'fast' AND card_seconds IS NOT NULL
             AND ts >= strftime('%s', 'now', '-2 days')),
     o AS (SELECT s, ROW_NUMBER() OVER (ORDER BY s) AS rn, COUNT(*) OVER () AS n FROM c)
SELECT n, MAX(CASE WHEN rn = (n + 1) / 2 THEN s END) AS p50,
          MAX(CASE WHEN rn = CAST(0.9 * n + 0.999999 AS INTEGER) THEN s END) AS p90
FROM o GROUP BY n;
```
Промах и переплата при промахе (приёмка `PLAN_SPEED.md`: ≤ 2 % и ≤ 0,5 %):
```sql
SELECT COUNT(*) AS priced,
       SUM(card_min_price > full_min_price) AS misses,
       ROUND(100.0 * SUM(card_min_price > full_min_price) / COUNT(*), 2) AS miss_pct,
       ROUND(AVG(CASE WHEN card_min_price > full_min_price
                      THEN (card_min_price / full_min_price - 1) * 100 END), 2) AS overpay_pct_on_miss
FROM searches
WHERE source = 'user' AND COALESCE(cls, 'fast') = 'fast'
  AND card_min_price IS NOT NULL AND full_min_price > 0
  AND ts >= strftime('%s', 'now', '-2 days');
```
До и после включения — те же запросы с `ts < <момент включения>` и `ts >= <момент включения>`.

## Как включить и откатить
В `.env` на сервере `CARD_EARLY=1` (по желанию `CARD_EARLY_SECONDS`, `CARD_EARLY_SITES`), затем `./deploy/botctl.sh restart`. Откат — `CARD_EARLY=0` (или убрать строку) и тот же перезапуск. Схема `searches` от флага не зависит: колонки добавятся при первом запуске кода.

## Проверка
- RED (`8ac62f1` на коде `74854fa`): 16 failed, 1 passed (страховочный тест «флаг выключен»).
- GREEN (`e8a6f13`): `tests/test_card_early.py` — 23 passed (4 повтора подряд); полный pytest без прокси — 1103 passed, 3 skipped (39,9 с).
- Флаг выключен, A/B со старым кодом (`kt1-all/src` против `kt2-card/src`, 4 сценария: все сайты, предохранитель, багаж «с», ни одного ответа): вызовы Telegram, тексты, снимок, шаг, `busy` и прежние колонки `searches` совпадают; отличаются только новые колонки.
- `gate --profile quick`, код `e8a6f13`, флаг выключен: код 0, 8 из 8 ok (A 10/2: n 10, p95 90,3, max 90,3, сайтов 0,9; A 50/4: n 50, p95 90,5, max 90,8, сайтов 0,6).
- Прогон A 10/2 с `CARD_EARLY=1` — не сделан (кончился лимит сессии).

# КТ2, работа 10 (П32): предварительные цены из кэша Aviasales — флаг `TP_PREVIEW`

Ветка `kt2-tpcache` от `kt2-card` `56d2a95`. Коммиты: `703b4dd` (тесты, RED), `3462801` (код), `0bbd8ed` (правка по ревью: отмена посреди правки), этот файл.

## Что и где
- `src/avia/config.py` — флаги: `TP_PREVIEW` (только `1` включает, по умолчанию `0`), `TP_PREVIEW_TIMEOUT` (3 с; ноль, минус, nan, inf, мусор → 3). Флаг включён, а `TP_TOKEN` пуст — запросов нет, при старте бота одна строка в лог: `TP_PREVIEW=1, но TP_TOKEN пуст: предварительных цен из кэша Aviasales не будет`.
- `src/avia/tp_preview.py` (новый) — задача предпросмотра (`run_preview`), минимум после фильтров (`cheapest`), текст строки (`preview_line`), снятие задачи в конце поиска (`stop_preview`), колонки для `searches` (`preview_metrics`).
- `src/avia/bot.py`:
  - `WaitMessageUpdater` — `preview_line` и `show_preview`: строка под шапкой, правка сразу; без строки `_render` выдаёт прежний текст.
  - `_run_search` — задача кэша стартует сразу за задачей поиска; снимается в `finally` фазы поиска, до последней правки сообщения ожидания; `preview_*` идут в `record_search`.
  - `Bot.__init__` — строка в лог про пустой `TP_TOKEN`.
- `src/avia/parsers/travelpayouts.py` — `query_params(q, limit)`: в одну сторону параметры прежние (тест сравнивает словарь целиком), туда-обратно — `return_at` и `one_way=false`. `fetch_travelpayouts` берёт параметры оттуда. Источник `aviasales_cache` владельца туда-обратно до запроса не доходит: у него нет `url_template_rt`, pipeline отвечает «обратные билеты пока не поддерживаются». Поэтому с выключенным флагом ничего не меняется.
- `src/avia/metrics.py` — колонки `searches`: `preview_seconds`, `preview_min_price` (NULL допускают; миграция `ALTER TABLE … ADD COLUMN`, как у `card_*`).
- `tests/test_tp_preview.py` — 32 теста; `fetch_travelpayouts` подменён, сети нет.

## Как работает при `TP_PREVIEW=1` и `TP_TOKEN`
- Вместе со стартом живого поиска — отдельная задача: `fetch_travelpayouts(q, TP_TOKEN, TP_MARKER, limit=30)`. Маршрут, дата, взрослые и валюта — из того же `Query`, что у живого поиска. Берём 30 самых дешёвых: фильтры человека могут отбросить первые.
- Цены пришли не позже `TP_PREVIEW_TIMEOUT` — под шапкой сообщения ожидания (шапка — «Ищу билеты», маршрут и дата) одна строка:

  `⚡ Предварительно: от 6 900 ₽ · DP · вылет 23:40 <i>(по кэшу Aviasales, цена может отличаться)</i>`

  Человек видит: «⚡ Предварительно: от 6 900 ₽ · DP · вылет 23:40 (по кэшу Aviasales, цена может отличаться)». Нет авиакомпании или времени в ответе — эта часть пропадает, пометка остаётся всегда.
- Минимум — после фильтров человека, как в карточке (`apply_criteria`: пересадки, время в пути, вылет утром/днём/…).
  - Багаж: в ответе Data API о багаже ничего нет (`Offer.baggage` пуст), поэтому по багажу не фильтруем. `filter_by_baggage` такие билеты и так оставил бы: неизвестный багаж проходит в обоих режимах. В кэше обычно самый дешёвый тариф, часто без багажа. При «с багажом» строка может быть дешевле карточки — это пойдёт в долю расхождений.
  - Время прилёта в ответе нет: фильтр «прилёт утром/…» билет кэша пропускает, как в карточке пропускаются неизвестные данные.
  - Авиакомпания — код IATA из ответа (SU, DP): справочника названий в боте нет.
- Туда-обратно (`return_at`, `one_way=false`). Ответ того же вида (`success`, `currency`, `data[]` с `price`, `airline`, `departure_at`, `transfers`, `duration`, `link`), его разбирает та же `parse_prices_for_dates` без правок:
  - цена — за всю поездку, как у живой карточки туда-обратно; авиакомпания и время вылета — «туда»;
  - `duration` в ответе — сумма обоих плеч, а `parse_prices_for_dates` кладёт её в длительность «туда». Фильтр «в пути до N ч» меряет каждое плечо и отбросил бы билет по сумме, поэтому для туда-обратно длительность убираем: неизвестна — фильтр пропускает, как в карточке;
  - `return_transfers` разбор не читает: «пересадок не больше N» проверяется только у «туда», обратный путь неизвестен и проходит, как в карточке;
  - живого ответа туда-обратно я не видел: токена нет, в сеть ходить нельзя. Вид ответа — по документации Data API v3. Если в проде окажется иначе, строки не будет (разбор упадёт или вернёт пусто) — в логе будет причина.
- Ответ позже таймаута, пусто, после фильтров ничего, ошибка (401, ошибка API, нет сети), правка сообщения ожидания не прошла — строки нет, причина в лог (`avia.tp_preview`). Поиск кэш не ждёт. Если правка не прошла или задачу сняли посреди правки, строку не держим: иначе она всплыла бы с правкой сайта или с последней правкой ожидания, а метрика сказала бы «не было».
- Правка со строкой — сразу, мимо паузы 1,5 с между правками; она одна на поиск. Правки по сайтам после неё — с прежней паузой.
- Конец поиска (все сайты, предохранитель, ошибка) — задача кэша снимается (ждём её не дольше 2 с), висящих задач нет. Тест «кэш медленнее всего поиска» проверяет это по `asyncio.all_tasks()`.
- Цены кэша — только в сообщении ожидания. Их нет в карточке, снимке выдачи (`store.put`), истории цен (`history.annotate`), метриках сайтов (`site_runs`, `attempts`), `sites_ok` и `first_seconds`.
- Ранняя карточка П31, предохранитель, SEARCHING, `busy` и возврат попытки (П29) — как в `kt2-card`. Тест с `CARD_EARLY=1` сравнивает вызовы Telegram карточки с прогоном без предпросмотра: совпадают. Глубокий поиск, подписки и фон не тронуты: у глубокого поиска тот же `WaitMessageUpdater`, но строку там никто не ставит.

## Метрики
- `preview_seconds` — от старта поиска до показа строки (после удачной правки, теми же часами, что `total_seconds`); `preview_min_price` — минимум в строке. Строки не было (флаг выключен, нет токена, кэш опоздал, пуст, ошибка) — NULL. Фон, подписки, глубокий поиск и строки до выкладки — NULL.
- Сравнивать с `full_min_price` (П31): минимум полной выдачи после фильтров и багажа человека.

Время до строки кэша и сколько поисков её получили (приёмка П32: первые цены ≤ 2 с), за 2 суток:
```sql
WITH s AS (SELECT preview_seconds AS p FROM searches
           WHERE source = 'user' AND COALESCE(cls, 'fast') = 'fast'
             AND ts >= strftime('%s', 'now', '-2 days')),
     o AS (SELECT p, ROW_NUMBER() OVER (ORDER BY p) AS rn, COUNT(*) OVER () AS n FROM s WHERE p IS NOT NULL)
SELECT (SELECT COUNT(*) FROM s) AS searches,
       (SELECT COUNT(*) FROM s WHERE p IS NOT NULL) AS with_preview,
       (SELECT COUNT(*) FROM s WHERE p <= 2.0) AS within_2s,
       (SELECT MAX(CASE WHEN rn = (n + 1) / 2 THEN p END) FROM o) AS p50,
       (SELECT MAX(CASE WHEN rn = CAST(0.9 * n + 0.999999 AS INTEGER) THEN p END) FROM o) AS p90;
```
Время до первых цен в любом виде — строка кэша или первый сайт с ценами (`first_seconds`), что раньше; до включения это просто `first_seconds`:
```sql
WITH f AS (SELECT CASE WHEN preview_seconds IS NULL THEN first_seconds
                       WHEN first_seconds IS NULL THEN preview_seconds
                       ELSE MIN(preview_seconds, first_seconds) END AS f
           FROM searches
           WHERE source = 'user' AND COALESCE(cls, 'fast') = 'fast'
             AND ts >= strftime('%s', 'now', '-2 days')),
     o AS (SELECT f, ROW_NUMBER() OVER (ORDER BY f) AS rn, COUNT(*) OVER () AS n FROM f WHERE f IS NOT NULL)
SELECT n, MAX(CASE WHEN rn = (n + 1) / 2 THEN f END) AS p50,
          MAX(CASE WHEN rn = CAST(0.9 * n + 0.999999 AS INTEGER) THEN f END) AS p90
FROM o GROUP BY n;
```
Доля поисков, где строка кэша отличается от итога больше чем на 5 %, и в какую сторону:
```sql
SELECT COUNT(*) AS compared,
       SUM(ABS(preview_min_price / full_min_price - 1) > 0.05) AS off_5pct,
       ROUND(100.0 * SUM(ABS(preview_min_price / full_min_price - 1) > 0.05) / COUNT(*), 1) AS off_5pct_share,
       SUM(preview_min_price < full_min_price * 0.95) AS cache_cheaper,
       SUM(preview_min_price > full_min_price * 1.05) AS cache_dearer,
       ROUND(AVG((preview_min_price / full_min_price - 1) * 100), 1) AS mean_diff_pct
FROM searches
WHERE source = 'user' AND COALESCE(cls, 'fast') = 'fast'
  AND preview_min_price IS NOT NULL AND full_min_price > 0
  AND ts >= strftime('%s', 'now', '-2 days');
```
Строка кэша была, а живых цен нет (после фильтров): `… AND preview_min_price IS NOT NULL AND full_min_price IS NULL` с `COUNT(*)`. До и после включения — те же запросы с `ts < <момент включения>` и `ts >= <момент включения>`.

## Как включить и откатить
1. В `.env` на сервере `TP_TOKEN=<токен из кабинета Travelpayouts, раздел API>` (тот же, что для КТ4) и `TP_PREVIEW=1`; по желанию `TP_PREVIEW_TIMEOUT` (секунды, по умолчанию 3). `TP_MARKER` — как у владельца, в строке не виден.
2. `./deploy/botctl.sh restart`.
3. Проверка: в логе старта нет строки «TP_TOKEN пуст»; после первых поисков — строки `avia.tp_preview` («строка кэша Aviasales, …» или причина, почему её нет) и непустые `preview_seconds` в `searches`.

Откат — `TP_PREVIEW=0` (или убрать строку) и тот же перезапуск. `TP_TOKEN` можно оставить: без флага запросов нет. `aviasales_cache` в `sites.yaml` остаётся выключенным — предпросмотру он не нужен. Схема `searches` от флага не зависит: колонки добавятся при первом запуске кода.

## Внешний сервис — решение владельца
С флагом каждый живой поиск человека уходит в Travelpayouts (`api.travelpayouts.com`, внешний сервис):
- что уходит: город вылета и прилёта, дата вылета, дата обратно (туда-обратно), валюта и токен владельца (заголовок `X-Access-Token`);
- что не уходит: Telegram id, имя, число пассажиров (цена умножается в боте), фильтры человека;
- Travelpayouts видит, какие маршруты и даты ищут через бот владельца, и число запросов на его токен. В проде около 19 живых поисков в сутки — это 19 запросов в сутки; лимиты Data API — по кабинету Travelpayouts.

Включать ли — решает владелец.

## Проверка
- RED (`703b4dd` на коде `56d2a95`): 30 failed, 1 passed (страховочный тест «флаг выключен»). Причины падения — только отсутствие нового: `Settings` без `tp_preview`, `record_search` без `preview_seconds`, нет `query_params` и `return_at`.
- GREEN (`3462801`): `tests/test_tp_preview.py` — 31 passed (4 повтора подряд, по 11,5 с); вместе с `tests/test_card_early.py` — 54 passed. Полный pytest без прокси — 1134 passed, 3 skipped (51,5 с).
- Ревью (отдельный проверяющий, только чтение): блокирующих нет. Отмена задачи посреди правки оставляла строку для последней правки ожидания — исправлено в `0bbd8ed` (тест сначала RED, потом GREEN; 32 passed). Пересадки «обратно» у туда-обратно не проверяются — так и задумано, см. выше.
- Полный pytest без прокси на `0bbd8ed` — 1135 passed, 3 skipped (51,8 с).
- Флаг выключен, A/B со старым кодом (`kt2-card/src` против `kt2-tpcache/src`, токен задан, 7 сценариев: все сайты, предохранитель, багаж «с», ни одного ответа, «только прямые», туда-обратно, `CARD_EARLY=1`). Совпадают вызовы Telegram и тексты, снимок, шаг, `busy`, `site_runs`, счётчик попыток, прежние колонки `searches`; висящих задач нет. Отличаются только новые колонки (NULL). Повтор на `0bbd8ed` — то же.
- SQL этого раздела проверен на тестовой базе (5 поисков людей и фон): все три запроса считают ожидаемые значения.
- `gate --profile quick`, флаг выключен: код 0, 8 из 8 ok.
  - На `3462801`: A 10/2 — n 10, p95 90,2, max 90,2, сайтов 1; A 50/4 — n 50, p95 90,5, max 90,8, сайтов 0,6.
  - На `0bbd8ed`: A 10/2 — n 10, p95 90,3, max 90,3, сайтов 1,1; A 50/4 — n 50, p95 90,6, max 91,1, сайтов 0,6.
  - JSON — в `.sim_results/` (не в git); `kt0-up` не изменился.
- Прогона с `TP_PREVIEW=1` в симуляторе нет: там нет Data API, а в настоящую сеть ходить нельзя. Время до строки и расхождения — по проду, SQL выше.
