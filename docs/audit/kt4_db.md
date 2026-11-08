# КТ4, работа 1: одна SQLite и рестарт без потерь (ADR-002)

Ветка `kt4-db` от `kt2-tpcache` `59627b8` (КТ1 и КТ2 поверх `main` `9ba5ae3`). Флаг `DB_SINGLE`, по умолчанию 0. Пушей нет.

| Коммит | Что |
|---|---|
| `e3ad5f6` | тесты (RED) |
| `0ea4336` | код: `statedb.py`, `chat_state.py`, подключение в боте |
| `a2d24f3` | правки по ревью: отказ старта на устаревшей `avia.sqlite`, выдачи в zlib, чистка TTL по ходу работы |
| этот | отчёт и `kt4_db_growth.py` |

## Что и где

- `src/avia/statedb.py` (новый):
  - флаг: `db_single_enabled()` — `DB_SINGLE=1` в окружении (`.env` через `load_settings`);
  - `db_path(storage_dir, name)` — единственное место с именами баз. При 0 отдаёт прежние `users.sqlite`, `subscriptions.sqlite`, `history.sqlite`, `metrics.sqlite`; при 1 — `avia.sqlite`, уже перенесённую;
  - `ensure_single_db` — WAL, `PRAGMA user_version`, миграции по номеру `MIGRATIONS`:
    - 1 — перенос четырёх файлов;
    - 2 — таблицы `chat_sessions`, `chat_snapshots`;
  - `export_legacy` — откат в четыре файла.
- `src/avia/chat_state.py` (новый): кодирование сессии и выдачи в JSON, `ChatStateStore` (TTL, запись только изменений), `PersistentResultStore` (`ResultStore` владельца, после `put` пишет чат в базу).
- `src/avia/bot.py`:
  - пути баз — через `db_path`;
  - при `DB_SINGLE=1` бот берёт сессии и выдачи из базы (`open_chat_state`);
  - `_save_chat_state` вызывается после `dialog.on_text` / `on_callback` и после `_process_actions` (в `finally`) в `handle` и `handle_callback`. При 0 метод сразу выходит.
- Пути баз через `db_path`: `dispatcher.py`, `hedge.py`, `pipeline.py`, `droidhub.py`, `speed.py`, `phone_check.py`, `cli.py`.
- `src/avia/backup.py`: при 1 копируется только `avia.sqlite`.
- `src/avia/cli.py`: команда `db-rollback`.
- Тесты: `tests/test_migrations.py`, `test_restart.py`, `test_db_rollback.py`, `test_db_single.py`, `test_db_single_writes.py`, `test_chat_state.py`, фикстуры `tests/legacy_db_fixtures.py`. Тесты владельца не менялись.

При `DB_SINGLE=0` поведение прежнее:
- те же четыре файла: `db_path` даёт тот же `Path`;
- те же SQL: сторы владельца не менялись;
- сессии и выдачи — в памяти: `chat_state = None`, `ResultStore()`.

Проверки флага 0:
- `test_flag_off_keeps_owner_behaviour`;
- `test_backup_flag_off_unchanged`;
- 1135 тестов владельца;
- quick при 0. В его каталогах — четыре файла, `avia.sqlite` нет.

## Разведка

### Где открываются четыре базы (на `59627b8`)

| Место | База |
|---|---|
| `bot.py:274`, `:288`, `:296`, `:313` | users, metrics, history, subscriptions (сторы бота) |
| `bot.py:1950` | history (оценка роста в `/admin` → «БД») |
| `bot.py:1930` | `glob("*.sqlite")` — только размеры файлов для `/admin` |
| `dispatcher.py:34` (`_get_metrics`) | metrics |
| `hedge.py:75` | metrics |
| `pipeline.py:426` (`record_timeline`) | metrics |
| `droidhub.py:1221` (`_record_failure`) | metrics |
| `speed.py:75`, `:77`, `:689`, `:691` | metrics (`SpeedTable`, `get_speed_table`; `:77` и `:691` — запасной путь `state/` без настроек) |
| `phone_check.py:121`, `:143`, `:462`, `:749` | metrics, history, metrics, metrics |
| `cli.py:21`, `:36` | history (`forecast-train`), metrics (`stop-calibrate`) |
| `backup.py:55–59` | все `*.sqlite` каталога `state/` |
| `stage.py:61` | путь или стор передаёт вызывающий: своего имени файла нет |

Теперь все эти места берут путь из `db_path`. `tests/test_db_single.py::test_no_module_builds_old_file_names_itself` проверяет, что имена четырёх файлов в коде есть только в `statedb.py`.

Вне ветки: `loadtest/run.py:418`, `:422`, `:458` в `kt0-up` читают `metrics.sqlite` и `subscriptions.sqlite` из каталога прогона. При 1 этих файлов нет, и поля `empty_jobs`, `stop_reason`, `subscriptions` в JSON прогона пустые. Пороги gate на них не смотрят. `kt0-up` не менялся.

### Таблицы

| База | Таблицы | Индексы |
|---|---|---|
| users | `users` | — |
| subscriptions | `subscriptions`, `sub_prices`, `notifications` | 3 |
| history | `price_history`, `route_daily`, `scheduler_heartbeat`, `bg_collect_slots` | 7 + 2 авто |
| metrics | `site_runs`, `attempts`, `searches`, `parser_watch_state`, `price_timeline` | 8 + 1 авто |

Совпадающих имён таблиц и индексов нет. Общая только служебная `sqlite_sequence` в subscriptions, history и metrics. Её строки называются по таблицам, а таблицы разные, поэтому строки переносятся без конфликтов, со значением `seq` из старого файла: AUTOINCREMENT не выдаёт заново id удалённых строк. Если совпадение появится, перенос остановится с `MigrationError` (`test_name_collision_stops_migration`). Колонки КТ2 (`card_*`, `full_min_price`, `preview_*` в `searches`) едут вместе с `CREATE TABLE` из старого файла. В базе до КТ2 их добавляет `ALTER` владельца уже в `avia.sqlite` (`test_metrics_before_kt2_gets_columns_from_owner_store`).

### Что в сессии и выдаче

- **Сессия** `dialog.Session`: шаг (`Step`, str-Enum), города, даты (`date`), пассажиры, багаж, `dialog_id`, `SearchCriteria` (frozen dataclass, части суток — `frozenset`), `threshold_sub_id`.
- **Выдача** `store.SearchSnapshot`: `Query` (frozen, даты), `results: list[SiteResult]` с `Offer` (вложены `Leg`, `OfferAlt`), `flat`, `pool`, `criteria`, `shown`, `view`, `created_at`, счётчики отброшенных.

Замыканий, соединений и задач в сессии и выдаче нет. Что напрямую не сохраняется и как это решено:

| Что | Решение |
|---|---|
| Общие объекты: один `Offer` лежит в `results`, `flat` и `pool` сразу | Билеты пишутся одним списком, ссылки — по номеру. После загрузки это снова один объект (`test_session_and_snapshot_roundtrip`) |
| Атрибуты сверх полей: `fresh_cache.py:86` делает `setattr(o, "cached_age", …)` | Пишутся вместе с полями и восстанавливаются атрибутом (`cached_age` в `test_restart`) |
| `Step` — str-Enum, `date`, `frozenset` | JSON с метками типов. Список классов закрыт. Тест `test_every_field_type_is_supported` упадёт, если в модели появится поле неподдержанного типа |
| Живой поиск в момент рестарта (шаг `SEARCHING`) | Поиск умирает с процессом. При загрузке шаг становится `DONE`, как в `_leave_searching`, иначе человек получал бы «⏳ Поиск ещё идёт» |

### TTL

- **Выдача** — из кода: `ResultStore(ttl=1800.0)` (`store.py:37`), 30 мин от создания выдачи.
- **Сессия** — в коде владельца TTL нет: сессии живут в памяти до рестарта. Предлагаю **сутки от последнего изменения** (`SESSION_TTL_SECONDS = 86 400`):
  - недописанный вечером диалог продолжается утром;
  - старше суток даты в диалоге чаще всего уже прошли, и человек начинает с меню, как сейчас после любого рестарта.
- **Строки** — по одной на чат, поэтому база растёт с числом людей, а не поисков. Старше TTL строки удаляются при старте и раз в 10 мин при записи.

## Флаг

**Включить:**
1. Бэкап, как обычно: `./deploy/botctl.sh backup`.
2. `DB_SINGLE=1` в `.env` на сервере.
3. `./deploy/botctl.sh restart`.

При первом старте перенос идёт в одной транзакции. Старые файлы открыты только на чтение (`ATTACH … ?mode=ro`) и не меняются (`test_old_files_untouched`). Повторный старт ничего не переносит. Перенос 149 МБ (440 тыс. строк `price_history`, 114 тыс. `site_runs`, 54 тыс. `attempts`) занял 1,9 с.

**Откатить без потерь:**
1. `./deploy/botctl.sh stop`.
2. На сервере: `cd /home/avia/avia-bot && sudo -u avia .venv/bin/python -m avia.cli db-rollback` — всё из `avia.sqlite` уходит в четыре файла, включая записанное после переноса.
   - Прежние четыре файла откладываются в `<имя>.sqlite.pre-rollback-<время>`, `avia.sqlite` — в `avia.sqlite.rolledback-<время>`. Ничего не удаляется.
   - Сессии и выдачи в файлы не едут: при 0 они в памяти.
   - Если команду прервали, её можно запустить ещё раз: `avia.sqlite` остаётся на месте до последнего шага.
3. `DB_SINGLE=0` в `.env`.
4. `./deploy/botctl.sh start`.

Следующее включение `DB_SINGLE=1` перенесёт файлы заново (`test_flag_on_again_imports_fresh_files`).

**Если переключить 1 → 0 → 1 без `db-rollback`:**
- записи времени 0 есть только в старых файлах;
- бот при 1 не стартует с `MigrationError`: «… изменились после переноса …», с подсказкой, что делать (`test_flag_toggled_without_rollback_refuses_start`);
- признак изменения — размер файла и счётчик изменений из заголовка SQLite, не mtime. Удалённые старые файлы — не ошибка.

**Восстановление из бэкапа при 1:**
1. Остановить бота.
2. Переименовать `avia.sqlite`, `avia.sqlite-wal` и `avia.sqlite-shm`: старый `-wal` рядом с восстановленным файлом испортит его.
3. `gunzip -c state/backups/avia-ГГГГММДД-ЧЧММ.sqlite.gz > state/avia.sqlite`.
4. Запустить бота.

Бэкап берёт базу через backup API SQLite, то есть вместе с WAL. `test_backup_takes_single_db_with_wal_and_restores` пишет строки, которые есть только в `-wal` (копия одного основного файла их не видит), снимает бэкап, восстанавливает его в новый каталог и сравнивает все таблицы.

## Запись: хватает `busy_timeout` и коротких транзакций

ADR-002 даёт выбор: один поток или `aiosqlite`. Для одного процесса хватает синхронных коротких транзакций, отдельный поток не нужен:
- `busy_timeout` — 10 с: тот же `timeout=10`, что у сторов владельца; проверено `PRAGMA busy_timeout`;
- запись метрик владельца и так идёт в своём потоке (КТ1).

Цифры (`pytest -s tests/test_db_single_writes.py`, Мак разработчика):

| Замер | Результат |
|---|---|
| `save_chat` (сессия и выдача 5 сайтов × 15 билетов: 46,8 КБ JSON, в базе zlib) | p50 1,09 мс, p99 1,45 мс, макс. 1,54 мс, n = 200 |
| Обслуживание раз в сутки и при старте, 200 тыс. строк `price_history`, удаляется 1 день | `rebuild_route_daily` 23 мс, `prune` 30 мс |
| Запись на цикле в это время | `users.register` макс. 22,7 мс, `save_chat` макс. 11,2 мс |
| Догоняющий `prune` за 15 дней | 742 мс. Самое долгое ожидание записи — 676 мс (одна запись из 62), медиана 0,4–0,9 мс |
| «database is locked» | 0 |

Чем платим: раньше `users.sqlite` не ждал чистки истории. Теперь синхронная запись на цикле может ждать самую длинную транзакцию обслуживания: обычно до ~25 мс, в худшем случае — секунду раз в сутки. Если это станет заметно, `history.prune` удаляет порциями (предложение, кода нет).

`save_chat` на цикле кодирует выдачу и тогда, когда она не менялась: сравнение идёт по отпечатку JSON. Самая большая выдача симулятора (1495 билетов) — 11,7 мс на кодирование. В проде билетов меньше.

## Проверки

### RED на `59627b8` (коммит `e3ad5f6`)

```
ERROR tests/test_migrations.py
ERROR tests/test_restart.py
ERROR tests/test_db_rollback.py
ERROR tests/test_db_single.py
ERROR tests/test_db_single_writes.py
!!!!!!!!!!!!!!!!!!! Interrupted: 5 errors during collection !!!!!!!!!!!!!!!!!!!!
5 errors in 0.29s
```
Причина — `ImportError: cannot import name 'statedb' from 'avia'` (и `chat_state`).

Тесты правок по ревью на коде `0ea4336`:
```
FAILED tests/test_chat_state.py::test_expired_rows_pruned_while_running_and_not_resurrected
FAILED tests/test_chat_state.py::test_unchanged_chat_is_not_rewritten - TypeE...
FAILED tests/test_migrations.py::test_flag_toggled_without_rollback_refuses_start
FAILED tests/test_db_rollback.py::test_two_rollbacks_in_one_second_keep_both_copies
FAILED tests/test_db_rollback.py::test_view_stops_export - Failed: DID NOT RA...
5 failed, 9 passed in 0.34s
```
Проходят защитные тесты: типы полей и «старые файлы удалены».

### GREEN

- Новые тесты на `0ea4336`: `35 passed in 6.25s`.
- На `a2d24f3`: `49 passed in 7.01s`.

### pytest

Полный прогон без прокси (`~/avia-scale/upstream/.venv`), флаг по умолчанию 0:
- `59627b8`: `1135 passed, 3 skipped in 49.07s`;
- `a2d24f3`: `1184 passed, 3 skipped in 57.18s` (21:39:34–21:40:32).

### gate --profile quick, код `a2d24f3`

Оба прогона — код 0. JSON — в `.sim_results/*_kt4db{0,1}.json`, не в git.

| прогон | метрика | порог | `DB_SINGLE=0` | `DB_SINGLE=1` |
|---|---|---|---|---|
| A 10/2 | card_latency.n | ≥ 10 | 10 | 10 |
| A 10/2 | card_latency.p95 | ≤ 103 | 90.3 | 90.3 |
| A 10/2 | card_latency.max | ≤ 103 | 90.3 | 90.3 |
| A 10/2 | card_sites.mean | ≥ 0.81 | 1 | 0.9 |
| A 50/4 | card_latency.n | ≥ 50 | 50 | 50 |
| A 50/4 | card_latency.p95 | ≤ 104 | 91.3 | 91.3 |
| A 50/4 | card_latency.max | ≤ 110 | 91.4 | 91.7 |
| A 50/4 | card_sites.mean | ≥ 0.55 | 0.6 | 0.6 |

Время прогонов: при 0 — 21:40:40–21:41:02, при 1 — 21:41:02–21:41:27.

На `0ea4336` оба quick тоже дали код 0 и 8/8. `card_sites.mean` в A 10/2 скачет между 0,9 и 1 и при 0, и при 1.

Размер баз A 50/4:
- при 0 — четыре файла, 241 664 Б;
- при 1 — `avia.sqlite`, 847 872 Б. Разница — 50 сессий и 50 выдач: в среднем 11 КБ zlib на выдачу. До сжатия выдачи было 14,6 МБ: 271 КБ JSON на выдачу, максимум 1,1 МБ.

## Рост базы

Как считалось:
- Скрипт: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -B docs/audit/kt4_db_growth.py SIM.sqlite ~/avia-scale/prod_stats/2026-11-08.json OUT`.
- `SIM.sqlite` — `avia.sqlite` из A 50/4 quick при 1. Таблицы подписок, людей и сессий заполняет синтетическая база через API сторов владельца.
- Байт на строку — `dbstat` (занятые байты таблицы и её индексов).

**Байт на строку:**

| Таблица | Байт | Таблица | Байт |
|---|---|---|---|
| `price_history` | 258 | `subscriptions` | 204 |
| `attempts` | 155 | `sub_prices` | 64 |
| `price_timeline` | 140 | `notifications` | 78 |
| `site_runs` | 129 | `users` | 47 |
| `searches` | 75 | `chat_sessions` | 462 |
| `scheduler_heartbeat` | 23 | `chat_snapshots` | 11 013 (среднее в симуляторе, zlib; макс. 43 945) |

**На единицу:**
- **Живой поиск:** 20,9 КБ — верхняя граница: в `price_history` по 15 строк на сайт с ценами (`history_max_offers_per_site`). Как в симуляторе — 2,5 КБ: 0,39 строки истории на сбор сайта, повторы маршрутов в пределах `SAME_PRICE_TTL`.
  - Строк на поиск: `searches` 1, `site_runs` 5,2, `attempts` 7,9 (по проду), `price_timeline` 0,1, `price_history` до 73.
- **Подписка:** 204 Б строка.
  - На проверку: 64 Б в `sub_prices` и сборы сайтов по 4,0 КБ (`site_runs` и до 15 строк истории) плюс 155 Б на попытку. Проверка всех 5,2 сайтов — до 21 КБ.
  - При интервале владельца 30–60 мин это до 0,5–1 МБ в сутки на подписку (верхняя граница).
- **Человек:** 11,5 КБ — `users` 47 + сессия 462 + выдача ~11 КБ. Строка на чат, держится не дольше TTL.

**Прод** (`prod_stats/2026-11-08.json`):
- Окно выгрузки — 7 суток, но данные моложе: поиски по дням есть с 06.11. Если делить на 7 суток, на поиск вышло бы 2,7 primary-попытки user при 5,2 сайта на поиск — так не бывает. Поэтому делю на 2,65 суток (06.11 00:00 – 08.11 15:30).
- Поисков людей 11 в сутки: 24 быстрых и 5 глубоких; в задании — 8–10.
- Попыток в сутки: user 87, sub 512, bg 322.
- Сборов (primary): sub 494, bg 317.

| Источник | МБ/сут | МБ/мес |
|---|---|---|
| живые поиски | 0,23 | 6,9 |
| подписки | 2,06 | 61,8 |
| фон | 1,32 | 39,5 |
| пульс планировщика | 0,01 | 0,2 |
| **всего, верхняя граница** | **3,61** | **108** |
| то же при делении на 7 суток | — | 41 |

Из 108 МБ/мес ~100 — `price_history`; её владелец уже держит в 45 сутках (`history.prune`, свёртка в `route_daily`). Устойчивый размер истории при верхней границе — ~150 МБ. Остальное — ~8 МБ/мес:
- `attempts` ~4,3 МБ;
- `site_runs` ~3,4 МБ — чистится через 90 суток;
- сессии и выдачи по проду почти не растут: строка на человека.

**Ретеншн — предложение, кода нет:**
- `price_history` — уже 45 суток у владельца, в рамках ADR (30–90). При 30 устойчивый размер ~100 МБ.
- `sub_prices`: сейчас не чистится. `min_price` подписки считается по всем её строкам, `first_price` — по первой. Предложение: удалять строки подписок, неактивных или с прошедшей датой вылета больше 30 суток. У активных — хранить 90 суток плюс первую строку и строку с минимумом. Рост маленький, ~0,2 МБ/мес, но таблица бесконечная.
- `attempts` и `searches` не чистятся (`metrics.prune` трогает только `site_runs` и `price_timeline`). Предложение: те же 90 суток.

## Что смотреть владельцу после включения

1. **Лог старта:**
   - `avia.sqlite: перенос из users.sqlite (N строк), …`;
   - `схема 0 → 2 за X с`;
   - `сессий из базы: N (поиск шёл в K, шаг снят)`;
   - `выдач из базы: N`.
   
   `MigrationError` — бот не стартует. Вернуть `DB_SINGLE=0` и прислать текст ошибки.
2. **После деплоя:** люди продолжают диалог, кнопки под выдачей младше 30 мин работают, «⌛ Результаты поиска устарели» после рестарта почти не встречается.
3. **Ошибки в логе** «не удалось сохранить сессию и выдачу» — сохранение сломалось, бот при этом отвечает.
4. **`/admin` → «БД»:**
   - файлов теперь пять: `avia.sqlite` и четыре старых, они больше не меняются;
   - «рост МБ/сут» при 1 завышен: делится размер всей `avia.sqlite`, а не одной истории.
5. **Бэкапы** — только `avia-*.sqlite.gz`. Старые четыре файла не копируются: их копии есть в бэкапах до включения.
6. **Диск:**
   - `avia.sqlite` ≈ сумма четырёх файлов плюс ~11 КБ на активного человека;
   - `avia.sqlite-wal` обычно до ~4 МБ (автоматический checkpoint SQLite); если растёт без конца — сообщить;
   - когда флаг приживётся, старые четыре файла можно убрать: бот это переносит.
7. **Откат** — только через `db-rollback` при остановленном боте (раздел «Флаг»).

## Что не сделано и ограничения

- Прод, телефоны, VPS и сеть не трогал. Включение на сервере и прогон на настоящих базах — за владельцем. Перенос проверен на фикстурах и синтетике 149 МБ.
- Версия SQLite на сервере не проверена:
  - в коде — UPSERT (3.24+, у владельца уже есть) и `ATTACH … ?mode=ro`;
  - `RETURNING` намеренно не используется;
  - `DROP COLUMN` есть только в тестовой фикстуре.
- Попытка, списанная за живой поиск, который убил рестарт, не возвращается (P29 не успевает). Так было и раньше, не менял.
- Запасной путь `speed.py` без настроек (`Path("state")`) при 1 создаёт `state/avia.sqlite` относительно текущего каталога. В проде каталог — корень бота, это та же `state/`.
- Откат четырёх файлов меняет их по одному в конце. Если команду прервать посередине, её нужно запустить снова: `avia.sqlite` остаётся на месте до последнего шага.
- Полный pytest при `DB_SINGLE=1` не гонял: часть тестов владельца читает `tmp_path/"metrics.sqlite"` напрямую и при 1 упадёт по построению. Флаг 1 покрыт новыми тестами и quick.
- `gate --profile full` перед передачей владельцу — по П33, вместе с выпуском 2.
