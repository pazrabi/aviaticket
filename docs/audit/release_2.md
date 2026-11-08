# Выпуск 2: ветка `release-2`

**Задание:** выпуск 2, 2026-11-08: `release-1` + `kt4-db` + `kt4-prefilter`, проверки перед передачей владельцу, файл для переноса. Порядок — как в выпуске 1 (`docs/audit/release_1.md`). Код бота не менялся: только слияние, отчёт и скрипт проверки отката.
**База:** `release-1` `b8909ab` — выпуск 1, на проде с 21:42. Ветка `release-2`, worktree `~/avia-scale/release-2`. Только локально: без push и PR. Вливает и выкладывает владелец (`deploy/botctl.sh deploy`).
**Код выпуска** — дерево `a09bc2d`. Коммит этого отчёта меняет только `docs/audit/`. Файл переноса (`release-2.bundle`) и его sha256 — в сообщении приёмщику: bundle создаётся после этого коммита.

## Как собрано

```
git -C ~/avia-scale/kt1-all worktree add -b release-2 ~/avia-scale/release-2 b8909ab
git merge --no-ff 877b4c5   # kt4-db        → d865c06
git merge --no-ff 679c4cc   # kt4-prefilter → a09bc2d
```

- Конфликтов нет. `pipeline.py` и `speed.py` (`kt4-db`), `bot.py` и `config.py` (`kt4-prefilter`) слились сами.
- Обе ветки — от `kt2-tpcache` `59627b8`, без `kt4-pair` и `kt4-parsers`. Поэтому слияние сверено так:
  - `kt4-prefilter`: `git patch-id --stable` ветки (`59627b8..679c4cc`) = patch-id merge (`d865c06..a09bc2d`) — `72aba64d…`.
  - `kt4-db`: patch-id разный — `6a72d39f…` против `0a2e7e6d…`. Но добавленные и удалённые строки (`git diff -U0`, без контекста) совпадают во всех 20 файлах. Отличие — только в контексте `speed.py`: соседняя строка импорта `SPEED_PAIR_WINDOW_DEFAULT, …, normalize_pair_window` пришла из `kt4-pair`.
- Имена четырёх баз в `src` после слияния есть только в `statedb.py` (и в комментарии `config.py:111`). Код КТ4 из выпуска 1 путей мимо `db_path` не строит, это держит и `test_no_module_builds_old_file_names_itself`.
- `git log --oneline 9ba5ae3..release-2`: до этого отчёта 68 коммитов, с ним — 69. Из них 16 merge: 14 из выпуска 1 и 2 этого выпуска (`d865c06`, `a09bc2d`). От `b8909ab` — 11 коммитов до отчёта, 12 с ним.

## Что входит

| Ветка | SHA | Что даёт |
|---|---|---|
| `release-1` | `b8909ab` | выпуск 1 целиком (`docs/audit/release_1.md`) — на проде |
| `kt4-db` (КТ4, работа 1, ADR-002) | `877b4c5` | флаг `DB_SINGLE`: одна `state/avia.sqlite` вместо четырёх файлов (перенос в одной транзакции, WAL, `user_version` и миграции), сессии и выдачи переживают перезапуск; откат — `python -m avia.cli db-rollback` |
| `kt4-prefilter` (КТ4, работа 2, ADR-003) | `679c4cc` | флаг `SUB_PREFILTER`: перед сбором подписки — запрос в кэш Aviasales; цена не изменилась на ≥ `SUB_PREFILTER_PCT` % и страховочный сбор не нужен — сбор на телефонах пропускается |

Новые модули: `src/avia/statedb.py`, `src/avia/chat_state.py`, `src/avia/sub_prefilter.py`. Подробности — `docs/audit/kt4_db.md`, `docs/audit/kt4_prefilter.md`; приёмка — `~/avia-scale/work/docs/review/kt4.md`. `.env.example`, `config/`, `deploy/`, `requirements.txt` выпуск не менял.

## Что меняется сразу (без флагов)

**Ничего** — проверено по диффу `b8909ab..a09bc2d -- src` (15 файлов, +1087 −22):

- **Пути баз.** Все места, где открывались `users`, `metrics`, `history`, `subscriptions.sqlite` (`bot.py`, `dispatcher.py`, `hedge.py`, `pipeline.py`, `droidhub.py`, `speed.py`, `phone_check.py`, `cli.py`), берут путь из `statedb.db_path`. При `DB_SINGLE` ≠ `1` он отдаёт тот же `storage_dir / "<имя>.sqlite"` (`statedb.py:49–55`). WAL, перенос и `avia.sqlite` — только в ветке флага 1.
- **Бот.** `self.chat_state = None`, `store` — прежний `ResultStore()`, сессии в памяти. `open_chat_state` зовётся только при `db_single_enabled()`.
  - `_save_chat_state` добавлен до и после `_process_actions` (через `try/finally`) и в двух местах `handle_callback`. При `chat_state is None` он сразу выходит.
  - Исключения `_process_actions` идут дальше, как раньше.
- **Бэкап** (`backup.py`). Фильтр «только `avia.sqlite`» — только при флаге 1. При 0 копируются те же `*.sqlite`.
- **CLI.** Новая команда `db-rollback`. Без вызова ничего не делает.
- **Подписки.** `Bot.sub_prefilter = None`, если не `SUB_PREFILTER=1` и `TP_TOKEN` (`sub_prefilter.prefilter_enabled`). `Scheduler(prefilter=None)` — ветка `_prefilter_skips` не вызывается. Строки предфильтра в «🌐 Парсинг» нет.
- **Лог.** Новая строка только при `SUB_PREFILTER=1` без `TP_TOKEN`: `WARNING SUB_PREFILTER=1, но TP_TOKEN пуст: …`.
- **Схема.** В четырёх файлах не меняется. Новые таблицы (`db_meta`, `db_legacy_tables`, `chat_sessions`, `chat_snapshots`) создаются только внутри `avia.sqlite` при флаге 1.

Это же показывают проверки:
- quick при выключенных флагах — те же 8/8, что и у выпуска 1;
- в части А проверки отката `avia.sqlite` не появилась, бэкап — четыре файла;
- полный pytest — 1505 = 1378 (выпуск 1) + 49 (`kt4-db`) + 78 (`kt4-prefilter`); тесты владельца не тронуты.

## Флаги выпуска 2

Флаги выпуска 1 — без изменений, их таблица — в `docs/audit/release_1.md`.

Чтение флагов:
- `DB_SINGLE` читает `statedb.db_single_enabled()` из окружения при каждом вызове `db_path`. В окружение он попадает из `.env` через `load_settings` (`load_dotenv`).
- `SUB_PREFILTER*` — поля `Settings` (`config.py`).
- Все включаются строкой в `.env` на сервере и `./deploy/botctl.sh restart`. Старый код (`b8909ab`) этих строк не читает.

| Флаг | По умолчанию | Что включает | Включить | Откатить | Что смотреть до и после |
|---|---|---|---|---|---|
| `DB_SINGLE` | `0` (включает только `1`) | одна `state/avia.sqlite`: при первом старте перенос четырёх файлов в одной транзакции (старые файлы открыты только на чтение и не меняются), WAL, `busy_timeout` 10 с. Сессии (TTL 24 ч) и выдачи (TTL 1800 с, zlib) хранятся в базе и переживают перезапуск. Бэкап — только `avia-*.sqlite.gz` | порядок ниже: `backup` → `.env` → `restart` → проверка | только через `db-rollback` при остановленном боте (ниже). Просто `DB_SINGLE=0` — записи после переноса останутся в `avia.sqlite`, бот их не увидит | лог старта (строки ниже); `/admin` → «🗄 База»: пять файлов, четыре старых больше не растут, «рост МБ/сут» при 1 завышен (делится вся `avia.sqlite`); бэкапы — `avia-*.sqlite.gz`; `avia.sqlite-wal` обычно до ~4 МБ; после рестарта кнопки под выдачей младше 30 мин работают, «⌛ Результаты поиска устарели» почти не встречается; в логе нет `не удалось сохранить сессию и выдачу` |
| `SUB_PREFILTER` | `0` (только `1`); без `TP_TOKEN` не включается | перед сбором группы подписок — один запрос в Data API Travelpayouts (`api.travelpayouts.com`, внешний сервис, токен владельца). Сбор пропускается, если кэш отличается от последней собранной цены меньше чем на `SUB_PREFILTER_PCT` %, цена не у порога человека и страховочный сбор не нужен. Ошибка, 429, «кэш не знает» — собираем, как сейчас; после 429 — 10 мин без запросов. Счётчик решений — в памяти | `TP_TOKEN=…` и `SUB_PREFILTER=1` в `.env`, `restart`; без токена — WARNING при старте | `SUB_PREFILTER=0` или убрать строку, `restart`; схема БД не менялась | `/admin` → «🌐 Парсинг» → «7 дн»: «Источники: … Подписки: N» и строка `Предфильтр подписок: проверок … · пропущено … · собрано … (кэш изменился …, у порога …, кэш не знает …, страховочный …, ошибка кэша …)`; `prod_stats.py` → `attempts.by_site_device_source` по ключам `…\|sub` ÷ `attempts.span_days`, `site_runs.device_busy_share` по `…\|sub`. До включения — ~606 сборов подписок в сутки (выгрузка 2026-11-08 21:31, `kt4.md`). `/admin` → «🔔 Подписки»: «Уведомлений за 24 ч» и «Уведомления за 7 дней по причинам» — не должны заметно упасть |
| `SUB_PREFILTER_PCT` | `5` (ноль, минус, мусор, nan, inf → 5) | порог изменения цены кэша в % (5 % — включительно, вверх и вниз) | число в `.env`, `restart` | убрать, `restart` | то же; при большом пороге — меньше сборов, позже уведомления |
| `SUB_PREFILTER_FULL_HOURS` | `0` = 2 интервала опроса подписки (минус, мусор, nan, inf → 0) | срок страховочного полного сбора в часах, один на все подписки (3 ч — 3 интервала у 60-минутных, 6 у 30-минутных) | число в `.env`, `restart` | убрать или `0`, `restart` | модель (`kt4_prefilter.md`): по умолчанию 100 % изменений замечено за 2 интервала, экономия 21–28 %; больший срок — экономия выше, замечено меньше. Решать после 1–2 дней по строке предфильтра (П36) |

Таймаут запроса предфильтра — общий с П32, `TP_PREVIEW_TIMEOUT` (3 с).

Поведение под `SUB_PREFILTER=1`, о котором надо сказать владельцу (`kt4.md`, замечание 2): уведомления, не привязанные к изменению на ≥ 5 % (падение на 500 ₽ у дорогих билетов, конец паузы 3 ч, «ниже обычного»), приходят в пределах 2 интервалов, а не 1.

## `DB_SINGLE` на проде

**Включение:**
1. `./deploy/botctl.sh backup` — копии четырёх файлов до переноса. При `DB_SINGLE=1` бэкап берёт уже только `avia.sqlite`.
2. На сервере в `.env` бота — строка `DB_SINGLE=1`.
3. `./deploy/botctl.sh restart`. При первом старте идёт перенос. На сервере `history` — 63 МБ; в тесте `kt4-db` 149 МБ переносились 1,9 с.
4. Проверка: `./deploy/botctl.sh logs 60` — в логе старта:
   ```
   avia.sqlite: перенос из users.sqlite (N строк), subscriptions.sqlite (…), history.sqlite (…), metrics.sqlite (…)
   avia.sqlite: схема 0 → 2 за X с
   сессий из базы: N (поиск шёл в K, шаг снят)
   выдач из базы: N
   ```
   Затем `/admin` → «🗄 База» (пять файлов) и первый поиск. `MigrationError` — бот не стартует; вернуть `DB_SINGLE=0`, текст ошибки — разработчику. Старые файлы при переносе не меняются, данные не теряются.

**Откат флага без потерь:**
1. `./deploy/botctl.sh stop`.
2. На сервере: `cd /home/avia/avia-bot && sudo -u avia .venv/bin/python -m avia.cli db-rollback`.
   - Всё из `avia.sqlite` уходит в четыре файла, включая записанное после переноса.
   - Прежние файлы откладываются в `<имя>.sqlite.pre-rollback-<время>`, `avia.sqlite` — в `avia.sqlite.rolledback-<время>`. Ничего не удаляется.
   - Сессии и выдачи в файлы не едут: при 0 они в памяти.
   - Команду можно повторить, если её прервали.
3. `DB_SINGLE=0` в `.env`.
4. `./deploy/botctl.sh start`.

Следующее `DB_SINGLE=1` перенесёт файлы заново. Переключение 1 → 0 → 1 без `db-rollback` бот не пустит: `MigrationError` «… изменились после переноса …» (`kt4_db.md`, «Флаг»). Восстановление из бэкапа при 1 — там же.

## Откат выпуска целиком

**Итог: при `DB_SINGLE=0` откат на `b8909ab` безопасен, в базах ничего откатывать не нужно. При `DB_SINGLE=1` — сначала `db-rollback` кодом выпуска 2, потом выкладка `b8909ab`. Без этого шага старый код не видит ни одной записи времени `DB_SINGLE=1`.**

Проверка — `docs/audit/release_2_rollback.py` на основе `release_1_rollback.py`. Каждая фаза — отдельный процесс со своим кодом в `PYTHONPATH`, 14 шагов, как в выпуске 1:
- настоящие `_run_search` (цены и «все упали») и `_run_deep_search`;
- `/stats`, 10 разделов `/admin`;
- прямые чтения и записи всех четырёх групп таблиц, таблица скоростей;
- `backup_all`, `prune`;
- контроль: поиск падает, перехват лога обязан увидеть `ERROR avia.bot: search failed`.

Провал — исключение в шаге, `ERROR` вне контроля, таблица без новых строк, `integrity_check` не `ok`. Поиск, Telegram и HTTP бота подменены. Флаги выпуска сняты, окружение чистое.

```
cd ~/avia-scale/release-2
~/avia-scale/upstream/.venv/bin/python -B docs/audit/release_2_rollback.py --work <новый каталог>   # без прокси
```

Прогон 2026-11-08, 22:01:53–22:01:55: **код выхода 0, 85 шагов `ok` из 85** (14 × 6 фаз + проверка меток).

**Часть А — флаги выключены:** `new` (release-2) → `old` (`b8909ab`, `git archive`) → `new2` (release-2).

| Фаза | Добавлено строк | Попытки человека 555: день, за день, всего | ERROR вне контроля | Контроль |
|---|---|---|---|---|
| `new` | users 4; searches 4, site_runs 8, attempts 2, price_timeline 1, parser_watch_state 1; price_history 4; subscriptions 1, sub_prices 1, notifications 1 | нет → `1, 1` | 0 | ✅ |
| `old` | users 1; searches 4, site_runs 8, attempts 2, price_timeline 1; price_history 4; sub_prices 1, notifications 1 | `1, 1` → `2, 2` | 0 | ✅ |
| `new2` | как у `old` | `2, 2` → `3, 3` | 0 | ✅ |

`integrity_check` четырёх файлов — `ok`, `avia.sqlite` не появилась. Человек 555 в каждой фазе делает три поиска: с ценами, «все упали» и контроль с исключением. Списывается одна попытка, две возвращаются — П29 есть и в `b8909ab`.

**Часть Б — откат флага и выпуска.** Код release-2 — копия `HEAD` (`git archive`; скрипт проверяет, что `src` и `config` рабочего дерева совпадают с `HEAD`): `db-rollback` берёт `state/` рядом с кодом, как на сервере.

1. `legacy` (release-2, `DB_SINGLE=0`) — четыре файла, как на проде до включения: те же строки, что `new` в части А, 0 ERROR.
2. `single` (release-2, `DB_SINGLE=1`) — перенос и новые записи с метками `single-…` и человеком 905:
   ```
   avia.statedb: avia.sqlite: перенос из users.sqlite (4 строк), subscriptions.sqlite (3 строк), history.sqlite (4 строк), metrics.sqlite (16 строк)
   avia.statedb: avia.sqlite: схема 0 → 2 за 0.0 с
   avia.chat_state: сессий из базы: 0 (поиск шёл в 0, шаг снят)
   avia.chat_state: выдач из базы: 0
   avia.sqlite: user_version 2, journal_mode wal, таблицы КТ4 [chat_sessions, chat_snapshots, db_legacy_tables, db_meta], чаты {chat_sessions: 2, chat_snapshots: 2}
   четыре файла после фазы single: не изменились
   ```
   Таблица скоростей и бэкап берут `avia.sqlite` (бэкап — 1 файл, `avia`). 0 ERROR.
3. `python -m avia.cli db-rollback` с `DB_SINGLE=1` в окружении (как в `.env` в момент отката) — код 0:
   ```
   users.sqlite: users 5
   subscriptions.sqlite: subscriptions 1, sub_prices 2, notifications 2
   history.sqlite: price_history 8, route_daily 0, scheduler_heartbeat 0, bg_collect_slots 0
   metrics.sqlite: site_runs 16, attempts 4, searches 8, parser_watch_state 1, price_timeline 2
   Готово: avia.sqlite отложена, прежние файлы — *.pre-rollback-*. Дальше DB_SINGLE=0 в .env и перезапуск бота.
   ```
   - В `state/` — четыре файла.
   - Отложены `avia.sqlite.rolledback-20261108-220154` и четыре `*.sqlite.pre-rollback-20261108-220154`.
   - Строк по таблицам в `avia.sqlite` до отката столько же, сколько в четырёх файлах после: 49 строк в 13 таблицах.
4. `old_after` (`b8909ab`, флаг не читает) — первым шагом ищет записи фазы `single`: `{"user (UserStore.get)": 1, "attempts": 1, "price_timeline": 1, "price_history": 4, "sub_prices": 1, "notifications": 1, "searches": 1}` — все на месте. Потом все 14 шагов, 0 ERROR. `integrity_check` — `ok`.

**Проверка ловит поломку:**
- `--drop-after-rollback sub_prices` удаляет после `db-rollback` одну строку `single-sub` → код 1: `markers_single: записей фазы single не видно: ['sub_prices']`.
- `--old-src` — копия `b8909ab`, где `INSERT INTO searches` идёт без списка колонок → код 1: в `old` и `old_after` по 4 × `ERROR avia.metrics` и «в searches не добавилось строк».
- **Без `db-rollback`:** `legacy` → `single` → `b8909ab`, фазы скрипта запущены вручную → `markers_single`: не видно ни одной из 7 меток (`user`, `attempts`, `price_timeline`, `price_history`, `sub_prices`, `notifications`, `searches` — все 0). Отсюда правило выше.

**Как откатить выпуск** (на Маке владельца):
1. Если на сервере `DB_SINGLE=1`, сначала откатить флаг кодом выпуска 2 — раздел «Откат флага без потерь». В `b8909ab` команды `db-rollback` нет.
2. `git switch --detach b8909ab`, затем `./deploy/botctl.sh deploy`. `rsync --delete` уберёт `statedb.py`, `chat_state.py`, `sub_prefilter.py`; `state/`, `debug/` и `.env` не трогает.
3. Строки `DB_SINGLE`, `SUB_PREFILTER*` в `.env` старый код не читает. После `db-rollback` `DB_SINGLE` уже 0 (шаг 3 отката флага). Если оставить 1, повторная выкладка выпуска 2 сразу снова перенесёт четыре файла в новую `avia.sqlite`.
4. Обратно — снова `deploy` с `release-2`. Фаза `new2` части А показывает, что код выпуска читает и строки, которые записал `b8909ab`.

## Проверки

Все — без прокси, `PYTHONDONTWRITEBYTECODE=1`. Флаги выпусков 1 и 2 сняты `env -u`. Перед каждым прогоном `pgrep -f '[l]oadtest\.(run|gate)|[-]m pytest'` пуст. Ждал через Monitor с until-циклом.

### pytest

`~/avia-scale/upstream/.venv/bin/python -m pytest -q -p no:cacheprovider` на `a09bc2d`, 21:56:46–21:57:52:

```
1505 passed, 3 skipped, 6 xfailed in 65.24s (0:01:05)
```

- 1505 = 1378 (выпуск 1) + 49 (`kt4-db`) + 78 (`kt4-prefilter`). Совпадает с копией приёмщика (`kt4.md`).
- 6 xfailed — `kt4-pair` без флага, 3 skipped — `sklearn`, как в выпуске 1.
- При `DB_SINGLE=1` полный pytest не гонял: 12 тестов владельца пишут в `tmp/metrics.sqlite` напрямую и при флаге падают по построению (`kt4.md`, замечание 4).

### gate quick, флаги выключены

`cd ~/avia-scale/kt0-up && PYTHONPATH=~/avia-scale/release-2/src:~/avia-scale/kt0-up .venv/bin/python -m loadtest.gate --profile quick --tag _rel2 --results ~/avia-scale/release-2/.sim_results`. `kt0-up` — `8f92434`, после прогонов `git status` пуст. 21:58:18–21:58:40, **код 0, 8/8**.

| прогон | метрика | порог | факт | ok |
|---|---|---|---|---|
| A 10/2 | card_latency.n | ≥ 10 | 10 | ok |
| A 10/2 | card_latency.p95 | ≤ 103 | 90.2 | ok |
| A 10/2 | card_latency.max | ≤ 103 | 90.2 | ok |
| A 10/2 | card_sites.mean | ≥ 0.81 | 0.9 | ok |
| A 50/4 | card_latency.n | ≥ 50 | 50 | ok |
| A 50/4 | card_latency.p95 | ≤ 104 | 90.8 | ok |
| A 50/4 | card_latency.max | ≤ 110 | 91.2 | ok |
| A 50/4 | card_sites.mean | ≥ 0.55 | 0.6 | ok |

### gate quick, `DB_SINGLE=1`

Та же команда с `DB_SINGLE=1`, `--tag _rel2db`. 21:58:40–21:59:05, **код 0, 8/8**. Флаг действовал: `backups` в JSON прогонов — `avia-20261108-2158.sqlite.gz`, при `_rel2` — четыре файла. Поле `subscriptions` в JSON при 1 пустое: `loadtest/run.py` читает `subscriptions.sqlite`, его нет (`kt4_db.md`). Пороги на него не смотрят.

| прогон | метрика | порог | факт | ok |
|---|---|---|---|---|
| A 10/2 | card_latency.n | ≥ 10 | 10 | ok |
| A 10/2 | card_latency.p95 | ≤ 103 | 90.3 | ok |
| A 10/2 | card_latency.max | ≤ 103 | 90.3 | ok |
| A 10/2 | card_sites.mean | ≥ 0.81 | 0.9 | ok |
| A 50/4 | card_latency.n | ≥ 50 | 50 | ok |
| A 50/4 | card_latency.p95 | ≤ 104 | 91.1 | ok |
| A 50/4 | card_latency.max | ≤ 110 | 91.2 | ok |
| A 50/4 | card_sites.mean | ≥ 0.55 | 0.6 | ok |

### gate full, флаги выключены

`--profile full --tag _rel2`. 21:59:05–22:00:20, **код 0 с первого раза, повтора по П22 не было**.

| прогон | метрика | порог | факт | ok |
|---|---|---|---|---|
| A 100/8 | card_latency.n | ≥ 90 | 100 | ok |
| A 100/8 | card_latency.p95 | ≤ 104 | 90.6 | ok |
| A 100/8 | card_latency.max | ≤ 109 | 90.9 | ok |
| A 100/8 | card_sites.mean | ≥ 0.43 | 0.46 | ok |
| A 50/8 prod | card_latency.n | ≥ 50 | 50 | ok |
| A 50/8 prod | card_latency.p95 | ≤ 105 | 91.6 | ok |
| A 50/8 prod | card_latency.max | ≤ 106 | 91.8 | ok |
| A 50/8 prod | card_sites.mean | ≥ 0.97 | 1.12 | ok |
| A 100/8 prod | card_latency.n | ≥ 95 | 100 | ok |
| A 100/8 prod | card_latency.p95 | ≤ 105 | 91.6 | ok |
| A 100/8 prod | card_latency.max | ≤ 109 | 91.7 | ok |
| A 100/8 prod | card_sites.mean | ≥ 0.84 | 0.91 | ok |

Низкая ветка `A 100/8 prod` (0,58–0,63 в выпуске 1, есть и на `59627b8`) в этот раз не выпала. Один прогон этого не опровергает.

## Что не сделано и не проверено

- Push и PR — нельзя по заданию. Не проверял, что `main` на GitHub совпадает с выложенным `b8909ab` (сеть не трогал).
- `gate --profile full` при `DB_SINGLE=1` и любой gate при `SUB_PREFILTER=1` не гонял. Для предфильтра в симуляторе нет Data API, в сеть ходить нельзя. Поведение под ним — по тестам и модели `kt4_prefilter.md`.
- Перенос настоящих прод-баз (`history` 63 МБ) не проверен: в проверке отката базы маленькие, в тестах `kt4-db` — фикстуры и синтетика 149 МБ. Первое `DB_SINGLE=1` на сервере — первый настоящий перенос.
- Проверка отката идёт на заглушках поиска и Telegram, сессий в базе при `single` — 2 (выдачи `store.put`). Перезапуск с живыми диалогами не проверял: это `tests/test_restart.py` ветки `kt4-db`.
- Решения из отчётов веток остаются открытыми:
  - `SUB_PREFILTER_FULL_HOURS` и порог плана «≤ 50 % сборов» (П36);
  - условие ADR-003 «до вылета < 7 дней — всегда полный сбор»;
  - ретеншн `sub_prices`, `attempts`, `searches`;
  - перевод тестов владельца на `db_path`, если `DB_SINGLE` включать надолго.
- `.sim_results/` в `.gitignore` нет. JSON gate — в `~/avia-scale/release-2/.sim_results`, не в git.
