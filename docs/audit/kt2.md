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
